"""Heartbeat probe: requests through a channel's local proxy that yield both
the exit IP and the round-trip latency, trying several IP-echo sources in order.

WireGuard is connectionless, so a channel is only "active" if traffic actually
flows through it right now. We learn that — plus the public exit IP and a latency
number — from a tiny HTTPS GET routed through the channel's ``mixed`` inbound.

The probe does not depend on a single echo service: it tries a short list of
sources and stops at the first that returns a valid *public* IPv4/IPv6 address.
That keeps one flaky or retired endpoint from masking a healthy tunnel (and from
losing the exit IP entirely).

Why the primary is addressed by **literal IP (``1.1.1.1``), never a hostname**:
if the host runs a system-wide TUN/FakeIP VPN (another sing-box, clash, …), its
DNS resolves public hostnames to sinkhole addresses (e.g. ``198.18.0.0/15``),
which our sing-box would then route into the tunnel and fail to reach. Hitting a
literal IP needs no DNS at all, so the first attempt reflects the tunnel's real
health regardless of what owns the system resolver. (Cloudflare's TLS cert lists
``1.1.1.1`` as an IP SAN, so certificate verification still passes.) The hostname
fallbacks are only reached when that first attempt fails; their names are carried
to the proxy via HTTP ``CONNECT`` and resolved by sing-box *through* the tunnel,
not by the host, so the host's sinkholed resolver is bypassed there too.
"""

from __future__ import annotations

import ipaddress
import time
import urllib.request

# IP-echo sources, tried in order. The first to return a valid public IP wins.
#
#   - cloudflare-trace: multi-line body, one line is ``ip=<exit ip>``.
#   - the rest: plain-text body that is just the IP (optionally with a newline).
#
# Kept deliberately short — enough to avoid a single point of failure without
# multiplying latency on the hot heartbeat path.
IP_ECHO_SOURCES: list[tuple[str, str]] = [
    ("cloudflare-trace", "https://1.1.1.1/cdn-cgi/trace"),  # noqa: S1313
    ("icanhazip", "https://icanhazip.com"),
    ("ipify", "https://api.ipify.org"),
]

# IPv6-only echo sources, used for the supplementary v6-exit lookup on
# channels that carry IPv6. The hostnames resolve AAAA-only (or the literal
# is v6), so the connection is forced over v6 through the tunnel — a v4-happy
# echo can't answer by accident. Same ordering/first-win rules as above.
IPV6_ECHO_SOURCES: list[tuple[str, str]] = [
    ("icanhazip-v6", "https://ipv6.icanhazip.com"),
    ("ipify-v6", "https://api6.ipify.org"),
]

_USER_AGENT = "alle-probe/1"

# An IP-echo body is a few dozen bytes (cloudflare-trace ~300). Reading more
# than this means the endpoint is not an IP echo; never slurp an unbounded
# body on the heartbeat path.
MAX_BODY_BYTES = 8192

# Overall wall-clock budget for one channel: every source it tries, and the
# supplementary IPv6-exit lookup after them. Without it, a dead channel costs
# (sources × per-request timeout) — multiplied by the channel count, enough to
# stall a whole probe pass for minutes. It is the number `Engine.probe_all`'s
# `ceil(n / pool) × deadline` reasoning rests on, so nothing a channel does may
# fall outside it.
CHANNEL_DEADLINE = 15.0


def proxy_opener(port: int):
    """A urllib opener that routes everything through the local proxy on
    ``port`` — the one way alle drives traffic through a channel (shared with
    the speed test in :mod:`alle.throughput`)."""
    proxy = f"http://127.0.0.1:{port}"
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    return urllib.request.build_opener(handler)


