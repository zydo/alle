"""Presentation helpers for the alle CLI.

The service layer returns structured data; this module turns it into stable human
text or JSON for the command-line adapter.
"""

from __future__ import annotations

import json
import re
from typing import Any

from alle import routes

# Escape/control-sequence hygiene for anything echoed to a terminal. Labels,
# cities, filenames, and provider diagnostics are user- or network-supplied:
# rendered raw, an embedded CSI/OSC sequence could clear the screen, move the
# cursor over earlier output, or retitle the window. Strip well-formed ANSI
# sequences first, then every remaining C0 (except tab) and C1 control byte.
_ANSI_SEQ = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI:  ESC [ params final
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC:  ESC ] ... BEL/ST
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
    r"|\x1b."  # any other two-byte escape
)


def sanitize_text(value: Any) -> str:
    """``value`` as text with ANSI sequences and control characters removed."""
    text = _ANSI_SEQ.sub("", str(value))
    return "".join(
        ch for ch in text if (ch >= " " or ch == "\t") and not ("\x7f" <= ch <= "\x9f")
    )


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


def _display(channel: dict) -> str:
    """The channel's shown name: its label, or the id when no label is set."""
    return channel.get("label") or channel["name"]


# The base columns every channel table leads with, in one place so the layout
# stays identical across `channels ls` and `test` (the one detail table). Each
# extends this with its own trailing columns (state, latency, traffic, …).
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
    # Sanitize once, up front: widths must be measured on what is printed, or
    # a stripped escape sequence would leave its column misaligned.
    rows = [[sanitize_text(r[i]) for i in range(n)] for r in rows]
    widths = [len(headers[i]) for i in range(n)]
    for r in rows:
        for i in range(n):
            widths[i] = max(widths[i], len(r[i]))

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


def provider_token_refresh(channels: dict) -> list[str]:
    """Indented lines summarizing the channel re-resolve after a token replace:
    which channels were re-resolved with the new token, and which couldn't be
    (kept their old server, and will refresh on the next reconnect)."""
    resolved = channels.get("resolved") or []
    failed = channels.get("failed") or []
    lines = []
    if resolved:
        lines.append(f"  Re-resolved {len(resolved)} channel(s): {', '.join(resolved)}")
    if failed:
        lines.append(
            f"  {len(failed)} channel(s) couldn't be re-resolved "
            f"(kept the old server, will retry on reconnect): {', '.join(failed)}"
        )
    if not resolved and not failed:
        lines.append("  No channels to re-resolve.")
    return lines


def channels_list(data: dict) -> str:
    if not data["providers"]:
        return "No providers added yet. Add one:  alle providers add nordvpn"
    channels = data["channels"]
    if not channels:
        return 'No channels yet. Add one:  alle channels add nordvpn --country "United States"'
    # STATUS is administrative intent (enabled/disabled), not probe liveness —
    # this table stays static config, independent of whether alle is running.
    return "\n".join(
        _table(
            [*BASE_HEADERS, "STATUS"],
            [
                [*_base_cells(c), "enabled" if c.get("enabled", True) else "disabled"]
                for c in channels
            ],
        )
    )


def locations(data: dict) -> str:
    def with_warning(text: str) -> str:
        warning = data.get("warning")
        return f"Warning: {warning}\n{text}" if warning else text

    if not data.get("available"):
        return with_warning(
            "\n".join(
                [
                    f"{data['display_name']}: locations are not listed here.",
                    f"  {data['help']}",
                ]
            )
        )

    provider = data["provider"]
    if "country" in data:
        if not data.get("matched", True):
            return with_warning(
                f"{data['country']!r} is not a {data['display_name']} country. "
                f"See the full list: alle locations {provider}"
            )
        lines = [f"{provider} cities in {data['country']} ({len(data['cities'])}):"]
        lines.extend(f"  {city}" for city in data["cities"])
        return with_warning("\n".join(lines))

    lines = [
        f"{provider}: {data['country_count']} countries, {data['city_count']} cities"
    ]
    for item in data["countries"]:
        cities = item["cities"]
        lines.append(f"  {item['country']}" + (f"  ({len(cities)})" if cities else ""))
        lines.extend(f"      {city}" for city in cities)
    return with_warning("\n".join(lines))


def _router_where(router: dict) -> str:
    port = router.get("port")
    return f"127.0.0.1:{port}" if port else "(port assigned on next daemon start)"


def _router_mode(router: dict) -> str:
    """One phrase for the entrypoint's behavior — must never let a user believe
    they are behind a VPN when the router is passing traffic through.

    The two priority-boundary toggles always show together: the built-in
    LAN block (priority zero — does local traffic bypass the VPN rules?) and
    unmatched handling (priority last — does everything else leak direct or
    hit the kill-switch?).
    """
    n = router.get("rule_count", 0)
    if not n and not router.get("killswitch"):
        return "pass-through (no rules)"
    lan = "LAN bypasses VPN" if router.get("lan_direct", True) else "LAN follows rules"
    mode = f"{n} rule(s), {lan}, unmatched → {router['unmatched']}"
    if router.get("killswitch"):
        mode += " — kill-switch ON"
    return mode


