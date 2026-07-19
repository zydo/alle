"""Read sing-box binary rule-set (``.srs``) files and answer membership.

The rule-match tracer needs "is this domain/IP inside geosite/geoip category
X?" — a question sing-box only answers at packet time. This module reads the
same cached ``.srs`` files the engine compiles into ``type: local`` rule_set
entries (fetched and digest-verified by :mod:`alle.geodata`) and evaluates
them offline, so the tracer never needs a running sing-box.

The format (sing-box ``common/srs/binary.go``, pinned by SINGBOX_VERSION) is
*not* protobuf: a 3-byte ``SRS`` magic + one version byte + a zlib stream.
Inside: a uvarint rule count, then rules — each either *default* (a sequence
of typed items ending in ``0xFF`` + an invert flag) or *logical* (and/or over
nested rules). Domains and domain suffixes are stored as ONE succinct trie
(sing ``common/domain``) over reversed domain strings, with two marker
labels: ``\\r`` ("match any remaining prefix" — a suffix entry written with a
leading dot) and ``\\n`` ("dot-boundary suffix or the domain itself" — a
suffix entry without the dot). IP CIDRs are stored as a sorted list of
(from, to) address ranges. The matcher here mirrors sing-box's own
``Matcher.has`` walk over that trie bit-for-bit, so the tracer's verdicts are
the engine's semantics, not an approximation.

Within one default rule sing-box ORs the destination-address items (domain
trie, keywords, regexes, ip_cidr set) — a destination matches the rule if any
of them hits (``route/rule/rule_abstract.go``). Items that constrain other
dimensions (network, ports, process, wifi, …) never appear in the pinned
geosite/geoip sources; a rule carrying one is treated as not matching a bare
destination (conservative, disclosed via :attr:`RuleSet.unsupported`).
"""

from __future__ import annotations

import io
import ipaddress
import re
import zlib
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"SRS"
# Highest container version this reader understands (sing-box 1.13 writes 3).
# Newer versions only add rule item types; the item loop below fails cleanly
# on ones we do not know, so the version byte itself is not gated.
_PREFIX_LABEL = 0x0D  # '\r' — trie label: match any remaining prefix
_ROOT_LABEL = 0x0A  # '\n' — trie label: dot-boundary suffix or the domain itself

# Rule item type bytes (binary.go's ruleItem* constants, in iota order).
_ITEM_QUERY_TYPE = 0
_ITEM_NETWORK = 1
_ITEM_DOMAIN = 2
_ITEM_DOMAIN_KEYWORD = 3
_ITEM_DOMAIN_REGEX = 4
_ITEM_SOURCE_IP_CIDR = 5
_ITEM_IP_CIDR = 6
_ITEM_SOURCE_PORT = 7
_ITEM_SOURCE_PORT_RANGE = 8
_ITEM_PORT = 9
_ITEM_PORT_RANGE = 10
_ITEM_PROCESS_NAME = 11
_ITEM_PROCESS_PATH = 12
_ITEM_PACKAGE_NAME = 13
_ITEM_WIFI_SSID = 14
_ITEM_WIFI_BSSID = 15
_ITEM_ADGUARD_DOMAIN = 16
_ITEM_PROCESS_PATH_REGEX = 17
_ITEM_NETWORK_TYPE = 18
_ITEM_NETWORK_IS_EXPENSIVE = 19
_ITEM_NETWORK_IS_CONSTRAINED = 20
_ITEM_FINAL = 0xFF

_ITEM_NAMES = {
    _ITEM_QUERY_TYPE: "query_type",
    _ITEM_NETWORK: "network",
    _ITEM_SOURCE_IP_CIDR: "source_ip_cidr",
    _ITEM_SOURCE_PORT: "source_port",
    _ITEM_SOURCE_PORT_RANGE: "source_port_range",
    _ITEM_PORT: "port",
    _ITEM_PORT_RANGE: "port_range",
    _ITEM_PROCESS_NAME: "process_name",
    _ITEM_PROCESS_PATH: "process_path",
    _ITEM_PACKAGE_NAME: "package_name",
    _ITEM_WIFI_SSID: "wifi_ssid",
    _ITEM_WIFI_BSSID: "wifi_bssid",
    _ITEM_PROCESS_PATH_REGEX: "process_path_regex",
    _ITEM_NETWORK_TYPE: "network_type",
    _ITEM_NETWORK_IS_EXPENSIVE: "network_is_expensive",
    _ITEM_NETWORK_IS_CONSTRAINED: "network_is_constrained",
}


