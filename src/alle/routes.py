"""Routing rule model: matcher validation, target parsing, and the shadow lint.

A rule is **one matcher plus a target**, stored in ``state.json`` under
``router.rules`` and compiled verbatim (in stored order) into sing-box
``route.rules`` — order is law, first match wins, and alle never reorders by
"specificity" (undefinable once richer matcher types arrive). The safety net
for the ordering footgun is the shadow lint here: a rule that can never match
because an earlier rule strictly covers it is flagged, not silently dead.

Matcher vocabulary (one per rule): ``domain_suffix`` — the one domain matcher,
matching the domain itself *and* its subdomains (dot-boundary, so
``example.co.uk`` never bleeds into ``otherexample.co.uk``) — ``ip_cidr``, and
``all`` (the catch-all that makes "VPN by default" a one-liner). There is
deliberately no exact-only domain type: one semantic keeps rules predictable,
and the legacy ``domain`` (exact) type is read as ``domain_suffix`` wherever
it can still appear (old state files, old bundles, explicit API type
overrides). Targets are extensible strings: ``<provider>/<channel_id>``,
``direct``, or ``block`` today; future forms (``group:…``) join without a
state migration.
"""

from __future__ import annotations

import ipaddress
import re

# Built-in default-direct destinations: private, link-local, loopback, and
# multicast/broadcast ranges. Compiled ahead of all user rules (when the
# ``lan_direct`` toggle is on) so LAN access — printers, NAS, router admin
# pages, mDNS/SSDP discovery — keeps working under a "route everything through
# VPN" catch-all. DNS is deliberately *not* here: sending plain DNS direct by
# default would leak browsing activity outside the tunnel, so it stays subject
# to user rules. mDNS/SSDP multicast is covered because their destinations
# (224.0.0.251 / ff02::fb, 239.255.255.250) fall inside the multicast CIDRs;
# their *unicast* legs ride the port list below.
LAN_DIRECT_CIDRS = (
    "10.0.0.0/8",  # IPv4 private # noqa: S1313
    "172.16.0.0/12",  # noqa: S1313
    "192.168.0.0/16",  # noqa: S1313
    "169.254.0.0/16",  # IPv4 link-local # noqa: S1313
    "127.0.0.0/8",  # IPv4 loopback
    "224.0.0.0/4",  # IPv4 multicast (mDNS/SSDP/LAN discovery) # noqa: S1313
    "255.255.255.255/32",  # IPv4 broadcast
    "::1/128",  # IPv6 loopback
    "fe80::/10",  # IPv6 link-local
    "fc00::/7",  # IPv6 unique local (ULA)
    "ff00::/8",  # IPv6 multicast
)

# The port half of the built-in LAN-direct block: LAN housekeeping protocols
# whose *unicast* legs the CIDRs above cannot see — a DHCP renewal goes
# unicast to the server, and mDNS/SSDP queriers answer unicast from a
# well-known port. UDP-only (that is what these protocols speak), and a fixed
# curated list on purpose: every direct-bypass port is a small tunnel-bypass
# channel, so this is not user-extensible — custom port routing belongs to
# user rules once the port matcher exists. Settled design (2026-07-18): one
# toggle, fixed contents, full transparency; if a real network ever needs the
# list changed and user rules cannot express it, the fallback is
# subtractive-only overrides (disable entries, never add). Port 53 is
# deliberately absent:
# plain DNS stays subject to the hijack/user rules (see LAN_DIRECT_CIDRS'
# comment on leaks). Rides the same ``lan_direct`` toggle as the CIDRs.
LAN_DIRECT_UDP_PORTS = (
    67,  # DHCP server (client -> server, incl. unicast renewals)
    68,  # DHCP client
    1900,  # SSDP / UPnP discovery
    5353,  # mDNS
)

# One DNS label: alnum (plus inner hyphens/underscores), max 63 chars.
_LABEL = r"(?!-)[a-z0-9_-]{1,63}(?<!-)"
# A routable domain needs at least one dot (≥2 labels): a bare single label like
# "xxx" is neither a usable domain nor an IP, so it is rejected rather than
# silently becoming a one-label suffix match.
_DOMAIN_RE = re.compile(rf"^{_LABEL}(\.{_LABEL})+$")


class RuleError(Exception):
    """A routing rule the user typed is not usable."""


def normalize_domain(value: str) -> str:
    v = value.strip().lower().rstrip(".")
    if v.startswith("*."):
        # domain matchers already cover subdomains — the wildcard is redundant
        raise RuleError(f"{value!r}: use {v[2:]} — domains always match subdomains")
    if not v or not _DOMAIN_RE.match(v):
        raise RuleError(f"{value!r} is not a valid domain name")
    return v