def routes_list(data: dict) -> str:
    router = data["router"]
    head = f"Router entrypoint {_router_where(router)} — {_router_mode(router)}"
    rules = data["rules"]
    if data.get("flat") or data.get("filter"):
        if not rules:
            if data.get("filter"):
                return f"No matchers targeting {data['filter']!r}. See: alle routes ls"
            return "\n".join(
                [
                    head,
                    "No routing matchers — the entrypoint passes everything through "
                    "without a VPN.",
                    "Add one:  alle routes ruleset create Streaming --via <provider>/<channel> --domain netflix.com",
                ]
            )
        headers = ["ID", "RULESET", "MATCH", "TARGET", "NOTE"]
        rows = [
            [
                r["id"],
                r.get("ruleset", ""),
                r["match"],
                r["target"],
                f"shadowed by {routes.shadow_label(r['shadowed_by'])} — never matches"
                if r.get("shadowed_by")
                else "",
            ]
            for r in rules
        ]
        return "\n".join([head, *_table(headers, rows)])

    rulesets = data.get("rulesets") or []
    if not rulesets:
        return "\n".join(
            [
                head,
                "No routing rulesets — the entrypoint passes everything through without a VPN.",
                "Add one:  alle routes ruleset create Streaming --via <provider>/<channel> --domain netflix.com",
            ]
        )
    lines = [head]
    for rs in rulesets:
        lines.append(
            f"{rs['id']}  {rs['name']} → {rs['target']} ({rs['matcher_count']} matcher(s))"
        )
        for rule in rs["rules"]:
            note = (
                f"  [shadowed by {routes.shadow_label(rule['shadowed_by'])} — never matches]"
                if rule.get("shadowed_by")
                else ""
            )
            lines.append(f"  {rule['id']}  {rule['match']}{note}")
    return "\n".join(lines)


def _latency(ms: float | int | None) -> str:
    return f"{ms}ms" if ms is not None else "-"


def _state_cell(c: dict) -> str:
    """The brief state word for the STATE column — never the verbose error.

    Rows carry a ready ``state`` label (``Healthy`` / ``Stopped`` / ``Timeout``
    / …). The verbose explanation lives in ``detail`` (the log / ``--json``),
    not jammed into the table — a long error here used to blow the column width."""
    return c.get("state") or _state_from_error(c)


def _state_from_error(c: dict) -> str:
    if c.get("healthy"):
        return "Healthy"
    err = (c.get("error") or "").lower()
    if err == "stopped":
        return "Stopped"
    return {
        "proxy closed": "Proxy closed",
        "timeout": "Timeout",
        "no valid ip": "No valid IP",
    }.get(err, "Failed")


def test_result(data: dict) -> str:
    channels = data["channels"]
    if not channels:
        if data.get("filter"):
            return f"No channel named {data['filter']!r}. See: alle channels ls"
        return "No channels configured. Add one:  alle channels add nordvpn --country …"

    if data.get("speed"):
        # --speed strictly APPENDS to the plain table: same columns in the
        # same order, plus DOWNLOAD and UPLOAD at the end.
        headers = [
            *BASE_HEADERS,
            "STATE",
            "LATENCY",
            "IP",
            "SENT",
            "RECV",
            "DOWNLOAD",
            "UPLOAD",
        ]
        rows = []
        for c in channels:
            speed = c.get("speed_result") or {}
            rows.append(
                [
                    *_base_cells(c),
                    _state_cell(c),
                    _latency(c.get("latency_ms")),
                    c.get("ip") or "-",
                    _bytes(c.get("sent", 0)),
                    _bytes(c.get("received", 0)),
                    _mbps(speed.get("download_bps")),
                    _mbps(speed.get("upload_bps")),
                ]
            )
        return "\n".join(_table(headers, rows))

    headers = [*BASE_HEADERS, "STATE", "LATENCY", "IP", "SENT", "RECV"]
    rows = [
        [
            *_base_cells(c),
            _state_cell(c),
            _latency(c.get("latency_ms")),
            c.get("ip") or "-",
            _bytes(c.get("sent", 0)),
            _bytes(c.get("received", 0)),
        ]
        for c in channels
    ]
    return "\n".join(_table(headers, rows))


# The speed-test result columns appended after BASE_HEADERS, and fixed minimum
# widths for them. Their values aren't known until each channel finishes, so the
# streamed table can't size them from the data; these floors fit typical
# "Healthy" / "23ms" / "45.2 Mbps" cells and keep rows aligned as they appear.
_SPEED_RESULT_HEADERS = ["STATE", "LATENCY", "IP", "SENT", "RECV", "DOWNLOAD", "UPLOAD"]
_SPEED_RESULT_WIDTHS = [7, 7, 15, 8, 8, 10, 10]
_SPEED_HEADERS = [*BASE_HEADERS, *_SPEED_RESULT_HEADERS]