class SrsError(Exception):
    """The ``.srs`` file could not be parsed (corrupt, or a format this
    reader does not know)."""


def _read_exact(r: io.BufferedIOBase, n: int) -> bytes:
    data = r.read(n)
    if len(data) != n:
        raise SrsError("truncated rule-set file")
    return data


def _read_uvarint(r: io.BufferedIOBase) -> int:
    result = shift = 0
    while True:
        byte = _read_exact(r, 1)[0]
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result
        shift += 7
        if shift > 63:
            raise SrsError("uvarint overflow")


def _read_string_list(r: io.BufferedIOBase) -> list[str]:
    return [
        _read_exact(r, _read_uvarint(r)).decode("utf-8", errors="replace")
        for _ in range(_read_uvarint(r))
    ]


class _SuccinctTrie:
    """sing ``common/domain``'s succinct set, decoded for membership queries.

    LOUDS layout: ``label_bitmap`` walks nodes in BFS order — a 0 bit per
    child edge (its label in ``labels``), a 1 bit terminating each node's
    edge list; ``leaves`` marks which nodes end a stored key. The query walk
    below is a line-for-line port of ``Matcher.has`` (matcher.go), marker
    labels included.
    """

    def __init__(self, leaves: list[int], label_bitmap: list[int], labels: bytes):
        self.leaves = leaves
        self.label_bitmap = label_bitmap
        self.labels = labels
        # rank index: ones in label_bitmap before each 64-bit word — enough
        # to answer rank/select with one popcount + bisect, no big bit array.
        self.ranks = [0]
        for word in label_bitmap:
            self.ranks.append(self.ranks[-1] + word.bit_count())

    def _get(self, words: list[int], i: int) -> int:
        word = i >> 6
        if word >= len(words):
            return 0
        return (words[word] >> (i & 63)) & 1

    def _rank1(self, i: int) -> int:
        """Ones in ``label_bitmap[:i]``."""
        word, bit = i >> 6, i & 63
        if word >= len(self.label_bitmap):
            return self.ranks[-1]
        return (
            self.ranks[word] + (self.label_bitmap[word] & ((1 << bit) - 1)).bit_count()
        )

    def _select1(self, i: int) -> int:
        """Position of the ``i``-th (0-based) 1 bit in ``label_bitmap``."""
        word = bisect_right(self.ranks, i) - 1
        remaining = i - self.ranks[word]
        bits = self.label_bitmap[word]
        pos = word << 6
        while True:
            if bits & 1 and remaining == 0:
                return pos
            if bits & 1:
                remaining -= 1
            bits >>= 1
            pos += 1

    def has(self, key: bytes) -> bool:
        node_id = bm_idx = 0
        for current in key:
            while True:
                if self._get(self.label_bitmap, bm_idx):
                    return False
                next_label = self.labels[bm_idx - node_id]
                if next_label == _PREFIX_LABEL:
                    return True
                if next_label == _ROOT_LABEL:
                    next_node = bm_idx + 1 - self._rank1(bm_idx + 1)
                    if current == ord(".") and self._get(self.leaves, next_node):
                        return True
                if next_label == current:
                    break
                bm_idx += 1
            node_id = bm_idx + 1 - self._rank1(bm_idx + 1)
            bm_idx = self._select1(node_id - 1) + 1
        if self._get(self.leaves, node_id):
            return True
        while True:
            if self._get(self.label_bitmap, bm_idx):
                return False
            next_label = self.labels[bm_idx - node_id]
            if next_label in (_PREFIX_LABEL, _ROOT_LABEL):
                return True
            bm_idx += 1

    def match_domain(self, domain: str) -> bool:
        return self.has(domain.encode()[::-1])


