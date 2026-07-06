"""Presentation helpers for the alle CLI.

The service layer returns structured data; this module turns it into stable human
text or JSON for the command-line adapter.
"""

from __future__ import annotations

import json
import time
from typing import Any


def json_text(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if hasattr(value, "__dict__"):
        return value.__dict__
    return str(value)


def _bytes(n: int) -> str:
    """Human-readable byte size (1536 -> '1.5 KB'); base-1024 units."""
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _mbps(bps: float | None) -> str:
    """Bits-per-second to a human 'NN.N Mbps' (or '-' when the test failed)."""
    if not bps:
        return "-"
    return f"{bps / 1e6:.1f} Mbps"


def _ago(epoch: int) -> str:
    if not epoch:
        return "-"
    secs = max(0, int(time.time()) - int(epoch))
    if secs < 60:
        return f"{secs}s ago"
    minutes = secs // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _display(channel: dict) -> str:
    """The channel's shown name: its label, or the id when no label is set."""
    return channel.get("label") or channel["name"]


# The base columns every channel table leads with, in one place so the layout
# stays identical across `channels ls`, `status`, `test`, and `metrics`. Each of
# those extends this with its own trailing columns (state, latency, traffic, …).
# ID is the globally-unique, provider-qualified handle (``nordvpn/canada_1``) —
# the same ref commands accept — so it needs no separate provider column.
BASE_HEADERS = ["LABEL", "ID", "PORT", "COUNTRY", "CITY"]


def _channel_ref(c: dict) -> str:
    """A channel's globally-unique ref, ``<provider>/<id>`` (e.g. ``nordvpn/us_1``)."""
    return f"{c['provider']}/{c['name']}"


def _base_cells(c: dict) -> list[str]:
    """The base-column cells for one channel row, matching ``BASE_HEADERS``.

    Reads the fields every channel dict from ``alle.service`` carries: label,
    id (``name``) + provider (combined into the ref), port (pre-formatted
    ``:<n>``), and display country/city.
    """
    return [_display(c), _channel_ref(c), c["port"], c["country"], c["city"]]


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Render a left-aligned table as ``[header, dash-separator, *rows]``.

    Column widths fit the widest cell (or the header); columns are joined by two
    spaces, and the separator underlines each column with dashes of its width —
    the ``header`` / ``------`` / ``rows`` layout shared by every alle table.
    """
    n = len(headers)
    widths = [len(headers[i]) for i in range(n)]
    for r in rows:
        for i in range(n):
            widths[i] = max(widths[i], len(str(r[i])))

    def line(cells) -> str:
        return "  ".join(str(cells[i]).ljust(widths[i]) for i in range(n)).rstrip()

    return [line(headers), "  ".join("-" * w for w in widths), *(line(r) for r in rows)]


def providers_list(data: dict) -> str:
    providers = data["providers"]
    if not providers:
        return "No providers added yet. Add one:  alle providers add nordvpn"
    headers = ["PROVIDER", "TYPE", "DETAIL"]
    rows = []
    for p in providers:
        if (
            p.get("kind") == "config"
        ):  # portal .conf providers: show how many are imported
            n = p.get("channel_count", 0)
            detail = f"{n} .conf file" + ("" if n == 1 else "s")
            kind = "config"
        else:  # token/API providers: show the masked credential
            detail = p.get("credential", "") or "(no credential)"
            kind = "token"
        rows.append([p["display_name"], kind, detail])
    return "\n".join(_table(headers, rows))


def channels_list(data: dict) -> str:
    if not data["providers"]:
        return "No providers added yet. Add one:  alle providers add nordvpn"
    channels = data["channels"]
    if not channels:
        return 'No channels yet. Add one:  alle channels add nordvpn --country "United States"'
    return "\n".join(_table(BASE_HEADERS, [_base_cells(c) for c in channels]))


def locations(data: dict) -> str:
    if not data.get("available"):
        return "\n".join(
            [
                f"{data['display_name']}: locations are not listed here.",
                f"  {data['help']}",
            ]
        )

    provider = data["provider"]
    if "country" in data:
        if not data.get("matched", True):
            return (
                f"{data['country']!r} is not a {data['display_name']} country. "
                f"See the full list: alle locations {provider}"
            )
        lines = [f"{provider} cities in {data['country']} ({len(data['cities'])}):"]
        lines.extend(f"  {city}" for city in data["cities"])
        return "\n".join(lines)

    lines = [
        f"{provider}: {data['country_count']} countries, {data['city_count']} cities"
    ]
    for item in data["countries"]:
        cities = item["cities"]
        lines.append(f"  {item['country']}" + (f"  ({len(cities)})" if cities else ""))
        lines.extend(f"      {city}" for city in cities)
    return "\n".join(lines)


def _router_where(router: dict) -> str:
    port = router.get("port")
    return f"127.0.0.1:{port}" if port else "(port assigned on next daemon start)"


def _router_mode(router: dict) -> str:
    """One phrase for the entrypoint's behavior — must never let a user believe
    they are behind a VPN when the router is passing traffic through."""
    n = router.get("rule_count", 0)
    if not n and not router.get("killswitch"):
        return "pass-through (no rules)"
    mode = f"{n} rule(s), unmatched → {router['unmatched']}"
    if router.get("killswitch"):
        mode += " — kill-switch ON"
    return mode


def routes_list(data: dict) -> str:
    router = data["router"]
    head = f"Router entrypoint {_router_where(router)} — {_router_mode(router)}"
    rules = data["rules"]
    if not rules:
        if data.get("filter"):
            return f"No rules targeting {data['filter']!r}. See: alle routes ls"
        return "\n".join(
            [
                head,
                "No routing rules — the entrypoint passes everything through "
                "without a VPN.",
                "Add one:  alle routes add <provider>/<channel> --domain-suffix example.com",
            ]
        )
    headers = ["ID", "MATCH", "TARGET", "NOTE"]
    rows = [
        [
            r["id"],
            r["match"],
            r["target"],
            f"shadowed by {r['shadowed_by']} — never matches"
            if r.get("shadowed_by")
            else "",
        ]
        for r in rules
    ]
    return "\n".join([head, *_table(headers, rows)])


def metrics(data: dict) -> str:
    channels = data["channels"]
    if not channels:
        if data.get("filter"):
            return f"No channel named {data['filter']!r}. See: alle channels ls"
        return "No channels configured. Add one:  alle channels add nordvpn --country …"

    headers = [*BASE_HEADERS, "SENT", "RECV", "TOTAL", "SEEN"]
    rows = [
        [
            *_base_cells(c),
            _bytes(c["sent"]),
            _bytes(c["received"]),
            _bytes(c["total"]),
            _ago(c["updated_at"]),
        ]
        for c in channels
    ]
    return "\n".join(_table(headers, rows))


def _latency(ms: float | int | None) -> str:
    return f"{ms}ms" if ms is not None else "-"


def _state_cell(c: dict) -> str:
    """Health with the failure reason folded in, mirroring ``alle status``:
    ``Healthy`` when the probe succeeded, ``Stopped`` when the runtime is down,
    otherwise the probe's error reason (capitalized), falling back to ``Failed``."""
    if c["healthy"]:
        return "Healthy"
    err = c.get("error") or ""
    if err == "stopped":
        return "Stopped"
    return (err[:1].upper() + err[1:]) if err else "Failed"


def test_result(data: dict) -> str:
    channels = data["channels"]
    if not channels:
        if data.get("filter"):
            return f"No channel named {data['filter']!r}. See: alle channels ls"
        return "No channels configured. Add one:  alle channels add nordvpn --country …"

    if data.get("speed"):
        headers = [*BASE_HEADERS, "STATE", "LATENCY", "IP", "DOWNLOAD", "UPLOAD"]
        rows = []
        for c in channels:
            speed = c.get("speed_result") or {}
            rows.append(
                [
                    *_base_cells(c),
                    _state_cell(c),
                    _latency(c.get("latency_ms")),
                    c.get("ip") or "-",
                    _mbps(speed.get("download_bps")),
                    _mbps(speed.get("upload_bps")),
                ]
            )
        return "\n".join(_table(headers, rows))

    headers = [*BASE_HEADERS, "STATE", "LATENCY", "IP"]
    rows = [
        [
            *_base_cells(c),
            _state_cell(c),
            _latency(c.get("latency_ms")),
            c.get("ip") or "-",
        ]
        for c in channels
    ]
    return "\n".join(_table(headers, rows))


def daemon_status(data: dict) -> str:
    svc = data["service"]
    d = data["daemon"]
    lines = []
    if not svc.get("supported"):
        lines.append(
            f"Login service: not supported on {svc.get('platform', 'this platform')}."
        )
    elif svc["installed"]:
        state = "active" if svc["active"] else "installed (not active)"
        lines.append(f"Login service: {state} ({svc['manager']}).")
        lines.append(f"  Unit: {svc['unit_path']}")
    else:
        lines.append(f"Login service: not installed ({svc['manager']}).")
        lines.append("  Install with: alle daemon install")

    if d["running"]:
        ver = d["version"] or "unknown"
        lines.append(f"Daemon: running, version {ver}.")
        if d["skew"]:
            lines.append(
                f"  ⚠ CLI is {d['cli_version']} — run `alle restart` to match."
            )
    else:
        lines.append("Daemon: not running.")
    return "\n".join(lines)


def _skew_lines(snapshot: dict) -> list[str]:
    """A one-line skew warning when the running daemon is on older code than the
    CLI — an upgrade landed but the old daemon is still serving until restarted."""
    d = snapshot.get("daemon") or {}
    if d.get("skew"):
        return [
            f"  ⚠ daemon running {d['version']}, CLI is {d['cli_version']} — "
            "run `alle restart` to pick up the upgrade"
        ]
    return []


def status(snapshot: dict) -> str:
    channels = snapshot["channels"]
    if not snapshot["running"]:
        lines = ["Alle - Inactive", *_skew_lines(snapshot)]
        if channels:
            lines.append(
                f"  ({snapshot['channel_count']} channel(s) across "
                f"{snapshot['provider_count']} provider(s) configured; run `alle start`)"
            )
        return "\n".join(lines)

    lines = ["Alle - Active", *_skew_lines(snapshot)]
    router = snapshot.get("router")
    if router:
        lines.append(f"  Router  {_router_where(router)} — {_router_mode(router)}")
    if not channels:
        lines.append(
            '  (no channels yet — add one: alle channels add nordvpn --country "United States")'
        )
        return "\n".join(lines)

    headers = [*BASE_HEADERS, "STATE", "AGO", "LATENCY", "IP"]
    rows = []
    for channel in channels:
        latency = (
            f"{channel['latency_ms']}ms" if channel["latency_ms"] is not None else "-"
        )
        probe = channel.get("probe") or {}
        rows.append(
            [
                *_base_cells(channel),
                channel["state"],
                _ago(probe.get("at", 0)) if probe else "-",
                latency,
                channel["ip"] or "-",
            ]
        )
    return "\n".join(lines + _table(headers, rows))
