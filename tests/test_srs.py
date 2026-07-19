"""The .srs reader: parse sing-box binary rule-sets, answer membership.

The encoder here is a test-side mirror of sing-box's writer
(``common/srs/binary.go`` + sing ``common/domain``): the succinct-trie
construction is the same queue algorithm, the item encodings byte-identical.
The reader in :mod:`alle.srs` was additionally validated against real
published rule-sets using ``sing-box rule-set decompile`` as the oracle;
these tests keep the coverage hermetic (no network, no binary).
"""

from __future__ import annotations

import ipaddress
import zlib
from collections.abc import Sequence

import pytest

from alle import routes, srs

# ---- encoder (mirrors the Go writer) -----------------------------------------


def _uvarint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _string_list(items: list[str]) -> bytes:
    out = _uvarint(len(items))
    for s in items:
        raw = s.encode()
        out += _uvarint(len(raw)) + raw
    return out


def _bits_to_words(bits: list[int]) -> list[int]:
    words = [0] * ((len(bits) + 63) >> 6) if bits else []
    for i, bit in enumerate(bits):
        if bit:
            words[i >> 6] |= 1 << (i & 63)
    return words


def _succinct_set(keys: list[bytes]) -> bytes:
    """sing's ``newSuccinctSet`` (LOUDS trie over sorted keys), serialized."""
    leaves_bits: list[int] = []
    bitmap_bits: list[int] = []
    labels = bytearray()
    queue: list[tuple[int, int, int]] = [(0, len(keys), 0)]
    i = 0
    while i < len(queue):
        start, end, col = queue[i]
        while len(leaves_bits) <= i:
            leaves_bits.append(0)
        if start < end and col == len(keys[start]):
            leaves_bits[i] = 1
            start += 1
        j = start
        while j < end:
            frm = j
            while j < end and keys[j][col] == keys[frm][col]:
                j += 1
            queue.append((frm, j, col + 1))
            labels.append(keys[frm][col])
            bitmap_bits.append(0)
        bitmap_bits.append(1)
        i += 1
    out = b"\x00"  # set version
    for words in (_bits_to_words(leaves_bits), _bits_to_words(bitmap_bits)):
        out += _uvarint(len(words))
        out += b"".join(w.to_bytes(8, "big") for w in words)
    return out + _uvarint(len(labels)) + bytes(labels)


def _domain_matcher(exact: Sequence[str] = (), suffix: Sequence[str] = ()) -> bytes:
    """``NewMatcher`` non-legacy key building: ``\\n``-prefixed suffixes
    (dot-boundary + the root itself), ``\\r``-prefixed dotted suffixes."""
    keys = []
    for s in suffix:
        keys.append((("\r" + s) if s.startswith(".") else ("\n" + s)).encode()[::-1])
    keys.extend(d.encode()[::-1] for d in exact)
    return _succinct_set(sorted(set(keys)))


def _ip_set(cidrs: list[str]) -> bytes:
    ranges = []
    for c in cidrs:
        net = ipaddress.ip_network(c)
        ranges.append((net.network_address.packed, net.broadcast_address.packed))
    out = b"\x01" + len(ranges).to_bytes(8, "big")
    for frm, to in sorted(ranges):
        out += _uvarint(len(frm)) + frm + _uvarint(len(to)) + to
    return out


def _default_rule(
    *,
    exact: Sequence[str] = (),
    suffix: Sequence[str] = (),
    keywords: Sequence[str] = (),
    regexes: Sequence[str] = (),
    cidrs: Sequence[str] = (),
    networks: Sequence[str] = (),
    invert: bool = False,
) -> bytes:
    out = b"\x00"
    if networks:
        out += b"\x01" + _string_list(list(networks))
    if exact or suffix:
        out += b"\x02" + _domain_matcher(list(exact), list(suffix))
    if keywords:
        out += b"\x03" + _string_list(list(keywords))
    if regexes:
        out += b"\x04" + _string_list(list(regexes))
    if cidrs:
        out += b"\x06" + _ip_set(list(cidrs))
    return out + b"\xff" + bytes([invert])


def _logical_rule(mode: str, rules: list[bytes], invert=False) -> bytes:
    return (
        b"\x01"
        + (b"\x00" if mode == "and" else b"\x01")
        + _uvarint(len(rules))
        + b"".join(rules)
        + bytes([invert])
    )


