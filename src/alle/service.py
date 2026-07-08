"""Reusable application operations for alle.

This module is the seam shared by the CLI today and future local daemon/API,
Web UI, and desktop clients. It orchestrates domain/runtime modules and returns
structured Python data; it deliberately does not print, prompt, or exit.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path

from alle import (
    __version__,
    applog,
    credentials,
    daemon,
    daemonctl,
    geo,
    locations,
    metrics,
    paths,
    routes,
    singbox,
    throughput,
    wgconf,
)
from alle.engine import Engine
from alle.providers import (
    PROVIDERS,
    ProviderError,
    auth_fields,
    auth_help,
    config_help,
    display_name,
    is_functional,
    kind,
    known,
    match,
    preview,
    provider_wg,
)
from alle.state import ReferencedError, Store


class ServiceError(RuntimeError):
    """A user-correctable application error, ready to show in the CLI."""


def _blockers_error(blockers: dict[str, list[dict]]) -> ServiceError:
    """Restrict-only removal refusal: every blocker in one pass, with the fix."""
    lines = ["cannot remove — routing rules still reference:"]
    ids: list[str] = []
    for ref in sorted(blockers):
        rules_ = blockers[ref]
        ids.extend(r["id"] for r in rules_)
        detail = ", ".join(f"{r['id']} ({routes.describe(r)})" for r in rules_)
        lines.append(f"  {ref} — {detail}")
    lines.append(f"Remove the rules first:  alle routes rm {' '.join(ids)}")
    return ServiceError("\n".join(lines))


def resolve_provider(name: str) -> str:
    """Map a typed provider name to its key."""
    provider = match(name)
    if provider is None:
        names = ", ".join(known())
        raise ServiceError(f"unknown provider {name!r} (known: {names}).")
    return provider


def _country_display(channel) -> str:
    """Country label for a channel, or a braced placeholder when unknown."""
    return channel.country or "(Unknown)"


def _city_display(channel) -> str:
    """City label (kept called "City" even when it's a state/region). Config
    channels with no parsed location read ``(Unknown)``; an API channel with a
    country but no city means "any city in that country"."""
    if channel.city:
        return channel.city
    if kind(channel.provider) == "config":
        return "(Unknown)"
    return "(Any City)" if channel.country else "(Unknown)"


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


def provider_catalog() -> dict:
    """Every provider alle recognises + how to add it — drives the UI add form.

    For each: its kind (``token``/``config``), whether it's functional, the
    credential fields to prompt for (token providers), and the portal help
    (config providers).
    """
    out = []
    for p in known():
        instructions, url = auth_help(p)
        out.append(
            {
                "provider": p,
                "display_name": display_name(p),
                "kind": kind(p),
                "functional": is_functional(p),
                "fields": [
                    {"key": f.key, "label": f.label, "secret": f.secret}
                    for f in auth_fields(p)
                ],
                "config_help": config_help(p),
                "help": instructions,
                "url": url,
            }
        )
    return {"providers": out}


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
                "kind": kind(provider),
                "credential": detail,
                "channel_count": len(store.provider_channels(provider)),
            }
        )
    return {"providers": providers}


def _provider_removal_plan(providers: list[str]) -> list[dict]:
    if not providers:
        raise ServiceError("at least one provider is required.")
    providers = list(dict.fromkeys(providers))
    store = Store.load()
    plan = []
    refs: set[tuple[str, str]] = set()
    for provider in providers:
        if not store.has_provider(provider):
            raise ServiceError(f"{display_name(provider)} is not added.")
        refs.update((provider, ch.id) for ch in store.provider_channels(provider))
        plan.append(
            {
                "provider": provider,
                "display_name": display_name(provider),
                "channels_removed": len(store.provider_channels(provider)),
            }
        )
    blockers = store.rules_referencing(refs)
    if blockers:
        raise _blockers_error(blockers)
    return plan


def provider_remove(provider: str) -> dict:
    return provider_remove_many([provider])["providers"][0]


def provider_remove_many(providers: list[str], dry_run: bool = False) -> dict:
    planned = _provider_removal_plan(providers)
    if dry_run:
        return {"providers": planned, "dry_run": True}

    store = Store.load()
    for item in planned:
        provider = item["provider"]
        # Store first: if a later step fails, the leftover is a stray credential
        # on disk (which re-adding repairs), never a provider that is still
        # listed but has silently lost its credential.
        try:
            store.remove_provider(provider)
        except ReferencedError as e:  # rule added between plan and removal
            raise _blockers_error(e.blockers) from e
        credentials.remove(provider)
        metrics.remove_provider(provider)
        applog.log(
            f"removed provider {provider} ({item['channels_removed']} channel(s))"
        )
    daemon.ensure_running()
    return {"providers": planned, "dry_run": False}


def channel_add(
    provider: str,
    country: str | None,
    city: str | None,
    config: str | None = None,
    label: str = "",
) -> dict:
    store = Store.load()
    label = label.strip()
    if not store.has_provider(provider):
        raise ServiceError(
            f"{display_name(provider)} is not added — run `alle providers add {provider}` first."
        )

    # The two archetypes are mutually exclusive: token/API providers locate a server
    # by --country/--city; config providers import a .conf. They cannot be combined,
    # and a .conf import never invents a country/city it can't know.
    if config and (country or city):
        raise ServiceError(
            "--config cannot be combined with --country/--city: a WireGuard .conf is "
            "imported as-is, while --country/--city locate a server via an API provider "
            "(e.g. nordvpn). Use one or the other."
        )

    if config:
        return _channel_add_config(store, provider, config, label)

    if kind(provider) == "config":
        raise ServiceError(
            f"{display_name(provider)} channels are imported from a WireGuard .conf: "
            f"alle channels add {provider} --config /path/to/wireguard.conf"
        )
    if not is_functional(provider):
        raise ServiceError(
            f"adding channels under {display_name(provider)} isn't implemented yet."
        )

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

    channel = store.add_channel(provider, country, city or "", wg, label)
    applog.log(
        f"added channel {provider}/{channel.id} ({channel.location}) on :{channel.port}"
    )
    daemon.ensure_running()
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "channel": channel,
    }


def _channel_add_config(
    store: Store, provider: str, config: str, label: str = ""
) -> dict:
    """Import a channel from a WireGuard ``.conf`` (the config-provider archetype).

    Each ``.conf`` is a single server/peer, so one file becomes one channel. The
    parsed params land in ``state.json`` in the *same* shape the NordVPN API path
    produces, so imported and API-derived channels are identical to the engine.

    A ``.conf`` carries no country/city, and alle does not geolocate — so the
    channel id is taken from the file name (a factual, user-chosen label), and
    country/city are left empty rather than guessed.
    """
    path = Path(config).expanduser()
    if not path.is_file():
        raise ServiceError(f"config file not found: {config}")
    try:
        text = path.read_text()
    except OSError as e:
        raise ServiceError(f"could not read {config}: {e}") from e
    return _import_conf(store, provider, path.name, text, label)


def channel_add_conf_text(
    provider: str, filename: str, text: str, label: str = ""
) -> dict:
    """Import a channel from ``.conf`` *content* (the Web UI upload path).

    Same as ``channels add --config <file>`` but the caller supplies the bytes
    directly, since a browser can't hand the daemon a server-side file path.
    """
    store = Store.load()
    if not store.has_provider(provider):
        raise ServiceError(
            f"{display_name(provider)} is not added — add the provider first."
        )
    return _import_conf(store, provider, filename, text, label.strip())


def _import_conf(
    store: Store, provider: str, filename: str, text: str, label: str
) -> dict:
    """Parse a ``.conf`` (from a file or an upload) and upsert it as a channel.

    A ``.conf`` carries no country/city, and alle does not geolocate — so the
    channel id is the file name (a factual, user-chosen label), and country/city
    are parsed best-effort from the name's ISO codes rather than guessed.
    """
    if kind(provider) != "config":
        raise ServiceError(
            f"{display_name(provider)} uses an API — add channels by country, "
            f"not a .conf (see: alle locations {provider})."
        )
    try:
        wg = wgconf.parse(text)
    except wgconf.ConfError as e:
        raise ServiceError(f"{filename} is not a usable WireGuard .conf: {e}") from e
    stem = Path(filename).stem
    country, city = geo.from_filename(stem)
    # Identity is the file name: re-importing the same .conf updates it in place
    # (keys may have rotated) rather than creating wg_..._2.
    channel, created = store.upsert_channel(provider, stem, country, city, wg, label)
    action = "imported" if created else "updated"
    applog.log(
        f"{action} channel {provider}/{channel.id} from {filename} on :{channel.port}"
    )
    daemon.ensure_running()
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "channel": channel,
        "imported_from": filename,
        "updated": not created,
    }


def channel_list() -> dict:
    store = Store.load()
    channels = []
    for channel in store.channels():
        channels.append(
            {
                "provider": channel.provider,
                "name": channel.id,
                "label": channel.label,
                "port": f":{channel.port}",
                "port_number": channel.port,
                "country": _country_display(channel),
                "city": _city_display(channel),
            }
        )
    return {"providers": store.provider_names(), "channels": channels}


def metrics_snapshot(channel: str | None = None) -> dict:
    """Cumulative per-channel sent/received byte totals.

    Rows are the currently-configured channels (totals for removed channels are
    dropped at removal time), each joined with its stored counters. ``channel``
    filters to channels whose id matches, across every provider.
    """
    store = Store.load()
    stored = metrics.totals()
    rows = []
    for ch in store.channels():
        if channel and ch.id != channel:
            continue
        t = stored.get((ch.provider, ch.id), {})
        sent = int(t.get("sent", 0))
        received = int(t.get("received", 0))
        rows.append(
            {
                "provider": ch.provider,
                "name": ch.id,
                "label": ch.label,
                "port": f":{ch.port}",
                "port_number": ch.port,
                "country": _country_display(ch),
                "city": _city_display(ch),
                "sent": sent,
                "received": received,
                "total": sent + received,
                "updated_at": int(t.get("updated_at", 0)),
            }
        )
    return {
        "channels": rows,
        "filter": channel,
        "total_sent": sum(r["sent"] for r in rows),
        "total_received": sum(r["received"] for r in rows),
    }


def _is_pattern(ref: str) -> bool:
    return any(ch in ref for ch in "*?[")


def _channel_ref_matches(channel_id: str, ref: str) -> bool:
    return fnmatchcase(channel_id, ref) if _is_pattern(ref) else channel_id == ref


def _channel_row(provider: str, channel_id: str) -> dict:
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "channel": channel_id,
        "ref": f"{provider}/{channel_id}",
    }


def _resolve_channel_ref(store: Store, ref: str, provider: str | None) -> list[dict]:
    if "/" in ref and provider is None:
        provider_ref, channel_ref = ref.split("/", 1)
        matched_provider = match(provider_ref)
        if matched_provider is None:
            names = ", ".join(known())
            raise ServiceError(f"unknown provider {provider_ref!r} (known: {names}).")
        return _resolve_channel_ref(store, channel_ref, matched_provider)

    if provider is not None:
        matches = [
            _channel_row(ch.provider, ch.id)
            for ch in store.provider_channels(provider)
            if _channel_ref_matches(ch.id, ref)
        ]
        if not matches:
            raise ServiceError(
                f"no channel {ref!r} under {display_name(provider)} "
                "(see: alle channels ls)."
            )
        return matches

    matches = [
        _channel_row(ch.provider, ch.id)
        for ch in store.channels()
        if _channel_ref_matches(ch.id, ref)
    ]
    if not matches:
        raise ServiceError(f"no channel named {ref!r} (see: alle channels ls).")
    if not _is_pattern(ref) and len(matches) > 1:
        providers = ", ".join(item["display_name"] for item in matches)
        raise ServiceError(
            f"channel {ref!r} exists under multiple providers ({providers}); "
            f"use a qualified ref like: alle channels rm {matches[0]['ref']}"
        )
    return matches


def _channel_removal_plan(
    refs: list[str], provider: str | None = None, all_: bool = False
) -> list[dict]:
    store = Store.load()
    if provider is not None and not store.has_provider(provider):
        raise ServiceError(f"{display_name(provider)} is not added.")

    if all_:
        if refs:
            raise ServiceError("--all cannot be combined with channel names.")
        if provider is None:
            raise ServiceError("--all for channels requires --provider.")
        matches = [
            _channel_row(ch.provider, ch.id) for ch in store.provider_channels(provider)
        ]
        if not matches:
            raise ServiceError(f"no channels under {display_name(provider)}.")
    else:
        if not refs:
            raise ServiceError("at least one channel name is required.")
        matches = []
        for ref in refs:
            matches.extend(_resolve_channel_ref(store, ref, provider))

    plan = []
    seen = set()
    for item in matches:
        key = (item["provider"], item["channel"])
        if key not in seen:
            seen.add(key)
            plan.append(item)
    blockers = store.rules_referencing(
        {(item["provider"], item["channel"]) for item in plan}
    )
    if blockers:
        raise _blockers_error(blockers)
    return plan


def channel_remove_many(
    channel_ids: list[str],
    provider: str | None = None,
    dry_run: bool = False,
    all_: bool = False,
) -> dict:
    planned = _channel_removal_plan(channel_ids, provider, all_)
    if dry_run:
        return {"channels": planned, "dry_run": True}

    for item in planned:
        try:
            Store.load().remove_channel(item["provider"], item["channel"])
        except ReferencedError as e:  # rule added between plan and removal
            raise _blockers_error(e.blockers) from e
        metrics.remove_channel(item["provider"], item["channel"])
        applog.log(f"removed channel {item['provider']}/{item['channel']}")
    daemon.ensure_running()
    return {"channels": planned, "dry_run": False}


def channel_remove(provider: str, channel_id: str) -> dict:
    result = channel_remove_many([channel_id], provider)
    removed = result["channels"][0]
    return {
        "provider": removed["provider"],
        "display_name": removed["display_name"],
        "channel": removed["channel"],
    }


def channel_set_label(ref: str, label: str, provider: str | None = None) -> dict:
    """Set or clear one channel's display label. ``ref`` is a channel id or a
    ``provider/id`` ref (never a glob — a label targets exactly one channel).

    An empty ``label`` clears it, so the display falls back to the id.
    """
    if _is_pattern(ref):
        raise ServiceError("a glob cannot be used to label a single channel.")
    label = label.strip()
    store = Store.load()
    matched = _resolve_channel_ref(
        store, ref, provider
    )  # 1 row (non-glob, unambiguous)
    item = matched[0]
    store.set_label(item["provider"], item["channel"], label)
    applog.log(
        f"labelled {item['provider']}/{item['channel']} "
        + (f"as {label!r}" if label else "(cleared)")
    )
    return {
        "provider": item["provider"],
        "display_name": item["display_name"],
        "channel": item["channel"],
        "label": label,
        "cleared": not label,
    }


def locations_list(
    provider: str, country: str | None = None, refresh: bool = False
) -> dict:
    if not is_functional(provider):
        help_text = (
            config_help(provider)
            or f"{display_name(provider)} does not expose a locations API."
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
            "country": hit or country,
            "matched": hit is not None,
            "cities": cities,
        }

    countries = [
        {"country": name, "cities": cities} for name, cities in sorted(locs.items())
    ]
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "available": True,
        "countries": countries,
        "country_count": len(locs),
        "city_count": sum(len(v) for v in locs.values()),
    }


# ---- routing ----------------------------------------------------------------


def _router_info(store: Store) -> dict:
    """The router entrypoint's state for status/routes displays."""
    router = store.router
    port = int(router.get("port") or 0)
    rules_ = router.get("rules") or []
    killswitch = bool(router.get("killswitch"))
    return {
        "port": port or None,
        "allocated": bool(port),
        "rule_count": len(rules_),
        "killswitch": killswitch,
        "lan_direct": bool(router.get("lan_direct", True)),
        "unmatched": "block" if killswitch else "direct",
    }


def _decorate_rule(rule: dict, shadows: dict[str, str]) -> dict:
    return {
        **rule,
        "match": routes.describe(rule),
        "shadowed_by": shadows.get(rule["id"]),
    }


def routes_add(matcher_type: str, value: str, target: str) -> dict:
    """Append one routing rule; returns it plus any shadow warning."""
    try:
        normalized = routes.normalize_value(matcher_type, value)
        _, ref = routes.parse_target(target)
    except routes.RuleError as e:
        raise ServiceError(str(e)) from e
    if ref is not None:  # a channel target (ref is None for direct/block)
        provider = match(ref[0])  # accept the brand name too, like channel refs
        if provider is None:
            names = ", ".join(known())
            raise ServiceError(
                f"unknown provider {ref[0]!r} in target (known: {names})."
            )
        target = f"{provider}/{ref[1]}"

    store = Store.load()
    try:
        # channel-target existence is verified inside the transaction — the
        # mirror of the removal guard, so rules and channels can't cross in flight
        rule = store.add_rule(matcher_type, normalized, target)
    except ValueError as e:
        raise ServiceError(f"{e} (see: alle channels ls --refs).") from e
    shadows = routes.shadowed_by(store.rules())
    applog.log(f"added route {rule['id']}: {routes.describe(rule)} -> {rule['target']}")
    daemon.ensure_running()
    return {
        "rule": _decorate_rule(rule, shadows),
        "router_port": store.router.get("port") or None,
        "shadowed_by": shadows.get(rule["id"]),
    }


def routes_list(channel: str | None = None) -> dict:
    """Rules in evaluation order, shadow-annotated; ``channel`` filters by target
    (bare channel id, or a qualified ``provider/id`` ref)."""
    store = Store.load()
    rules_ = store.rules()
    shadows = routes.shadowed_by(rules_)
    rows = []
    for rule in rules_:
        if channel is not None:
            _, _, cid = rule["target"].partition("/")
            if not cid:  # direct/block targets never reference a channel
                continue
            if rule["target"] != channel and cid != channel:
                continue
        rows.append(_decorate_rule(rule, shadows))
    return {"rules": rows, "filter": channel, "router": _router_info(store)}


def routes_remove(ids: list[str], dry_run: bool = False) -> dict:
    if not ids:
        raise ServiceError("at least one rule id is required (see: alle routes ls).")
    ids = list(dict.fromkeys(ids))
    store = Store.load()
    existing = {rule["id"]: rule for rule in store.rules()}
    missing = [i for i in ids if i not in existing]
    if missing:  # all misses in one pass, like the removal blockers
        raise ServiceError(f"no rule(s) {', '.join(missing)} (see: alle routes ls).")
    planned = [_decorate_rule(existing[i], {}) for i in ids]
    if dry_run:
        return {"rules": planned, "dry_run": True}
    store.remove_rules(ids)
    for rule in planned:
        applog.log(f"removed route {rule['id']}: {rule['match']} -> {rule['target']}")
    daemon.ensure_running()
    return {"rules": planned, "dry_run": False}


def routes_reorder(ids: list[str]) -> dict:
    """Replace evaluation order with a full permutation of existing rule ids."""
    if not ids:
        raise ServiceError("at least one rule id is required (see: alle routes ls).")
    store = Store.load()
    try:
        ordered, changed = store.reorder_rules(ids)
    except ValueError as e:
        raise ServiceError(f"{e} (pass every rule id exactly once).") from e
    shadows = routes.shadowed_by(ordered)
    if changed:
        applog.log("reordered routes: " + " ".join(rule["id"] for rule in ordered))
        daemon.ensure_running()
    return {
        "rules": [_decorate_rule(rule, shadows) for rule in ordered],
        "changed": changed,
        "router": _router_info(store),
    }


def routes_lan_direct(enable: bool | None = None) -> dict:
    """Set (or with ``None`` just report) the built-in LAN/local direct rules.

    The rules themselves (:data:`alle.routes.LAN_DIRECT_CIDRS`) are fixed and
    compiled ahead of every user rule; this toggle is the only control.
    """
    store = Store.load()
    if enable is not None:
        store.set_lan_direct(enable)
        applog.log(
            "lan-direct "
            + (
                "enabled: built-in LAN/local rules go direct ahead of user rules"
                if enable
                else "disabled: LAN/local traffic follows user rules"
            )
        )
        daemon.ensure_running()
    return {
        "changed": enable is not None,
        "router": _router_info(store),
        "cidrs": list(routes.LAN_DIRECT_CIDRS),
    }


def routes_killswitch(enable: bool | None = None) -> dict:
    """Set (or with ``None`` just report) unmatched-traffic blocking."""
    store = Store.load()
    if enable is not None:
        store.set_killswitch(enable)
        applog.log(
            "kill-switch "
            + (
                "enabled: router unmatched -> block"
                if enable
                else "disabled: router unmatched -> direct"
            )
        )
        daemon.ensure_running()
    return {"changed": enable is not None, "router": _router_info(store)}


def status_snapshot() -> dict:
    store = Store.load()
    runner = singbox.Runner()
    running = runner.is_running()
    channels = []
    for channel in store.channels():
        probe = channel.probe or {}
        recon = channel.reconnect or {}
        if probe.get("ok"):
            state = "Active"
            latency = probe.get("latency_ms")
            ip = probe.get("ip") or None
        elif recon.get("failed"):
            state = "Reconnect failed"
            latency = None
            ip = None
        elif recon.get("attempts"):
            state = f"Reconnecting ({recon['attempts']})"
            latency = None
            ip = None
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
                "label": channel.label,
                "port": f":{channel.port}",
                "port_number": channel.port,
                "country": _country_display(channel),
                "city": _city_display(channel),
                "state": state,
                "probe": probe,
                "reconnect": recon,
                "latency_ms": latency,
                "ip": ip,
            }
        )
    return {
        "running": running,
        "state": "running" if running else "stopped",
        "router": _router_info(store),
        "daemon": _daemon_info(),
        "web_ui": web_ui_url(),
        "channels": channels,
        "provider_count": len({c["provider"] for c in channels}),
        "channel_count": len(channels),
    }


