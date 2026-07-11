"""Routing rule model: matcher validation, target parsing, and the shadow lint.

A rule is **one matcher plus a target**, stored in ``state.json`` under
``router.rules`` and compiled verbatim (in stored order) into sing-box
``route.rules`` — order is law, first match wins, and alle never reorders by
"specificity" (undefinable once richer matcher types arrive). The safety net
for the ordering footgun is the shadow lint here: a rule that can never match
because an earlier rule strictly covers it is flagged, not silently dead.

MVP matcher vocabulary (one per rule): ``domain`` (exact), ``domain_suffix``
(the domain and its subdomains, dot-boundary), ``ip_cidr``, and ``all`` (the
catch-all that makes "VPN by default" a one-liner). Targets are extensible
strings: ``<provider>/<channel_id>``, ``direct``, or ``block`` today; future
forms (``group:…``) join without a state migration.
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
# to user rules. mDNS is covered only because its multicast destinations
# (224.0.0.251 / ff02::fb) fall inside the multicast CIDRs.
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
        raise RuleError(
            f"{value!r}: use --domain-suffix {v[2:]} instead of a '*.' wildcard"
        )
    if not v or not _DOMAIN_RE.match(v):
        raise RuleError(f"{value!r} is not a valid domain name")
    return v


def normalize_value(matcher_type: str, value: str) -> str:
    """Canonicalize a matcher value (or raise :class:`RuleError`)."""
    if matcher_type == "all":
        return ""
    if matcher_type in ("domain", "domain_suffix"):
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

    Explicit ``matcher_type`` remains available for advanced overrides. Without
    it, CIDR/IP-looking entries become ``ip_cidr``; every domain defaults to
    ``domain_suffix`` — "netflix.com" routes the domain *and* its subdomains,
    the common intent. (The old two-label heuristic made a registrable domain
    like ``example.co.uk`` — three labels — an *exact* match, so its subdomains
    silently bypassed the rule.) Pass an explicit ``domain`` type for the rare
    host-only case where suffix matching would be too broad.
    """
    raw = value.strip()
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
    if ta == "domain":
        return tb == "domain" and a["value"] == b["value"]
    if ta == "domain_suffix" and tb in ("domain", "domain_suffix"):
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