def _srs_file(tmp_path, *rules: bytes, version: int = 3):
    body = _uvarint(len(rules)) + b"".join(rules)
    path = tmp_path / "test.srs"
    path.write_bytes(b"SRS" + bytes([version]) + zlib.compress(body))
    return path


# ---- domain matching ---------------------------------------------------------


def test_suffix_matches_root_and_subdomains(tmp_path):
    rs = srs.parse(_srs_file(tmp_path, _default_rule(suffix=["netflix.com"])))
    assert rs.match(domain="netflix.com")
    assert rs.match(domain="www.netflix.com")
    assert rs.match(domain="a.b.c.netflix.com")
    # dot boundary: no bleed into lookalike registrations
    assert not rs.match(domain="notnetflix.com")
    assert not rs.match(domain="netflix.com.evil.org")
    assert not rs.match(domain="netflix.org")


def test_dotted_suffix_excludes_the_root(tmp_path):
    rs = srs.parse(_srs_file(tmp_path, _default_rule(suffix=[".cdn.example"])))
    assert rs.match(domain="x.cdn.example")
    assert not rs.match(domain="cdn.example")


def test_exact_domain_matches_only_itself(tmp_path):
    rs = srs.parse(_srs_file(tmp_path, _default_rule(exact=["one.example.com"])))
    assert rs.match(domain="one.example.com")
    assert not rs.match(domain="sub.one.example.com")
    assert not rs.match(domain="example.com")


def test_mixed_exact_and_suffix_share_one_trie(tmp_path):
    rs = srs.parse(
        _srs_file(
            tmp_path,
            _default_rule(exact=["login.hbomax.com"], suffix=["hbo.com", "hbonow.com"]),
        )
    )
    assert rs.match(domain="hbo.com")
    assert rs.match(domain="play.hbo.com")
    assert rs.match(domain="login.hbomax.com")
    assert not rs.match(domain="cdn.hbomax.com")


def test_keyword_and_regex(tmp_path):
    rs = srs.parse(
        _srs_file(
            tmp_path,
            _default_rule(
                keywords=["nflx"], regexes=[r"(^|\.)cache\d+\.example\.com$"]
            ),
        )
    )
    assert rs.match(domain="ipv4.nflxvideo.net")
    assert rs.match(domain="cache12.example.com")
    assert rs.match(domain="eu.cache3.example.com")
    assert not rs.match(domain="cache.example.com")


def test_uncompilable_regex_disables_the_rule_and_is_disclosed(tmp_path):
    # Go RE2 accepts constructs Python re rejects; the rule must go
    # conservative (no match), not crash or silently pretend
    rs = srs.parse(_srs_file(tmp_path, _default_rule(regexes=["a(?P<n>b)(?P<n>c)"])))
    assert not rs.match(domain="abc.example")
    assert any(u.startswith("domain_regex:") for u in rs.unsupported)


# ---- IP matching -------------------------------------------------------------


def test_ip_ranges_both_families(tmp_path):
    rs = srs.parse(
        _srs_file(
            tmp_path,
            _default_rule(cidrs=["10.10.0.0/16", "192.0.2.8/32", "2001:db8::/32"]),
        )
    )
    assert srs.match_ip(rs, "10.10.255.255")
    assert srs.match_ip(rs, "192.0.2.8")
    assert srs.match_ip(rs, "2001:db8:ffff::1")
    assert not srs.match_ip(rs, "10.11.0.0")
    assert not srs.match_ip(rs, "192.0.2.9")
    assert not srs.match_ip(rs, "2001:db9::1")
    # a v4 address never falls into a v6 range's integer span
    assert not srs.match_ip(rs, "32.1.13.184")


def test_domain_and_ip_items_are_ored_within_a_rule(tmp_path):
    # sing-box treats them as one destination-address group (rule_abstract.go)
    rs = srs.parse(
        _srs_file(tmp_path, _default_rule(suffix=["example.com"], cidrs=["10.0.0.0/8"]))
    )
    assert rs.match(domain="example.com")
    assert rs.match(ips=[ipaddress.ip_address("10.1.2.3")])
    assert not rs.match(domain="other.org", ips=[ipaddress.ip_address("11.0.0.1")])


# ---- rule combinators --------------------------------------------------------


def test_multiple_rules_are_ored(tmp_path):
    rs = srs.parse(
        _srs_file(
            tmp_path,
            _default_rule(suffix=["a.example"]),
            _default_rule(cidrs=["198.51.100.0/24"]),
        )
    )
    assert rs.match(domain="a.example")
    assert srs.match_ip(rs, "198.51.100.7")
    assert not rs.match(domain="b.example")


