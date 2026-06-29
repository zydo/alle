"""Reusable application operations for alle.

This module is the seam shared by the CLI today and future local daemon/API,
Web UI, and desktop clients. It orchestrates domain/runtime modules and returns
structured Python data; it deliberately does not print, prompt, or exit.
"""

from __future__ import annotations

from alle import applog, credentials, daemon, locations, paths, singbox
from alle.engine import Engine
from alle.providers import (
    PROVIDERS,
    ProviderError,
    auth_fields,
    config_help,
    display_name,
    is_functional,
    kind,
    known,
    match,
    preview,
    provider_wg,
)
from alle.state import Store


class ServiceError(RuntimeError):
    """A user-correctable application error, ready to show in the CLI."""


def resolve_provider(name: str) -> str:
    """Map a typed provider name to its key."""
    provider = match(name)
    if provider is None:
        names = ", ".join(known())
        raise ServiceError(f"unknown provider {name!r} (known: {names}).")
    return provider


def validate_provider_credentials(provider: str, creds: dict) -> None:
    """Check a functional provider's credential against its API."""
    PROVIDERS[provider]["derive_key"](creds)


def provider_add_config(provider: str) -> dict:
    store = Store.load()
    store.add_provider(provider)
    applog.log(f"added provider {provider} (config-based)")
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "config_help": config_help(provider),
    }


def provider_add_token(provider: str, creds: dict) -> dict:
    store = Store.load()
    if is_functional(provider):
        validate_provider_credentials(provider, creds)
    credentials.set_(provider, creds)
    store.add_provider(provider)
    applog.log(f"added provider {provider}")
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "credential_preview": preview(provider, creds),
        "functional": is_functional(provider),
    }


def provider_list() -> dict:
    store = Store.load()
    providers = []
    for provider in store.provider_names():
        detail = ""
        creds = credentials.get(provider) or {}
        for field in auth_fields(provider):
            if field.secret:
                value = str(creds.get(field.key, ""))
                detail = f"******{value[-4:]}" if value else ""
                break
        providers.append(
            {
                "provider": provider,
                "display_name": display_name(provider),
                "credential": detail,
            }
        )
    return {"providers": providers}


def provider_remove(provider: str) -> dict:
    store = Store.load()
    if not store.has_provider(provider):
        raise ServiceError(f"{display_name(provider)} is not added.")
    count = len(store.provider_channels(provider))
    store.remove_provider(provider)
    credentials.remove(provider)
    applog.log(f"removed provider {provider} ({count} channel(s))")
    daemon.ensure_running()
    return {"provider": provider, "display_name": display_name(provider), "channels_removed": count}


def channel_add(
    provider: str, country: str | None, city: str | None, config: str | None = None
) -> dict:
    store = Store.load()
    if not store.has_provider(provider):
        raise ServiceError(
            f"{display_name(provider)} is not added — run `alle providers add {provider}` first."
        )

    if config:
        raise ServiceError(
            "importing channels from a WireGuard .conf is not implemented yet (post-MVP). "
            f"For now, {display_name(provider)} channels cannot be added."
        )

    if not is_functional(provider):
        if kind(provider) == "config":
            raise ServiceError(
                f"{display_name(provider)} channels are added from a .conf file: "
                f"alle channels add {provider} --config /path/to/wireguard.conf "
                "(not implemented yet)."
            )
        raise ServiceError(f"adding channels under {display_name(provider)} isn't implemented yet.")

    if not country:
        raise ServiceError(
            f"usage: alle channels add {provider} --country <country> [--city <city>]"
        )

    try:
        wg = provider_wg(provider, country, city or "")
    except ProviderError as e:
        msg = str(e)
        if "not a" in msg and "location" in msg:
            msg += f"\nSee available locations: alle locations {provider}"
        raise ServiceError(msg) from e

    channel = store.add_channel(provider, country, city or "", wg)
    applog.log(f"added channel {provider}/{channel.id} ({channel.location}) on :{channel.port}")
    daemon.ensure_running()
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "channel": channel,
    }