def _read_uint64_list(r: io.BufferedIOBase) -> list[int]:
    length = _read_uvarint(r)
    return [int.from_bytes(_read_exact(r, 8), "big") for _ in range(length)]


def _read_domain_matcher(r: io.BufferedIOBase) -> _SuccinctTrie:
    _read_exact(r, 1)  # set version byte (0)
    leaves = _read_uint64_list(r)
    label_bitmap = _read_uint64_list(r)
    labels = _read_exact(r, _read_uvarint(r))
    return _SuccinctTrie(leaves, label_bitmap, labels)


def _read_ip_ranges(r: io.BufferedIOBase) -> dict[int, list[tuple[int, int]]]:
    """Per-family sorted ``(from, to)`` integer ranges from a serialized IPSet."""
    if _read_exact(r, 1)[0] != 1:
        raise SrsError("unknown IP set version")
    # count is a fixed 8-byte big-endian uint64 here, not a varint (binary.go
    # keeps it that way for compatibility)
    count = int.from_bytes(_read_exact(r, 8), "big")
    ranges: dict[int, list[tuple[int, int]]] = {}
    for _ in range(count):
        frm = _read_exact(r, _read_uvarint(r))
        to = _read_exact(r, _read_uvarint(r))
        if len(frm) != len(to) or len(frm) not in (4, 16):
            raise SrsError("malformed IP range")
        ranges.setdefault(4 if len(frm) == 4 else 6, []).append(
            (int.from_bytes(frm, "big"), int.from_bytes(to, "big"))
        )
    for family in ranges.values():
        family.sort()
    return ranges


def _skip_uint16_list(r: io.BufferedIOBase) -> None:
    _read_exact(r, 2 * _read_uvarint(r))


def _skip_uint8_list(r: io.BufferedIOBase) -> None:
    _read_exact(r, _read_uvarint(r))


@dataclass
class _DefaultRule:
    trie: _SuccinctTrie | None = None
    keywords: list[str] = field(default_factory=list)
    regexes: list[re.Pattern] = field(default_factory=list)
    # per address family, ``(from, to)`` integer ranges sorted by ``from``
    # (netipx serializes them sorted and non-overlapping, so bisect works)
    ranges: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    invert: bool = False
    unsupported: list[str] = field(default_factory=list)

    def match(self, domain: str | None, ips: list) -> bool:
        if self.unsupported:
            # a constraint we cannot evaluate for a bare destination —
            # conservatively no match (surfaced via RuleSet.unsupported)
            return False
        if not (self.trie or self.keywords or self.regexes or self.ranges):
            # a rule with no items matches everything (sing-box semantics)
            return not self.invert
        matched = False
        if domain:
            matched = (
                (self.trie is not None and self.trie.match_domain(domain))
                or any(k in domain for k in self.keywords)
                or any(p.search(domain) for p in self.regexes)
            )
        if not matched and ips and self.ranges:
            matched = any(self._contains(ip) for ip in ips)
        return matched != self.invert

    def _contains(self, ip) -> bool:
        value = int(ip)
        family = self.ranges.get(ip.version) or []
        i = bisect_right(family, (value, 2**128)) - 1
        return i >= 0 and family[i][0] <= value <= family[i][1]


@dataclass
class _LogicalRule:
    mode: str  # "and" | "or"
    rules: list
    invert: bool

    @property
    def unsupported(self) -> list[str]:
        return [name for rule in self.rules for name in rule.unsupported]

    def match(self, domain: str | None, ips: list) -> bool:
        results = (rule.match(domain, ips) for rule in self.rules)
        matched = all(results) if self.mode == "and" else any(results)
        return matched != self.invert


