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


def providers_list(data: dict) -> str:
    providers = data["providers"]
    if not providers:
        return "No providers added yet. Add one:  alle providers add nordvpn"
    return "\n".join(f"  {p['display_name']:20}  {p['credential']}".rstrip() for p in providers)


def channels_list(data: dict) -> str:
    providers = data["providers"]
    if not providers:
        return "No providers added yet. Add one:  alle providers add nordvpn"

    channels = data["channels"]
    cols = ("name", "port", "country", "city")
    width = {c: max((len(str(r[c])) for r in channels), default=0) for c in cols}
    lines: list[str] = []
    for provider in providers:
        lines.append(f"{provider}:")
        rows = [r for r in channels if r["provider"] == provider]
        if not rows:
            lines.append("    (no channels)")
            continue
        for row in rows:
            lines.append(("    " + "  ".join(str(row[c]).ljust(width[c]) for c in cols)).rstrip())
    return "\n".join(lines)


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
        lines = [f"{provider} cities in {data['country']} ({len(data['cities'])}):"]
        lines.extend(f"  {city}" for city in data["cities"])
        return "\n".join(lines)

    lines = [f"{provider}: {data['country_count']} countries, {data['city_count']} cities"]
    for item in data["countries"]:
        cities = item["cities"]
        lines.append(f"  {item['country']}" + (f"  ({len(cities)})" if cities else ""))
        lines.extend(f"      {city}" for city in cities)
    return "\n".join(lines)


def status(snapshot: dict) -> str:
    channels = snapshot["channels"]
    if not snapshot["running"]:
        lines = ["Alle - Inactive"]
        if channels:
            lines.append(
                f"  ({snapshot['channel_count']} channel(s) across "
                f"{snapshot['provider_count']} provider(s) configured; run `alle start`)"
            )
        return "\n".join(lines)

    lines = ["Alle - Active"]
    if not channels:
        lines.append(
            '  (no channels yet — add one: alle channels add nordvpn --country "United States")'
        )
        return "\n".join(lines)

    rows = []
    for channel in channels:
        latency = f"{channel['latency_ms']}ms" if channel["latency_ms"] is not None else "-"
        ip = channel["ip"] or "-"
        probe = channel.get("probe") or {}
        rows.append(
            {
                "provider": channel["provider"],
                "name": channel["name"],
                "port": channel["port"],
                "country": channel["country"],
                "city": channel["city"],
                "state": channel["state"],
                "ago": _ago(probe.get("at", 0)) if probe else "-",
                "lat": latency,
                "ip": ip,
            }
        )
    cols = ("name", "port", "country", "city", "state", "ago", "lat", "ip")
    width = {c: max((len(str(r[c])) for r in rows), default=0) for c in cols}
    for provider in sorted({r["provider"] for r in rows}):
        lines.append(f"{provider}:")
        for row in (r for r in rows if r["provider"] == provider):
            lines.append(("    " + "  ".join(str(row[c]).ljust(width[c]) for c in cols)).rstrip())
    return "\n".join(lines)
