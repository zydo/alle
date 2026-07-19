"""Rule-match tracer: which routing rule wins for a destination, and why.

``trace(store, destination)`` walks the same logical rule table the engine
compiles into sing-box (``engine._router_config`` — order is law, first match
wins) and evaluates each rule with the one shared matching primitive,
:func:`alle.routes.match_destination`. geosite/geoip categories are answered
from the same cached, digest-verified ``.srs`` files the engine hands
sing-box, parsed by :mod:`alle.srs` — same data, same semantics, no running
sing-box required.

This is an offline evaluation, not a probe: nothing is sent through any
tunnel (``alle test`` does that). The only network I/O is the disclosed DNS
lookup for a domain destination — resolved against the same upstream the tun
DNS hijack uses (``constants.TUN_DNS_UPSTREAM``), by a minimal wire-format
query so the host's system resolver (possibly sinkholed by another VPN —
see :mod:`alle.probe`) never colors the answer. Tracing a literal IP skips
DNS entirely.

Fidelity notes, honest by design:

- The trace models the **IP-dialed** path (tun mode: apps resolve via the
  hijacked DNS, then dial the address; sniff recovers the domain for domain
  rules). A hostname-CONNECT proxy client whose connection stays a domain
  never has a destination IP at rule time, so ip_cidr/geoip rules cannot
  match it in the live config — the trace's IP matches are an upper bound
  for that path.
- A destination is traced as one flow. Its family follows alle's DNS
  strategy: IPv4 when any A answer exists (``prefer_ipv4``/``ipv4_only``),
  IPv6 only for v6-only destinations — and with no v6-capable channel the
  tun DNS suppresses AAAA entirely (``ipv4_only``), which the trace
  discloses rather than pretending a v6 flow could exist.
- Built-ins that key on dimensions a bare destination does not have (the
  UDP-port LAN rules, the protocol-sniffed DNS hijack) are shown in the
  walk as not-evaluable rather than silently skipped.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
from urllib.parse import urlsplit

from alle import geodata, routes, srs
from alle.constants import TUN_DNS_UPSTREAM
from alle.engine import channel_ipv6
from alle.state import Store

DNS_TIMEOUT = 4.0


class TraceError(Exception):
    """The destination could not be traced (unusable input)."""


# ---- minimal DNS client ------------------------------------------------------


def _dns_query(name: str, qtype: int) -> bytes:
    header = secrets.token_bytes(2) + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    qname = b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
    return header + qname + b"\x00" + qtype.to_bytes(2, "big") + b"\x00\x01"


def _skip_name(msg: bytes, i: int) -> int:
    while i < len(msg):
        length = msg[i]
        if length == 0:
            return i + 1
        if length & 0xC0 == 0xC0:  # compression pointer ends the name
            return i + 2
        i += 1 + length
    raise ValueError("truncated DNS name")


def _dns_answers(msg: bytes, query: bytes, qtype: int) -> list[str]:
    if len(msg) < 12 or msg[:2] != query[:2]:
        raise ValueError("mismatched DNS response")
    if msg[3] & 0x0F:  # rcode
        raise ValueError(f"DNS rcode {msg[3] & 0x0F}")
    qdcount = int.from_bytes(msg[4:6], "big")
    ancount = int.from_bytes(msg[6:8], "big")
    i = 12
    for _ in range(qdcount):
        i = _skip_name(msg, i) + 4
    out = []
    for _ in range(ancount):
        i = _skip_name(msg, i)
        rtype = int.from_bytes(msg[i : i + 2], "big")
        rdlen = int.from_bytes(msg[i + 8 : i + 10], "big")
        rdata = msg[i + 10 : i + 10 + rdlen]
        i += 10 + rdlen
        if rtype == qtype and len(rdata) in (4, 16):
            out.append(str(ipaddress.ip_address(rdata)))
    return out


def _resolve(domain: str) -> dict:
    """A/AAAA answers from the tun DNS upstream — the disclosed lookup.

    ``{"a": [...], "aaaa": [...], "error": str | None}``; a failure of one
    query type degrades to the other rather than failing the trace.
    """
    result: dict = {"a": [], "aaaa": [], "error": None}
    errors = []
    for key, qtype in (("a", 1), ("aaaa", 28)):
        query = _dns_query(domain, qtype)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(DNS_TIMEOUT)
                s.sendto(query, (TUN_DNS_UPSTREAM, 53))
                msg = s.recv(4096)
            result[key] = _dns_answers(msg, query, qtype)
        except (OSError, ValueError) as e:
            errors.append(f"{key.upper()}: {e}")
    if errors and not (result["a"] or result["aaaa"]):
        result["error"] = "; ".join(errors)
    return result


# ---- input parsing -----------------------------------------------------------


def parse_destination(value: str) -> tuple[str | None, str | None]:
    """``(domain, ip)`` from a user-typed destination (exactly one is set).

    Forgiving about pasted forms: the scheme may be omitted, and a path and/or
    port may follow the host — ``netflix.com/some/page.html``,
    ``192.168.1.1/admin/login.php``, ``example.org:8443``, a full
    ``https://host:port/path`` URL, a bracketed IPv6 ``[2001:db8::1]:443``, or a
    bare domain/IP. Each is reduced to its hostname. Raises
    :class:`TraceError` for anything unusable.
    """
    raw = (value or "").strip()
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    elif "/" in raw or raw.count(":") == 1 or (raw.startswith("[") and "]" in raw):
        # No scheme, but there is a path and/or a host:port to strip. A bare
        # IPv6 literal (≥2 colons, no path, no brackets) is left untouched so
        # ip_address below sees it whole rather than mangled by host:port split.
        host = urlsplit("//" + raw).hostname
        raw = host if host is not None else raw
    if not raw:
        raise TraceError(f"{value!r} contains no usable destination")
    try:
        return None, str(ipaddress.ip_address(raw))
    except ValueError:
        pass
    try:
        return routes.normalize_domain(raw), None
    except routes.RuleError as e:
        raise TraceError(str(e)) from e


# ---- the walk ----------------------------------------------------------------


def _load_geo(store: Store, rules: list[dict]) -> tuple[dict, dict[str, str]]:
    """Parsed rule-sets for every geo matcher in ``rules``, plus per-category
    problems (uncached, digest-failed, or unparseable)."""
    geo: dict = {}
    problems: dict[str, str] = {}
    for rule in rules:
        kind, name = rule.get("type"), str(rule.get("value"))
        if kind not in routes.GEO_TYPES or (kind, name) in geo:
            continue
        path = geodata.cached_path(store, kind, name)
        if path is None:
            problems[f"{kind}:{name}"] = "not cached (or failed its digest check)"
            continue
        try:
            geo[(kind, name)] = srs.parse(path)
        except srs.SrsError as e:
            problems[f"{kind}:{name}"] = f"unreadable rule-set: {e}"
    return geo, problems


def _which_ip(rule: dict, ips: list, geo: dict) -> str | None:
    """The specific address that made an ip-flavored rule match (for the
    human reason line)."""
    for ip in ips:
        if routes.match_destination(rule, ips=[ip], geo=geo):
            return str(ip)
    return None


def _segs(*parts) -> list[dict]:
    """Build structured reason segments — the single source for the reason line.

    Each part is either a plain ``str`` (normal text) or a ``(text, True)``
    tuple (emphasized — the matched matcher or the channel target). The joined
    text becomes the plain ``reason`` (CLI/JSON); the segment list becomes
    ``reason_parts`` (rendered with emphasis by clients that understand it, e.g.
    the Web UI). Bespoke built-in reasons pass a single string to ``finish``,
    which wraps it as one plain segment.
    """
    out: list[dict] = []
    for part in parts:
        if isinstance(part, tuple):
            text, bold = part
            out.append({"text": text, "bold": bool(bold)})
        else:
            out.append({"text": part})
    return out


def trace(store: Store, destination: str) -> dict:
    """Evaluate ``destination`` against the compiled rule order; first match
    wins. Returns the TraceResult dict (see ``docs/api.md``)."""
    domain, literal_ip = parse_destination(destination)
    router = store.router
    tun = bool(router.get("tun"))
    lan_direct = bool(router.get("lan_direct", True))
    killswitch = bool(router.get("killswitch"))

    built: set[tuple[str, str]] = set()
    v6_capable: set[tuple[str, str]] = set()
    for ch in store.channels():
        if not ch.enabled or not ch.wg or "peer" not in ch.wg:
            continue
        built.add((ch.provider, ch.id))
        if channel_ipv6(ch):
            v6_capable.add((ch.provider, ch.id))

    # -- destination IPs and the flow family --
    dns: dict | None = None
    all_ips: list[str] = []
    family: int | None
    if literal_ip is not None:
        flow_ips = [ipaddress.ip_address(literal_ip)]
        family = flow_ips[0].version
    else:
        dns = _resolve(domain)  # type: ignore[arg-type]  # domain is set here
        dns["upstream"] = TUN_DNS_UPSTREAM
        all_ips = list(dns["a"]) + list(dns["aaaa"])
        v6_suppressed = tun and not v6_capable and bool(dns["aaaa"])
        dns["aaaa_suppressed"] = v6_suppressed
        if dns["a"]:
            family = 4
            flow_ips = [ipaddress.ip_address(a) for a in dns["a"]]
        elif dns["aaaa"] and not v6_suppressed:
            family = 6
            flow_ips = [ipaddress.ip_address(a) for a in dns["aaaa"]]
        else:
            # unresolved (or v6-only with AAAA suppressed): domain rules
            # still answer; ip rules have nothing to match
            family = 6 if v6_suppressed else None
            flow_ips = []

    geo, geo_problems = _load_geo(store, store.rules())
    try:
        ruleset_names = {
            rule["id"]: block["name"]
            for block in store.rulesets()
            for rule in block["rules"]
        }
    except ValueError:
        # decoration only — a malformed grouping must not kill the trace
        ruleset_names = {}

    walked: list[dict] = []
    result: dict = {
        "input": destination,
        "domain": domain,
        "ip": literal_ip,
        "resolved_ips": all_ips,
        "flow_family": family,
        "dns": dns,
        "surface": {
            "tun": tun,
            "router_port": int(router.get("port") or 0),
            "lan_direct": lan_direct,
            "killswitch": killswitch,
        },
        "geo_problems": geo_problems,
        "walked": walked,
        "matched_rule": None,
        "verdict": None,
        "exit": None,
        "reason": None,
        # the reason as structured segments (see _segs); joined text mirrors
        # ``reason``. One source: clients render either form.
        "reason_parts": [],
    }

    def finish(
        entry: dict, verdict: str, exit_: str | None, reason: str | list[dict]
    ) -> dict:
        walked.append(entry)
        parts = reason if isinstance(reason, list) else [{"text": reason}]
        result["matched_rule"] = entry.get("rule_ref")
        result["verdict"] = verdict
        result["exit"] = exit_
        result["reason"] = "".join(p["text"] for p in parts)
        result["reason_parts"] = parts
        return result

    def skip(label: str, note: str) -> None:
        walked.append({"rule": label, "matched": False, "note": note})

    what = domain or literal_ip

    # 1. sniff — non-terminal, shown for fidelity with the compiled order
    # (matched: None — it inspects every connection but never routes one)
    walked.append(
        {
            "rule": "built-in: sniff",
            "matched": None,
            "note": "non-terminal action (protocol/domain sniffing) — "
            "routing continues",
        }
    )
    # 2. LAN-direct UDP ports
    if lan_direct:
        ports = ",".join(str(p) for p in routes.LAN_DIRECT_UDP_PORTS)
        skip(
            f"built-in: LAN-direct UDP ports ({ports})",
            "matches by destination port — a bare destination has none",
        )
    # 3. tun DNS hijack
    if tun:
        skip(
            "built-in: DNS hijack (tun)",
            "matches sniffed DNS traffic only",
        )
    # 4. LAN-direct CIDRs
    if lan_direct:
        lan_rule = {"type": "ip_cidr", "value": None}
        hit = None
        for cidr in routes.LAN_DIRECT_CIDRS:
            lan_rule["value"] = cidr
            if routes.match_destination(lan_rule, ips=flow_ips):
                hit = cidr
                break
        entry = {
            "rule": "built-in: LAN-direct CIDRs",
            "matched": hit is not None,
            "rule_ref": {"builtin": routes.LAN_DIRECT_SHADOW},
        }
        if hit:
            ip_hit = _which_ip({"type": "ip_cidr", "value": hit}, flow_ips, geo)
            return finish(
                entry | {"target": "direct"},
                "lan_direct",
                "direct",
                f"{ip_hit} is in the built-in LAN-direct range {hit} — "
                "goes direct, never through a tunnel",
            )
        walked.append(entry)
    # 5. blanket IPv6 reject (tun, no v6-capable channel)
    if tun and not v6_capable:
        entry = {
            "rule": "built-in: IPv6 reject (no IPv6-capable channel)",
            "matched": family == 6,
            "rule_ref": {"builtin": "reject-v6"},
        }
        if family == 6:
            return finish(
                entry,
                "reject_v6",
                None,
                f"{what} is an IPv6 destination and no enabled channel "
                "carries IPv6 — blocked instead of leaking outside the VPN",
            )
        walked.append(entry)
    # 6. user rules, in stored order
    for rule in store.rules():
        rid = rule["id"]
        token = routes.matcher_token(rule)
        label = f"{rid} {token}"
        ref = {
            "id": rid,
            "type": rule["type"],
            "value": rule.get("value"),
            "target": rule.get("target"),
            "matcher": token,
            "ruleset": ruleset_names.get(rid),
        }
        if rule["type"] in routes.GEO_TYPES:
            problem = geo_problems.get(f"{rule['type']}:{rule['value']}")
            if problem:
                # the engine compiles this to a matcher-less reject: sing-box
                # blocks ALL router/tun traffic until the data is fetched
                return finish(
                    {"rule": label, "matched": True, "rule_ref": ref},
                    "block",
                    None,
                    _segs(
                        (token, True),
                        f" is {problem} — the engine compiles this rule to "
                        "an unscoped reject, so all routed traffic is blocked "
                        "until it is fixed: alle routes geo refresh",
                    ),
                )
        matched = routes.match_destination(rule, domain=domain, ips=flow_ips, geo=geo)
        if not matched:
            walked.append({"rule": label, "matched": False})
            continue
        target = str(rule.get("target", ""))
        kind, chan = routes.parse_target(target)
        # what precisely matched, for the reason line
        if rule["type"] in ("ip_cidr", "geoip"):
            cause = _which_ip(rule, flow_ips, geo) or what
        else:
            cause = domain or literal_ip
        # a geoip match is decided by the destination's resolved IP — name the
        # domain that resolved to it inline, so the verdict is self-justifying.
        # (Only geoip: domain/ip_suffix rules match the domain directly, so the
        # IP resolution is not part of why they matched.)
        if rule["type"] == "geoip" and domain and cause:
            matched_prefix = _segs(
                (token, True), f" matched {cause} ", f"({domain} resolved to)"
            )
        else:
            matched_prefix = _segs((token, True), f" matched {cause}")
        if kind in ("direct", "block"):
            return finish(
                {"rule": label, "matched": True, "target": target, "rule_ref": ref},
                kind,
                "direct" if kind == "direct" else None,
                matched_prefix
                + _segs(
                    " — traffic goes "
                    + (
                        "direct (outside any tunnel)"
                        if kind == "direct"
                        else "nowhere (blocked)"
                    )
                ),
            )
        # parse_target guarantees a (provider, id) tuple for a channel target;
        # a None here means the stored target is malformed — fail closed.
        if chan is None or chan not in built:
            return finish(
                {"rule": label, "matched": True, "target": target, "rule_ref": ref},
                "block",
                None,
                matched_prefix
                + _segs(
                    ", but channel ",
                    (target, True),
                    " is disabled or unusable — matching traffic is blocked "
                    "(fail closed), not leaked outside the tunnel",
                ),
            )
        if tun and v6_capable and chan not in v6_capable and family == 6:
            # the engine compiles a same-matcher IPv6 guard ahead of this rule
            return finish(
                {
                    "rule": f"built-in: IPv6 guard for {rid}",
                    "matched": True,
                    "rule_ref": ref,
                },
                "reject_v6",
                None,
                matched_prefix
                + _segs(
                    ", but ",
                    (target, True),
                    " is IPv4-only and this is an IPv6 flow — blocked by the "
                    "rule's IPv6 guard instead of leaking or breaking the tunnel",
                ),
            )
        return finish(
            {"rule": label, "matched": True, "target": target, "rule_ref": ref},
            "channel",
            target,
            matched_prefix + _segs(" — traffic exits through ", (target, True)),
        )
    # 7. trailing catch-all IPv6 reject (tun, v6-capable fleet)
    if tun and v6_capable:
        entry = {
            "rule": "built-in: unmatched IPv6 reject",
            "matched": family == 6,
            "rule_ref": {"builtin": "reject-v6"},
        }
        if family == 6:
            return finish(
                entry,
                "reject_v6",
                None,
                f"{what} matched no rule and is IPv6 — unmatched IPv6 is "
                "blocked rather than leaked outside the VPN",
            )
        walked.append(entry)
    # 8. kill-switch
    if killswitch:
        return finish(
            {
                "rule": "built-in: kill-switch",
                "matched": True,
                "rule_ref": {"builtin": "killswitch"},
            },
            "killswitch",
            None,
            f"{what} matched no rule and the kill-switch is on — blocked "
            "instead of going direct",
        )
    # 9. route.final: direct
    result["verdict"] = "direct"
    result["exit"] = "direct"
    parts = _segs(
        f"{what} matched no rule — unmatched traffic goes direct (kill-switch is off)"
    )
    result["reason"] = parts[0]["text"]
    result["reason_parts"] = parts
    return result