def _read_default_rule(r: io.BufferedIOBase) -> _DefaultRule:
    rule = _DefaultRule()
    while True:
        item = _read_exact(r, 1)[0]
        if item == _ITEM_FINAL:
            rule.invert = bool(_read_exact(r, 1)[0])
            return rule
        if item == _ITEM_DOMAIN:
            rule.trie = _read_domain_matcher(r)
        elif item == _ITEM_DOMAIN_KEYWORD:
            rule.keywords = _read_string_list(r)
        elif item == _ITEM_DOMAIN_REGEX:
            for pattern in _read_string_list(r):
                try:
                    rule.regexes.append(re.compile(pattern))
                except re.error:
                    # Go RE2 syntax Python cannot compile — treat like an
                    # unevaluable constraint rather than silently dropping it
                    rule.unsupported.append(f"domain_regex:{pattern}")
        elif item == _ITEM_IP_CIDR:
            rule.ranges = _read_ip_ranges(r)
        # dimensions a bare destination has no answer for: parse past them
        # and mark the rule unevaluable
        elif item in (
            _ITEM_NETWORK,
            _ITEM_SOURCE_PORT_RANGE,
            _ITEM_PORT_RANGE,
            _ITEM_PROCESS_NAME,
            _ITEM_PROCESS_PATH,
            _ITEM_PACKAGE_NAME,
            _ITEM_WIFI_SSID,
            _ITEM_WIFI_BSSID,
            _ITEM_PROCESS_PATH_REGEX,
        ):
            _read_string_list(r)
            rule.unsupported.append(_ITEM_NAMES[item])
        elif item in (_ITEM_QUERY_TYPE, _ITEM_SOURCE_PORT, _ITEM_PORT):
            _skip_uint16_list(r)
            rule.unsupported.append(_ITEM_NAMES[item])
        elif item == _ITEM_SOURCE_IP_CIDR:
            _read_ip_ranges(r)
            rule.unsupported.append(_ITEM_NAMES[item])
        elif item == _ITEM_NETWORK_TYPE:
            _skip_uint8_list(r)
            rule.unsupported.append(_ITEM_NAMES[item])
        elif item in (_ITEM_NETWORK_IS_EXPENSIVE, _ITEM_NETWORK_IS_CONSTRAINED):
            rule.unsupported.append(_ITEM_NAMES[item])
        else:
            # AdGuard matchers and any post-1.13 item types have payloads we
            # cannot even skip safely — refuse the file rather than misread it
            raise SrsError(f"unsupported rule item type {item}")


def _read_rule(r: io.BufferedIOBase):
    rule_type = _read_exact(r, 1)[0]
    if rule_type == 0:
        return _read_default_rule(r)
    if rule_type == 1:
        mode_byte = _read_exact(r, 1)[0]
        if mode_byte not in (0, 1):
            raise SrsError(f"unknown logical mode {mode_byte}")
        rules = [_read_rule(r) for _ in range(_read_uvarint(r))]
        invert = bool(_read_exact(r, 1)[0])
        return _LogicalRule("and" if mode_byte == 0 else "or", rules, invert)
    raise SrsError(f"unknown rule type {rule_type}")


@dataclass
class RuleSet:
    """A parsed ``.srs`` file, ready for offline membership queries."""

    rules: list

    @property
    def unsupported(self) -> list[str]:
        """Constraint kinds present in the file that a bare destination
        cannot answer (rules carrying them are treated as not matching)."""
        return sorted({name for rule in self.rules for name in rule.unsupported})

    def match(self, domain: str | None = None, ips: list | None = None) -> bool:
        """True if any rule matches the destination — ``domain`` and/or its
        resolved ``ips`` (:mod:`ipaddress` address objects)."""
        ips = ips or []
        return any(rule.match(domain, ips) for rule in self.rules)


def parse(path: Path | str) -> RuleSet:
    """Parse one ``.srs`` file (or raise :class:`SrsError`)."""
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        raise SrsError(f"cannot read {path}: {e}") from e
    if not raw.startswith(MAGIC) or len(raw) < 5:
        raise SrsError(f"{path} is not a sing-box rule-set (bad magic)")
    try:
        body = zlib.decompress(raw[4:])
    except zlib.error as e:
        raise SrsError(f"{path}: corrupt rule-set body: {e}") from e
    r = io.BytesIO(body)
    try:
        rules = [_read_rule(r) for _ in range(_read_uvarint(r))]
    except SrsError as e:
        raise SrsError(f"{path}: {e}") from e
    return RuleSet(rules)


def match_ip(ruleset: RuleSet, ip: str) -> bool:
    """Convenience: membership of one IP literal."""
    return ruleset.match(ips=[ipaddress.ip_address(ip)])