def channel_list() -> dict:
    store = Store.load()
    channels = []
    for channel in store.channels():
        channels.append(
            {
                "provider": channel.provider,
                "name": channel.id,
                "port": f":{channel.port}",
                "port_number": channel.port,
                "country": channel.country or "—",
                "city": channel.city or ("Any City" if channel.country else "—"),
            }
        )
    return {"providers": store.provider_names(), "channels": channels}


def channel_remove(provider: str, channel_id: str) -> dict:
    store = Store.load()
    if not store.has_provider(provider):
        raise ServiceError(f"{display_name(provider)} is not added.")
    if not store.get_channel(provider, channel_id):
        raise ServiceError(
            f"no channel {channel_id!r} under {display_name(provider)} (see: alle status)."
        )
    store.remove_channel(provider, channel_id)
    applog.log(f"removed channel {provider}/{channel_id}")
    daemon.ensure_running()
    return {"provider": provider, "display_name": display_name(provider), "channel": channel_id}


def locations_list(provider: str, country: str | None = None, refresh: bool = False) -> dict:
    if not is_functional(provider):
        help_text = (
            config_help(provider) or f"{display_name(provider)} does not expose a locations API."
        )
        return {
            "provider": provider,
            "display_name": display_name(provider),
            "available": False,
            "help": help_text,
        }

    state = paths.state_dir()
    if refresh or locations.needs_refresh(state, provider):
        locations.update(state, [provider])
    locs = locations.load(state, provider)

    if country:
        hit = next((c for c in locs if c.lower() == country.lower()), None)
        cities = locs.get(hit, []) if hit else []
        return {
            "provider": provider,
            "display_name": display_name(provider),
            "available": True,
            "country": country,
            "cities": cities,
        }

    countries = [{"country": name, "cities": cities} for name, cities in sorted(locs.items())]
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "available": True,
        "countries": countries,
        "country_count": len(locs),
        "city_count": sum(len(v) for v in locs.values()),
    }


def status_snapshot() -> dict:
    store = Store.load()
    runner = singbox.Runner()
    running = runner.is_running()
    channels = []
    for channel in store.channels():
        probe = channel.probe or {}
        if probe.get("ok"):
            state = "Active"
            latency = int(probe["latency_ms"]) if probe.get("latency_ms") is not None else None
            ip = probe.get("ip") or None
        elif probe:
            state = (probe.get("error") or "failed").capitalize()
            latency = None
            ip = None
        else:
            state = "Pending"
            latency = None
            ip = None
        channels.append(
            {
                "provider": channel.provider,
                "name": channel.id,
                "port": f":{channel.port}",
                "port_number": channel.port,
                "country": channel.country or "—",
                "city": channel.city or ("Any City" if channel.country else "—"),
                "state": state,
                "probe": probe,
                "latency_ms": latency,
                "ip": ip,
            }
        )
    return {
        "running": running,
        "state": "running" if running else "stopped",
        "channels": channels,
        "provider_count": len({c["provider"] for c in channels}),
        "channel_count": len(channels),
    }


def probe_all() -> dict:
    store = Store.load()
    if not store.channels():
        return {"probed": False, "reason": "no_channels", "status": status_snapshot()}
    Engine(store).probe_all()
    return {"probed": True, "status": status_snapshot()}


def _stop_all() -> bool:
    runner = singbox.Runner()
    was_singbox = runner.is_running()
    was_applier = daemon.stop()
    if was_singbox:
        runner.stop()
    return was_singbox or was_applier


def start() -> dict:
    daemon.ensure_running()
    applog.log("start")
    return {"has_channels": bool(Store.load().channels())}


def stop() -> dict:
    was_running = _stop_all()
    applog.log("stop")
    return {"was_running": was_running}


def restart() -> dict:
    _stop_all()
    daemon.ensure_running()
    applog.log("restart")
    return {}


def logs_tail(lines: int = 200) -> str:
    return applog.tail(lines)