def web_ui_url() -> str:
    """The plain, token-free Web UI URL (the daemon serves it when running)."""
    from alle.webui import server as webui_server

    return webui_server.ui_url()


def ensure_web_ui(timeout: float = 6.0) -> bool:
    """Ensure the daemon is running and serving the Web UI. True if reachable.

    Starts the daemon if needed, then waits for its control server to accept
    connections (it starts asynchronously) so the browser never opens on a dead
    port.
    """
    from alle.webui import server as webui_server

    daemon.ensure_running()
    return webui_server.wait_until_serving(timeout)


def web_ui_login_url() -> str:
    """A one-time login URL for opening the Web UI (used by ``alle ui``)."""
    from alle.webui import server as webui_server

    return webui_server.mint_login_url()


def _daemon_info() -> dict:
    """The applier daemon's status + CLI↔daemon version skew for ``alle status``."""
    info = daemon.daemon_info()
    dv = info.get("version") if info else None
    return {
        "running": info is not None,
        "version": dv,
        "installed_version": daemon.installed_version(),
        "cli_version": __version__,
        "skew": bool(dv and dv != __version__),
        "service_installed": daemonctl.is_installed(),
    }


def _test_row(channel, probe: dict) -> dict:
    healthy = bool(probe.get("ok"))
    if healthy:
        state = "Healthy"
        latency = probe.get("latency_ms")
        ip = probe.get("ip") or None
        error = None
    else:
        state = "Stopped" if probe.get("error") == "stopped" else "Failed"
        latency = None
        ip = None
        error = probe.get("error") or "probe failed"
    return {
        "provider": channel.provider,
        "display_provider": display_name(channel.provider),
        "name": channel.id,
        "label": channel.label,
        "port": f":{channel.port}",
        "port_number": channel.port,
        "country": _country_display(channel),
        "city": _city_display(channel),
        "healthy": healthy,
        "state": state,
        "latency_ms": latency,
        "ip": ip,
        "error": error,
        "probe": probe,
        "speed_result": None,
    }