def _parse_trace_ip(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("ip="):
            return line[3:].strip() or None
    return None


def _valid_public_ip(text: str | None) -> str | None:
    """Return the trimmed text iff it is a single valid *public* IPv4/IPv6
    address, else ``None``.

    Trims surrounding whitespace/newlines first, then requires the remainder to
    parse as one address that is globally routable — so empty bodies, HTML/error
    pages, multi-token text, and private/loopback/FakeIP ranges are all rejected.
    """
    if not text:
        return None
    candidate = text.strip()
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate if addr.is_global else None


def _extract_ip(source: str, body: str) -> str | None:
    if source == "cloudflare-trace":
        return _valid_public_ip(_parse_trace_ip(body))
    return _valid_public_ip(body)


def probe_ipv6(
    port: int, timeout: float = 6, budget: float = CHANNEL_DEADLINE
) -> str | None:
    """The channel's IPv6 exit address, or ``None``.

    A supplementary lookup for v6-capable channels only — never on the
    critical path: the channel's health/latency verdict comes from
    :func:`probe_channel`, and a v6-echo failure only means "no v6 exit to
    show" (the server may still be v4-happy). One source failing falls
    through to the next; anything non-v6 or non-public is rejected.

    ``budget`` is what remains of the *channel's* overall deadline, and it is
    the whole point of the parameter: this lookup runs after a probe that has
    already spent part of that deadline, so it caps every open by the
    remainder and returns immediately when nothing is left. Bounded only
    per-source, a healthy channel could spend ``sources × timeout`` beyond its
    own deadline — 12 seconds on top of 15 — and hold a probe-pool worker for
    all of it.
    """
    started = time.monotonic()
    opener = proxy_opener(port)
    for _name, url in IPV6_ECHO_SOURCES:
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            break
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
        try:
            with opener.open(  # noqa: S310 (loopback proxy)
                req, timeout=min(timeout, remaining)
            ) as r:
                body = r.read(MAX_BODY_BYTES).decode(errors="replace")
        except Exception:  # noqa: BLE001, S112
            continue
        ip = _valid_public_ip(body)
        if ip and ":" in ip:
            return ip
    return None


def probe_channel(
    port: int,
    timeout: float = 10,
    deadline: float = CHANNEL_DEADLINE,
    ipv6: bool = False,
) -> dict:
    """Probe one channel's proxy by querying the IP-echo sources in order.

    Returns a status dict suitable for ``state.json``::

        {"ok": bool, "at": epoch, "latency_ms": float|None,
         "ip": str|None, "error": str|None, "detail": str|None}

    ``ipv6`` adds the supplementary v6-exit lookup for a v6-capable channel
    (an extra ``ipv6`` key). It lives here, behind the same call, because
    ``deadline`` bounds *the channel* — one budget, measured by one clock. Split
    across two calls it was two independent budgets, and the caller's own clock
    reading decided how much was left; a healthy v4 result could then spend
    ``v6 sources × timeout`` beyond the deadline it was supposed to obey.

    The first source to return a valid public IP wins (its round trip is the
    reported latency); a source is treated as failed when the request raises,
    times out, or yields an empty / non-IP / non-public / oversized body.

    Failures collapse to a few short ``error`` categories (``proxy closed``,
    ``timeout``, ``no valid IP``) for table/status cells; the verbose
    explanation — which sources were tried and the last exception — is in
    ``detail`` for the log. The categories map to the two real failure modes:
    ``proxy closed`` means the channel's local SOCKS port refused the
    connection (sing-box didn't start the inbound — e.g. the channel failed to
    build), while ``timeout`` means traffic entered the tunnel but nothing came
    back (an unreachable server *or* a bad WireGuard key — the handshake is
    silent, so the two are indistinguishable here).
    """
    opener = proxy_opener(port)
    attempted: list[str] = []
    mode: str | None = None  # "closed" | "timeout" | "no_ip" — the dominant cause
    last_reason = "no valid IP"
    started = time.monotonic()
    for name, url in IP_ECHO_SOURCES:
        remaining = deadline - (time.monotonic() - started)
        if remaining <= 0:
            last_reason = f"channel deadline ({deadline:g}s) exhausted"
            break
        attempted.append(name)
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
        start = time.monotonic()
        try:
            with opener.open(  # noqa: S310 (loopback proxy)
                req, timeout=min(timeout, remaining)
            ) as r:
                body = r.read(MAX_BODY_BYTES).decode(errors="replace")
        except Exception as e:  # noqa: BLE001 — any failure means "try the next source"
            last_reason = _verbose_reason(e)
            cls = _classify(e)
            if cls == "closed":
                mode = "closed"  # definitive: the inbound isn't listening
            elif cls == "timeout":
                mode = mode or "timeout"
            continue
        ip = _extract_ip(name, body)
        if ip:
            latency = (time.monotonic() - start) * 1000
            healthy = {
                "ok": True,
                "at": int(time.time()),
                "latency_ms": round(latency, 1),
                "ip": ip,
                "error": None,
                "detail": None,
            }
            if ipv6:
                # Whatever the verdict cost comes out of the same budget: the
                # lookup is capped by the remainder and skipped when the
                # deadline is already spent. A missing v6 exit is informational
                # and never demotes this healthy result.
                healthy["ipv6"] = probe_ipv6(
                    port, budget=deadline - (time.monotonic() - started)
                )
            return healthy
        mode = mode or "no_ip"  # got a response, but not a valid public IP
        last_reason = "no valid IP"
    if mode is None:
        # Only reachable when the deadline fired before a classifiable failure;
        # that is itself a timeout through the tunnel.
        mode = "timeout"
    return {
        "ok": False,
        "at": int(time.time()),
        "latency_ms": None,
        "ip": None,
        "error": _ERROR_LABEL[mode],
        "detail": f"all IP sources failed ({', '.join(attempted)}); last: {last_reason}",
    }


_ERROR_LABEL = {
    "closed": "proxy closed",
    "timeout": "timeout",
    "no_ip": "no valid IP",
}

# Short table/status labels for a probe result. Kept here (next to the
# categories) so every surface — `alle test`, `alle status`, the Web UI —
# renders the same word for the same failure.
_STATE_LABEL = {
    "proxy closed": "Proxy closed",
    "timeout": "Timeout",
    "no valid IP": "No valid IP",
    "stopped": "Stopped",
}


def state_label(probe: dict | None) -> str:
    """One short word for a probe result, for tables and status lines:

    ``Healthy`` / ``Stopped`` / ``Timeout`` / ``Proxy closed`` / ``No valid IP``
    / ``Failed``. ``None`` (no probe yet) → ``Pending``."""
    if not probe:
        return "Pending"
    if probe.get("ok"):
        return "Healthy"
    return _STATE_LABEL.get((probe.get("error") or "failed").lower(), "Failed")


def _classify(e: Exception) -> str:
    """Bucket a probe exception: ``closed`` (local SOCKS port refused/reset),
    ``timeout`` (connected but no timely response through the tunnel), or
    ``other``. ``URLError`` carries the real cause in ``.reason``."""
    reason = getattr(e, "reason", e)
    text = f"{type(reason).__name__} {reason}".lower()
    if isinstance(reason, ConnectionError) or "refused" in text or "reset" in text:
        return "closed"
    if isinstance(reason, TimeoutError) or "timeout" in text or "timed out" in text:
        return "timeout"
    return "other"


def _verbose_reason(e: Exception) -> str:
    """The verbose per-attempt reason carried in ``detail`` for the log."""
    name = type(e).__name__
    if "timed out" in str(e).lower() or name in ("timeout", "TimeoutError"):
        return "timeout"
    reason = getattr(e, "reason", None)
    return f"{name}: {reason}" if reason else name