def test_inverted_rule(tmp_path):
    rs = srs.parse(
        _srs_file(tmp_path, _default_rule(suffix=["a.example"], invert=True))
    )
    assert not rs.match(domain="a.example")
    assert rs.match(domain="b.example")


def test_logical_or_and_and(tmp_path):
    inner_a = _default_rule(suffix=["a.example"])
    inner_kw = _default_rule(keywords=["tracker"])
    rs_or = srs.parse(_srs_file(tmp_path, _logical_rule("or", [inner_a, inner_kw])))
    assert rs_or.match(domain="x.a.example")
    assert rs_or.match(domain="tracker.other.org")
    assert not rs_or.match(domain="clean.org")
    rs_and = srs.parse(_srs_file(tmp_path, _logical_rule("and", [inner_a, inner_kw])))
    assert rs_and.match(domain="tracker.a.example")
    assert not rs_and.match(domain="x.a.example")


# ---- constraints a destination cannot answer ---------------------------------


def test_unevaluable_dimension_goes_conservative(tmp_path):
    rs = srs.parse(
        _srs_file(tmp_path, _default_rule(suffix=["a.example"], networks=["udp"]))
    )
    assert not rs.match(domain="a.example")  # cannot confirm the network leg
    assert rs.unsupported == ["network"]


def test_empty_rule_matches_everything(tmp_path):
    rs = srs.parse(_srs_file(tmp_path, _default_rule()))
    assert rs.match(domain="anything.example")


# ---- malformed input ---------------------------------------------------------


def test_bad_magic_rejected(tmp_path):
    path = tmp_path / "bad.srs"
    path.write_bytes(b"NOPE" + zlib.compress(b"\x00"))
    with pytest.raises(srs.SrsError, match="bad magic"):
        srs.parse(path)


def test_corrupt_body_rejected(tmp_path):
    path = tmp_path / "corrupt.srs"
    path.write_bytes(b"SRS\x03not-zlib-at-all")
    with pytest.raises(srs.SrsError, match="corrupt"):
        srs.parse(path)


def test_truncated_body_rejected(tmp_path):
    rule = _default_rule(suffix=["a.example"])
    path = tmp_path / "trunc.srs"
    path.write_bytes(b"SRS\x03" + zlib.compress((_uvarint(1) + rule)[:-4]))
    with pytest.raises(srs.SrsError, match="truncated"):
        srs.parse(path)


def test_unknown_item_type_rejected_not_misread(tmp_path):
    rule = b"\x00" + bytes([0x30]) + b"\xff\x00"
    with pytest.raises(srs.SrsError, match="unsupported rule item"):
        srs.parse(_srs_file(tmp_path, rule))


def test_missing_file(tmp_path):
    with pytest.raises(srs.SrsError, match="cannot read"):
        srs.parse(tmp_path / "absent.srs")


# ---- the shared matching primitive -------------------------------------------


def test_match_destination_uses_parsed_geo(tmp_path):
    geo = {
        ("geosite", "netflix"): srs.parse(
            _srs_file(tmp_path, _default_rule(suffix=["netflix.com"]))
        )
    }
    rule = {"type": "geosite", "value": "netflix", "target": "direct"}
    assert routes.match_destination(rule, domain="www.netflix.com", geo=geo)
    assert not routes.match_destination(rule, domain="example.com", geo=geo)
    # a missing category matches nothing (the tracer discloses the gap)
    assert not routes.match_destination(
        {"type": "geoip", "value": "us", "target": "direct"},
        ips=[ipaddress.ip_address("8.8.8.8")],
        geo=geo,
    )


def test_match_destination_plain_types():
    assert routes.match_destination(
        {"type": "domain_suffix", "value": "a.com"}, domain="x.a.com"
    )
    assert not routes.match_destination(
        {"type": "domain_suffix", "value": "a.com"}, domain="xa.com"
    )
    assert routes.match_destination(
        {"type": "ip_cidr", "value": "10.0.0.0/8"},
        ips=[ipaddress.ip_address("10.9.9.9")],
    )
    assert not routes.match_destination(
        {"type": "ip_cidr", "value": "10.0.0.0/8"},
        ips=[ipaddress.ip_address("2001:db8::1")],
    )
    assert routes.match_destination({"type": "all", "value": ""})