def test_stream_widths(chans: list[dict]) -> list[int]:
    """Column widths for the live speed-test table.

    Base columns are sized from the channels that will be tested (known up front
    via ``on_begin``), so LABEL/ID/PORT/COUNTRY/CITY line up; the result columns
    use the fixed floors above (their values arrive later).
    """
    widths = [len(h) for h in BASE_HEADERS]
    for c in chans:
        for i, cell in enumerate(_base_cells(c)):
            widths[i] = max(widths[i], len(cell))
    return widths + list(_SPEED_RESULT_WIDTHS)


def _stream_line(cells: list[str], widths: list[int]) -> str:
    return "  ".join(
        str(cells[i]).ljust(widths[i]) for i in range(len(widths))
    ).rstrip()


def test_stream_header(widths: list[int]) -> str:
    """The header + dash separator for the live table (printed once, up front)."""
    return (
        _stream_line(_SPEED_HEADERS, widths) + "\n" + "  ".join("-" * w for w in widths)
    )


def test_stream_row(row: dict, widths: list[int]) -> str:
    """One aligned row for a just-completed channel (same columns as the final
    table from :func:`test_result`)."""
    speed = row.get("speed_result") or {}
    cells = [
        *_base_cells(row),
        _state_cell(row),
        _latency(row.get("latency_ms")),
        row.get("ip") or "-",
        _bytes(row.get("sent", 0)),
        _bytes(row.get("received", 0)),
        _mbps(speed.get("download_bps")),
        _mbps(speed.get("upload_bps")),
    ]
    return _stream_line(cells, widths)


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
        lines.extend(_runtime_lines({"daemon": d}))
    else:
        lines.append("Daemon: not running.")
    return "\n".join(lines)


def _runtime_lines(snapshot: dict) -> list[str]:
    """A warning line when the daemon reports sing-box in a degraded state."""
    rt = (snapshot.get("daemon") or {}).get("runtime") or {}
    status = rt.get("singbox")
    if status in {"crashed", "crash_looping", "config_rejected", "degraded"}:
        label = str(status).replace("_", "-")
        detail = rt.get("detail")
        return [f"  ⚠ sing-box {label}" + (f": {detail}" if detail else "")]
    return []


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


def _channels_summary(channels: list[dict]) -> str:
    """Per-provider channel counts with the enabled split when it matters:
    ``NordVPN: 6 channels (4 enabled), Proton VPN: 1 channel``."""
    from alle.providers import display_name

    counts: dict[str, list[int]] = {}  # provider -> [total, enabled]
    for c in channels:
        entry = counts.setdefault(c["provider"], [0, 0])
        entry[0] += 1
        entry[1] += 1 if c.get("enabled", True) else 0
    parts = []
    for p, (total, enabled) in sorted(counts.items()):
        label = f"{display_name(p)}: {total} channel" + ("" if total == 1 else "s")
        if enabled != total:
            label += f" ({enabled} enabled)"
        parts.append(label)
    return ", ".join(parts)


def status(snapshot: dict) -> str:
    """The system-level summary: run state, per-provider channel counts, router
    posture, Web UI. Deliberately no per-channel table — `alle test` is the one
    channel-detail view (fresh probes + traffic), and rendering the same rows
    here from cached probes needed an AGO column just to qualify staleness."""
    channels = snapshot["channels"]
    if not snapshot["running"]:
        lines = ["Alle - Inactive", *_skew_lines(snapshot), *_runtime_lines(snapshot)]
        if channels:
            detail = f"{snapshot['channel_count']} channel(s)"
            if snapshot.get("disabled_count"):
                detail += f" ({snapshot['enabled_count']} enabled)"
            lines.append(
                f"  ({detail} across "
                f"{snapshot['provider_count']} provider(s) configured; run `alle start`)"
            )
        return "\n".join(lines)

    lines = ["Alle - Active", *_skew_lines(snapshot), *_runtime_lines(snapshot)]
    if channels:
        lines.append(
            f"  Channels  {_channels_summary(channels)}  (details: alle channels ls)"
        )
    router = snapshot.get("router")
    if router:
        lines.append(
            f"  Router    {_router_where(router)} — {_router_mode(router)}"
            "  (details: alle routes ls)"
        )
        if router.get("tun"):
            lines.append(
                "  TUN       ON — all system traffic follows the routing rules"
                "  (disable: alle tun off)"
            )
    if snapshot.get("web_ui"):
        lines.append(f"  Web UI    {snapshot['web_ui']}  (open it: alle ui)")
    if snapshot.get("rest_api"):
        lines.append(
            f"  REST API  {snapshot['rest_api']} — shares the Web UI listener; "
            "Bearer auth required"
        )
    if not channels:
        lines.append(
            '  (no channels yet — add one: alle channels add nordvpn --country "United States")'
        )
    return "\n".join(lines)