def normalize_value(matcher_type: str, value: str) -> str:
    """Canonicalize a matcher value (or raise :class:`RuleError`)."""
    if matcher_type == "all":
        return ""
    if matcher_type == "domain_suffix":
        return normalize_domain(value)
    if matcher_type == "ip_cidr":
        try:
            # strict=False forgives host bits (10.0.0.1/8 -> 10.0.0.0/8); a bare
            # IP canonicalizes to /32 (or /128), so "match this one address" works.
            return str(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError as e:
            raise RuleError(f"{value!r} is not a valid IP or CIDR block") from e
    raise RuleError(f"unknown matcher type {matcher_type!r}")


def infer_matcher(value: str, matcher_type: str | None = None) -> tuple[str, str]:
    """Infer and normalize a ruleset matcher from one user-facing entry.

    Without an explicit ``matcher_type``, CIDR/IP-looking entries become
    ``ip_cidr`` and everything else must be a domain — always
    ``domain_suffix``, matching the domain *and* its subdomains (the common
    intent, and the only domain semantic alle has). The legacy exact
    ``domain`` type is accepted as an alias and normalized to
    ``domain_suffix``, so old bundles and API clients keep working.
    """
    raw = value.strip()
    if matcher_type == "domain":  # legacy exact type — one domain matcher now
        matcher_type = "domain_suffix"
    if matcher_type:
        return matcher_type, normalize_value(matcher_type, raw)
    if raw.lower() == "all":
        return "all", ""
    try:
        return "ip_cidr", str(ipaddress.ip_network(raw, strict=False))
    except ValueError:
        pass
    return "domain_suffix", normalize_domain(raw)


def parse_target(target: str) -> tuple[str, tuple[str, str] | None]:
    """``("direct"|"block"|"channel", (provider, channel_id) | None)``."""
    t = target.strip()
    if t in ("direct", "block"):
        return t, None
    provider, _, cid = t.partition("/")
    if provider and cid and "/" not in cid:
        return "channel", (provider, cid)
    raise RuleError(
        f"target {target!r} is not valid — use <provider>/<channel>, "
        "'direct', or 'block'."
    )


def describe(rule: dict) -> str:
    """Human one-liner for a rule's matcher (``domain_suffix netflix.com``)."""
    if rule["type"] == "all":
        return "all traffic"
    return f"{rule['type']} {rule['value']}"


# ---- shadow lint -------------------------------------------------------------


def _subnet_of(inner, outer) -> bool:
    """True iff ``inner`` is a subnet of ``outer``, same address family only.

    Paired isinstance (not a version compare) so type checkers narrow the
    ``IPv4Network | IPv6Network`` union that ``subnet_of()`` won't accept
    mixed; mixed families never overlap.
    """
    if isinstance(inner, ipaddress.IPv4Network) and isinstance(
        outer, ipaddress.IPv4Network
    ):
        return inner.subnet_of(outer)
    if isinstance(inner, ipaddress.IPv6Network) and isinstance(
        outer, ipaddress.IPv6Network
    ):
        return inner.subnet_of(outer)
    return False


def covers(a: dict, b: dict) -> bool:
    """True if rule ``a`` matches a superset (or all) of what ``b`` matches —
    i.e. an ``a`` evaluated earlier makes ``b`` unreachable.

    Deliberately best-effort by type: only same-family containment is decided
    (domains vs domains, CIDRs vs CIDRs, ``all`` vs anything); pairs it cannot
    reason about are treated as non-overlapping, so future matcher types
    degrade the lint, never break it.
    """
    ta, tb = a["type"], b["type"]
    if ta == "all":
        return True
    if tb == "all":
        return False
    if ta == "domain_suffix" and tb == "domain_suffix":
        # dot-boundary suffix semantics: google.com covers google.com + *.google.com
        return b["value"] == a["value"] or b["value"].endswith("." + a["value"])
    if ta == "ip_cidr" and tb == "ip_cidr":
        return _subnet_of(
            ipaddress.ip_network(b["value"]), ipaddress.ip_network(a["value"])
        )
    return False


def shadowed_by(rules: list[dict]) -> dict[str, str]:
    """Map each unreachable rule's id to the earliest earlier rule covering it."""
    out: dict[str, str] = {}
    for i, rule in enumerate(rules):
        for earlier in rules[:i]:
            if covers(earlier, rule):
                out[rule["id"]] = earlier["id"]
                break
    return out


# The marker placed in a rule's ``shadowed_by`` when it is covered by the
# built-in priority-zero LAN-direct block (not a user rule id). Distinct from
# any real id (``r…``/``rs…``) so the renderer can name the built-in block.
LAN_DIRECT_SHADOW = "lan-direct"


def shadowed_by_lan_direct(rule: dict) -> bool:
    """True if an ``ip_cidr`` rule is wholly inside a built-in LAN-direct range.

    The LAN-direct block sits at priority zero (ahead of every user rule), so a
    user rule targeting a private/link-local/multicast range it covers can never
    match while ``lan_direct`` is on — its traffic already went direct. Domain
    rules are never affected (a domain never resolves into a fixed private
    range the lint could see). Returns False for anything but a covered CIDR.
    """
    if rule.get("type") != "ip_cidr":
        return False
    try:
        net = ipaddress.ip_network(rule["value"])
    except ValueError:
        return False
    return any(_subnet_of(net, ipaddress.ip_network(cidr)) for cidr in LAN_DIRECT_CIDRS)


def shadow_label(shadowed_by: str) -> str:
    """Human wording for a ``shadowed_by`` marker — names the built-in LAN-direct
    block specially, otherwise the covering user rule's id."""
    if shadowed_by == LAN_DIRECT_SHADOW:
        return "the built-in LAN-direct rule"
    return f"earlier rule {shadowed_by}"