def _skipped_speed(reason: str) -> dict:
    return {
        "tested": False,
        "skip_reason": reason,
        "latency_ms": None,
        "download_bps": None,
        "upload_bps": None,
    }


def test(speed: bool = False, channel: str | None = None, progress=None) -> dict:
    """Actively probe channels, optionally speed-test the healthy ones.

    ``channel`` filters by channel id across providers. Speed testing is gated by
    the fresh probe result from this invocation, not by stale status state.
    """
    store = Store.load()
    channels = store.channels()
    if channel is not None:
        channels = [c for c in channels if c.id == channel]
        if not channels:
            raise ServiceError(f"no channel named {channel!r} (see: alle channels ls).")
    if not channels:
        return {
            "probed": False,
            "reason": "no_channels",
            "speed": speed,
            "filter": channel,
            "running": False,
            "channel_count": 0,
            "healthy_count": 0,
            "failed_count": 0,
            "channels": [],
        }

    results = Engine(store).probe_all(channels)
    rows = [_test_row(ch, results[f"{ch.provider}/{ch.id}"]) for ch in channels]
    running = not rows or any(
        (row["probe"] or {}).get("error") != "stopped" for row in rows
    )

    if speed:
        for row in rows:
            if not row["healthy"]:
                row["speed_result"] = _skipped_speed("unhealthy")
                continue

            def _progress(phase, row=row):
                if progress is not None:
                    progress(row, phase)

            # The probe above already measured latency through this tunnel, so
            # skip throughput.run's own latency phase and reuse that value.
            result = speedtest_run_one(
                row["port_number"], progress=_progress, measure_latency=False
            )
            result["latency_ms"] = row["latency_ms"]
            row["speed_result"] = {"tested": True, "skip_reason": None, **result}

    healthy_count = sum(1 for row in rows if row["healthy"])
    return {
        "probed": True,
        "speed": speed,
        "filter": channel,
        "running": running,
        "channel_count": len(rows),
        "healthy_count": healthy_count,
        "failed_count": len(rows) - healthy_count,
        "channels": rows,
    }


def speedtest_run_one(port: int, progress=None, measure_latency: bool = True) -> dict:
    """Drive one channel's proxy and return its latency/download/upload."""
    return throughput.run(port, progress=progress, measure_latency=measure_latency)


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
    if daemon.in_daemon_process():
        daemon.schedule_lifecycle("stop")
        applog.log("stop requested from web ui")
        return {"was_running": True, "stopping": True}
    was_running = _stop_all()
    applog.log("stop")
    return {"was_running": was_running}


def restart() -> dict:
    if daemon.in_daemon_process():
        cleared = Store.load().clear_reconnect_all()
        daemon.schedule_lifecycle("restart")
        applog.log(
            f"restart requested from web ui (cleared reconnect state for {cleared} channel(s))"
        )
        return {"reconnect_cleared": cleared, "restarting": True}
    _stop_all()
    # A manual restart is the user's cue that they've dealt with whatever broke,
    # so clear any give-up flags and let dead channels be retried from scratch.
    cleared = Store.load().clear_reconnect_all()
    daemon.ensure_running()
    applog.log(f"restart (cleared reconnect state for {cleared} channel(s))")
    return {"reconnect_cleared": cleared}


def logs_tail(lines: int = 200) -> str:
    return applog.tail(lines)


# ---- daemon service (login-service install) ----------------------------------


def daemon_install(linger: bool = False) -> dict:
    """Register the daemon as a user-level login service (see alle.daemonctl).

    Stops any manually-spawned daemon first so the freshly-installed supervisor
    is the sole owner, then hands off to it.
    """
    try:
        daemon.stop()  # a hand-spawned daemon would double up with the service
        result = daemonctl.install(linger=linger)
    except daemonctl.DaemonCtlError as e:
        raise ServiceError(str(e)) from e
    daemon.ensure_running()  # now routes through the supervisor
    return result


def daemon_uninstall() -> dict:
    """Remove the login service (state under ~/.alle is left intact)."""
    try:
        return daemonctl.uninstall()
    except daemonctl.DaemonCtlError as e:
        raise ServiceError(str(e)) from e


def daemon_status() -> dict:
    """Login-service + running-daemon status for ``alle daemon status``."""
    return {"service": daemonctl.status(), "daemon": _daemon_info()}
