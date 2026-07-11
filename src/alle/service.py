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
    bundle,
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
    txn,
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
    provider_resolver,
    provider_wg,
)
from alle.state import ReferencedError, Store, channel_id_from_filename


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
    metrics.revive_provider(provider)  # a re-add lifts any metrics tombstone
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
    # Credential + provider entry commit together: the journal rolls the
    # credential back if the state write (the commit point) never happens.
    with txn.setup_transaction(f"add provider {provider}") as t:
        credentials.set_(provider, creds)
        store.add_provider(provider)
        t.commit()
    metrics.revive_provider(provider)  # a re-add lifts any metrics tombstone
    applog.log(f"added provider {provider}")
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "credential_preview": preview(provider, creds),
        "functional": is_functional(provider),
        "updated": False,
        "unchanged": False,
    }


def _resolve_token_channels(
    provider: str, creds: dict
) -> tuple[dict[str, dict], list[str]]:
    """Resolve fresh WireGuard params for every token channel via ``creds``.

    A token channel's ``wg`` is *derived* from (provider, token, location), so a
    new token means the previously-picked servers were selected with a credential
    that may no longer be valid. Resolve each channel afresh through the new
    token, in one pass, reusing the shared resolver (account key derived once).

    Pure staging — writes nothing; the caller commits the results together with
    the credential. A per-channel resolve failure is non-fatal: the channel is
    reported in ``failed``, keeps its existing ``wg`` (so it stays usable), and
    the daemon's probe + auto-reconnect refresh it later — the same healing
    path the bundle apply relies on. Returns ``({cid: wg, …}, [failed ids])``.
    """
    channels = Store.load().provider_channels(provider)
    wg_by_cid: dict[str, dict] = {}
    failed: list[str] = []
    if not channels:
        return wg_by_cid, failed
    try:
        resolve = provider_resolver(provider, creds)
    except ProviderError:
        # The token just validated, so a resolver-build failure here is unusual;
        # treat every channel as "couldn't refresh" rather than crash the update.
        return wg_by_cid, [ch.id for ch in channels]
    for ch in channels:
        try:
            wg_by_cid[ch.id] = resolve(ch.country or "", ch.city or "")
        except ProviderError:
            failed.append(ch.id)
    return wg_by_cid, failed


def provider_update_token(provider: str, creds: dict) -> dict:
    """Replace an already-added token provider's credential, then refresh its
    channels.

    The new token is validated **before** anything is written, so a bad token
    leaves the old one in place (the failure surfaces as a ``ProviderError``).
    Every dependent token channel is then re-resolved through the new token
    (see :func:`_resolve_token_channels`) *before* the write, and the
    credential + all re-resolved channels commit in one setup transaction —
    a crash can never persist the new token with only some of its derived
    channels. Config providers have no token and are rejected.
    """
    store = Store.load()
    if not store.has_provider(provider):
        raise ServiceError(
            f"{display_name(provider)} is not added — run `alle providers add {provider}` first."
        )
    if kind(provider) == "config":
        raise ServiceError(
            f"{display_name(provider)} imports channels from a WireGuard .conf; "
            "it has no token to replace."
        )
    # Re-entering the identical token is a no-op: don't rewrite the file or churn
    # every channel through a needless re-resolve — just report "unchanged".
    if _same_credential(provider, creds):
        return {
            "provider": provider,
            "display_name": display_name(provider),
            "credential_preview": preview(provider, creds),
            "functional": is_functional(provider),
            "updated": False,
            "unchanged": True,
            "channels": {"resolved": [], "failed": []},
        }
    if is_functional(provider):
        validate_provider_credentials(provider, creds)  # raises → old token kept
    # Stage first (network, no writes), then commit credential + every
    # re-resolved channel together: one state transaction is the commit point,
    # and the setup journal rolls the credential back if it never happens.
    wg_by_cid, failed = _resolve_token_channels(provider, creds)
    with txn.setup_transaction(f"update {provider} token") as t:
        credentials.set_(provider, creds)
        resolved = Store.load().update_channels_wg(provider, wg_by_cid)
        t.commit()
    channels = {"resolved": resolved, "failed": failed}
    daemon.ensure_running()
    applog.log(
        f"updated provider {provider} token "
        f"({len(channels['resolved'])} channel(s) re-resolved, "
        f"{len(channels['failed'])} unchanged)"
    )
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "credential_preview": preview(provider, creds),
        "functional": is_functional(provider),
        "updated": True,
        "unchanged": False,
        "channels": channels,
    }


def _same_credential(provider: str, creds: dict) -> bool:
    """True when ``creds`` matches what's already stored for ``provider`` (after the
    same whitespace-stripping :func:`credentials.set_` applies), so replacing it
    would change nothing."""
    stored = credentials.get(provider)
    return bool(stored) and credentials.clean(creds) == stored


def provider_add_or_update_token(provider: str, creds: dict) -> dict:
    """One entry point for both surfaces: add the provider if new, or replace its
    token (and refresh channels) if it already exists. Returns the same shape as
    the underlying primitive, with ``updated`` telling the two cases apart."""
    if Store.load().has_provider(provider):
        return provider_update_token(provider, creds)
    return provider_add_token(provider, creds)


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
                "has_token": kind(provider) == "token" and bool(creds),
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


def provider_remove_many(providers: list[str], dry_run: bool = False) -> dict:
    planned = _provider_removal_plan(providers)
    if dry_run:
        return {"providers": planned, "dry_run": True}

    names = [item["provider"] for item in planned]
    # All-or-nothing: credentials go first (journalled — rolled back if the
    # state removal fails), then the whole batch is removed in ONE state
    # transaction, the commit point. The setup lock also means a concurrent
    # `providers add` cannot interleave and resurrect a half-removed provider.
    with txn.setup_transaction(f"remove provider(s) {', '.join(names)}") as t:
        for provider in names:
            credentials.remove(provider)
        try:
            Store.load().remove_providers(names)
        except ReferencedError as e:  # rule added between plan and removal
            raise _blockers_error(e.blockers) from e
        t.commit()
    for item in planned:
        metrics.remove_provider(item["provider"])
        applog.log(
            f"removed provider {item['provider']} "
            f"({item['channels_removed']} channel(s))"
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
    metrics.revive_channel(provider, channel.id)  # same identity re-created
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
    # (keys may have rotated) rather than creating wg_..._2. Snapshot the existing
    # channel first (by the same slugged id upsert will use) so we can tell a real
    # update from a byte-identical no-op.
    existing = store.get_channel(provider, channel_id_from_filename(stem))
    unchanged = existing is not None and _conf_channel_unchanged(
        existing, country, city, wg, label
    )
    channel, created = store.upsert_channel(provider, stem, country, city, wg, label)
    metrics.revive_channel(provider, channel.id)  # same identity re-created
    action = "imported" if created else ("unchanged" if unchanged else "updated")  # noqa: S3358
    applog.log(
        f"{action} channel {provider}/{channel.id} from {filename} on :{channel.port}"
    )
    daemon.ensure_running()
    return {
        "provider": provider,
        "display_name": display_name(provider),
        "channel": channel,
        "imported_from": filename,
        "updated": not created and not unchanged,
        "unchanged": unchanged,
    }


def _conf_channel_unchanged(
    existing, country: str, city: str, wg: dict, label: str
) -> bool:
    """True when re-importing a ``.conf`` would change nothing about the channel —
    same parsed location and WireGuard params, and no new label. Used to warn that
    the channel already exists instead of reporting a silent 'updated' no-op."""
    if (existing.country, existing.city) != (country, city):
        return False
    if existing.wg != wg:
        return False
    # A label is only applied when non-empty; an empty label never changes state.
    return not (label and label != existing.label)


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
    filters to one channel by bare id or ``provider/channel`` ref; a bare id
    matching channels under more than one provider is rejected.
    """
    store = Store.load()
    stored = metrics.totals()
    wanted = _resolve_channel_filter(store, channel)
    rows = []
    for ch in store.channels():
        if wanted and (ch.provider, ch.id) not in wanted:
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


def _resolve_channel_filter(store: Store, channel: str | None) -> set[tuple[str, str]]:
    """Resolve a ``--channel`` filter to the ``(provider, id)`` set it names.

    Accepts a bare id or a qualified ``provider/channel`` ref. A bare id that
    matches channels under more than one provider is rejected — channel ids are
    unique only within a provider, so a bare id operating across providers would
    silently touch the wrong channel. An explicit glob may span providers
    (the pattern makes the intent unambiguous). Empty/None → every channel.
    """
    if not channel:
        return set()
    rows = _resolve_channel_ref(store, channel, None)
    return {(r["provider"], r["channel"]) for r in rows}


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

    # The whole batch is removed in ONE state transaction (all-or-nothing):
    # a rule added between plan and removal blocks the batch, never half of it.
    try:
        Store.load().remove_channels(
            [(item["provider"], item["channel"]) for item in planned]
        )
    except ReferencedError as e:
        raise _blockers_error(e.blockers) from e
    for item in planned:
        metrics.remove_channel(item["provider"], item["channel"])
        applog.log(f"removed channel {item['provider']}/{item['channel']}")
    daemon.ensure_running()
    return {"channels": planned, "dry_run": False}


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


def _normalize_target(target: str) -> str:
    try:
        _, ref = routes.parse_target(target)
    except routes.RuleError as e:
        raise ServiceError(str(e)) from e
    if ref is None:
        return target.strip()
    provider = match(ref[0])
    if provider is None:
        names = ", ".join(known())
        raise ServiceError(f"unknown provider {ref[0]!r} in target (known: {names}).")
    return f"{provider}/{ref[1]}"


def _normalize_matchers(entries: list) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in entries:
        if isinstance(entry, dict):
            value = str(entry.get("value", ""))
            explicit = entry.get("type") or entry.get("matcher_type")
            matcher_type = str(explicit) if explicit else None
        else:
            value = str(entry)
            matcher_type = None
        try:
            out.append(routes.infer_matcher(value, matcher_type))
        except routes.RuleError as e:
            raise ServiceError(str(e)) from e
    if not out:
        raise ServiceError("at least one matcher is required.")
    return out


def _decorate_ruleset(block: dict, shadows: dict[str, str]) -> dict:
    rules_ = [_decorate_rule(rule, shadows) for rule in block["rules"]]
    return {
        "id": block["id"],
        "name": block["name"],
        "target": block["target"],
        "rules": rules_,
        "matcher_count": len(rules_),
        "shadowed_count": sum(1 for rule in rules_ if rule.get("shadowed_by")),
    }


def _routes_payload(
    store: Store, channel: str | None = None, flat: bool = False
) -> dict:
    rules_ = store.rules()
    shadows = routes.shadowed_by(rules_)
    # The built-in LAN-direct block sits ahead of every user rule; flag any user
    # CIDR rule it covers (only relevant when lan_direct is on).
    if store.router.get("lan_direct", True):
        for rule in rules_:
            if rule["id"] not in shadows and routes.shadowed_by_lan_direct(rule):
                shadows[rule["id"]] = routes.LAN_DIRECT_SHADOW
    rows = []
    for rule in rules_:
        if channel is not None:
            _, _, cid = rule["target"].partition("/")
            if not cid:
                continue
            if rule["target"] != channel and cid != channel:
                continue
        rows.append(_decorate_rule(rule, shadows))
    rulesets = []
    if channel is None:
        rulesets = [_decorate_ruleset(block, shadows) for block in store.rulesets()]
    return {
        "rules": rows,
        "rulesets": rulesets,
        "filter": channel,
        "flat": flat,
        "router": _router_info(store),
    }


def routes_ruleset_create(name: str, target: str, matchers: list) -> dict:
    """Create one ruleset atomically with one or more matchers."""
    name = (name or "").strip()
    if not name:
        raise ServiceError("ruleset name cannot be empty.")
    target = _normalize_target(target)
    normalized = _normalize_matchers(matchers)
    store = Store.load()
    try:
        block = store.create_ruleset(name, target, normalized)
    except ValueError as e:
        raise ServiceError(f"{e} (see: alle channels ls --refs).") from e
    shadows = routes.shadowed_by(store.rules())
    decorated = _decorate_ruleset(block, shadows)
    applog.log(
        f"created ruleset {decorated['id']} {decorated['name']!r}: "
        f"{decorated['matcher_count']} matcher(s) -> {target}"
    )
    daemon.ensure_running()
    return {"ruleset": decorated, "router": _router_info(store)}


def routes_ruleset_add(ruleset_id: str, matchers: list) -> dict:
    normalized = _normalize_matchers(matchers)
    store = Store.load()
    try:
        block = store.add_ruleset_matchers(ruleset_id, normalized)
    except ValueError as e:
        raise ServiceError(str(e)) from e
    shadows = routes.shadowed_by(store.rules())
    decorated = _decorate_ruleset(block, shadows)
    applog.log(
        f"added {len(normalized)} matcher(s) to ruleset {decorated['id']} "
        f"{decorated['name']!r}"
    )
    daemon.ensure_running()
    return {"ruleset": decorated, "router": _router_info(store)}


def routes_ruleset_remove(ruleset_id: str, dry_run: bool = False) -> dict:
    store = Store.load()
    block = next((b for b in store.rulesets() if b["id"] == ruleset_id), None)
    if block is None:
        raise ServiceError(f"unknown ruleset {ruleset_id!r} (see: alle routes ls).")
    shadows = routes.shadowed_by(store.rules())
    planned = _decorate_ruleset(block, shadows)
    if dry_run:
        return {"ruleset": planned, "dry_run": True}
    try:
        store.remove_ruleset(ruleset_id)
    except ValueError as e:
        raise ServiceError(str(e)) from e
    applog.log(f"removed ruleset {planned['id']} {planned['name']!r}")
    daemon.ensure_running()
    return {"ruleset": planned, "dry_run": False}


def routes_ruleset_rename(ruleset_id: str, name: str) -> dict:
    store = Store.load()
    try:
        block = store.rename_ruleset(ruleset_id, name.strip())
    except ValueError as e:
        raise ServiceError(str(e)) from e
    shadows = routes.shadowed_by(store.rules())
    decorated = _decorate_ruleset(block, shadows)
    return {"ruleset": decorated, "router": _router_info(store)}


def routes_ruleset_retarget(ruleset_id: str, target: str) -> dict:
    target = _normalize_target(target)
    store = Store.load()
    try:
        block = store.retarget_ruleset(ruleset_id, target)
    except ValueError as e:
        raise ServiceError(f"{e} (see: alle channels ls --refs).") from e
    shadows = routes.shadowed_by(store.rules())
    decorated = _decorate_ruleset(block, shadows)
    daemon.ensure_running()
    return {"ruleset": decorated, "router": _router_info(store)}


def routes_ruleset_update(
    ruleset_id: str, name: str, target: str, matchers: list
) -> dict:
    """Update one ruleset's name, target, and matchers (id/position kept)."""
    target = _normalize_target(target)
    normalized = _normalize_matchers(matchers)
    name = (name or "").strip()
    store = Store.load()
    try:
        block = store.update_ruleset(ruleset_id, name, target, normalized)
    except ValueError as e:
        raise ServiceError(str(e)) from e
    shadows = routes.shadowed_by(store.rules())
    decorated = _decorate_ruleset(block, shadows)
    applog.log(f"updated ruleset {ruleset_id}: {name!r} via {target}")
    daemon.ensure_running()
    return {"ruleset": decorated, "router": _router_info(store)}


def routes_list(channel: str | None = None, flat: bool = False) -> dict:
    """Rulesets in evaluation order, plus flat rows for debugging/API clients."""
    return _routes_payload(Store.load(), channel=channel, flat=flat)


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


def routes_reorder(ids: list[str], flat: bool = False) -> dict:
    """Replace ruleset-block order (or flat rule order for debugging)."""
    if not ids:
        what = "rule" if flat else "ruleset"
        raise ServiceError(f"at least one {what} id is required (see: alle routes ls).")
    store = Store.load()
    try:
        if flat:
            ordered, changed = store.reorder_rules(ids)
            shadows = routes.shadowed_by(ordered)
            payload = {
                "rules": [_decorate_rule(rule, shadows) for rule in ordered],
                "rulesets": [
                    _decorate_ruleset(block, shadows) for block in store.rulesets()
                ],
            }
        else:
            ordered_sets, changed = store.reorder_rulesets(ids)
            shadows = routes.shadowed_by(store.rules())
            payload = {
                "rulesets": [
                    _decorate_ruleset(block, shadows) for block in ordered_sets
                ],
                "rules": [_decorate_rule(rule, shadows) for rule in store.rules()],
            }
    except ValueError as e:
        noun = "rule" if flat else "ruleset"
        raise ServiceError(f"{e} (pass every {noun} id exactly once).") from e
    if changed:
        applog.log("reordered routes: " + " ".join(ids))
        daemon.ensure_running()
    return {**payload, "changed": changed, "router": _router_info(store)}


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


def setup_export() -> dict:
    """The whole setup as a declarative bundle (text + counts for the summary).

    The text contains WireGuard private keys and provider tokens — callers
    must treat it as a secret.
    """
    data = bundle.export_bundle()
    return {
        "text": bundle.dumps(data),
        "providers": len(data["providers"]),
        "channels": sum(len(e["channels"]) for e in data["providers"].values()),
        "rulesets": len(data["router"]["rulesets"]),
    }


def setup_import(text: str) -> dict:
    """Merge a bundle into the current setup (validate-all, then apply)."""
    try:
        summary = bundle.apply_import(text)
    except bundle.BundleError as e:
        raise ServiceError(str(e)) from e
    ch = summary["channels"]
    applog.log(
        f"imported bundle: +{len(ch['created'])} channel(s), "
        f"{len(ch['updated'])} updated, {len(summary['rulesets_added'])} ruleset(s), "
        f"{len(summary['wg_resolved'])} resolved fresh, "
        f"{len(summary['wg_fallback'])} from snapshot"
    )
    daemon.ensure_running()
    return summary


def setup_restore_plan(text: str) -> dict:
    """Validate a bundle for restore and report both sides' counts — the
    caller's confirmation step. Changes nothing."""
    try:
        return bundle.plan_restore(text)
    except bundle.BundleError as e:
        raise ServiceError(str(e)) from e


def setup_restore(text: str) -> dict:
    """Replace the entire setup with a bundle. Destructive — callers confirm
    (the CLI prompts / requires ``--yes``; the Web UI shows a dialog)."""
    try:
        summary = bundle.apply_restore(text)
    except bundle.BundleError as e:
        raise ServiceError(str(e)) from e
    applog.log(
        f"restored bundle: {len(summary['providers'])} provider(s), "
        f"{len(summary['channels'])} channel(s), {len(summary['rulesets'])} "
        f"ruleset(s); removed {len(summary['removed']['channels'])} channel(s); "
        f"{len(summary['wg_resolved'])} resolved fresh, "
        f"{len(summary['wg_fallback'])} from snapshot"
    )
    daemon.ensure_running()
    return summary


def _bundle_location_lookup():
    """A ``(provider) -> {country_lower: {city_lower, …}} | None`` lookup for
    bundle validation, plus a list of notes for locations we couldn't reach.

    Cached per call; a provider whose location list can't be fetched (offline,
    API down) returns None, so validation skips its country/city check rather
    than failing on an environment problem."""
    cache: dict[str, dict | None] = {}
    notes: list[str] = []

    def lookup(provider: str):
        if provider not in cache:
            spec = PROVIDERS.get(provider)
            if not spec or "locations" not in spec:
                cache[provider] = None
            else:
                try:
                    locs = spec["locations"]()  # {Country: [cities]}
                    cache[provider] = {
                        c.lower(): {city.lower() for city in cities}
                        for c, cities in locs.items()
                    }
                except ProviderError as e:
                    notes.append(
                        f"could not reach {display_name(provider)} to validate its "
                        f"countries/cities ({e}); skipped that check"
                    )
                    cache[provider] = None
        return cache[provider]

    return lookup, notes


def setup_validate(text: str) -> dict:
    """Validate a bundle file against every rule without applying it. Raises
    :class:`ServiceError` (line-annotated) if anything is wrong; otherwise
    returns the counts and any location-check notes."""
    lookup, notes = _bundle_location_lookup()
    try:
        parsed = bundle.validate_file(text, location_lookup=lookup)
    except bundle.BundleError as e:
        raise ServiceError(str(e)) from e
    return {
        "valid": True,
        "providers": len(parsed["providers"]),
        "channels": sum(len(e["channels"]) for e in parsed["providers"].values()),
        "rulesets": len(parsed["router"]["rulesets"] or []),
        "notes": notes,
    }


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
        "runtime": (info or {}).get("runtime"),
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


def test(
    speed: bool = False,
    channel: str | None = None,
    progress=None,
    on_row=None,
    on_begin=None,
    cancel=None,
) -> dict:
    """Actively probe channels, optionally speed-test the healthy ones.

    ``channel`` filters by channel id across providers. Speed testing is gated by
    the fresh probe result from this invocation, not by stale status state.

    Two optional streaming callbacks let a caller reveal results as each channel
    finishes instead of only in the final aggregate:

    - ``on_begin(chans)`` fires once, right after the to-test channel list is
      resolved (before probing), with ``[{"provider","name","label","port",
      "port_number"}, …]`` — enough to size/preview output before any result.
    - ``on_row(row)`` fires after each channel is fully done (probe, and — when
      ``speed`` — its download/upload test), with that channel's completed row,
      the same dict that ends up in the returned ``channels`` list.
    """
    store = Store.load()
    channels = store.channels()
    if channel is not None:
        # Resolve a bare id or provider/channel ref, rejecting an id that
        # matches channels under more than one provider (channel ids are only
        # unique within a provider).
        wanted = _resolve_channel_filter(store, channel)
        channels = [c for c in channels if (c.provider, c.id) in wanted]
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

    if on_begin is not None:
        on_begin(
            [
                {
                    "provider": c.provider,
                    "name": c.id,
                    "label": c.label,
                    "port": f":{c.port}",
                    "port_number": c.port,
                    "country": _country_display(c),
                    "city": _city_display(c),
                }
                for c in channels
            ]
        )

    results = Engine(store).probe_all(channels)
    rows = [_test_row(ch, results[f"{ch.provider}/{ch.id}"]) for ch in channels]
    running = not rows or any(
        (row["probe"] or {}).get("error") != "stopped" for row in rows
    )

    if speed:
        for row in rows:
            # A streaming caller that disconnected sets cancel(); stop starting
            # new per-channel transfers rather than driving the dead socket.
            if cancel and cancel():
                row["speed_result"] = _skipped_speed("cancelled")
                if on_row is not None:
                    on_row(row)
                continue
            if not row["healthy"]:
                row["speed_result"] = _skipped_speed("unhealthy")
            else:

                def _progress(phase, row=row):
                    if progress is not None:
                        progress(row, phase)

                # The probe above already measured latency through this tunnel, so
                # skip throughput.run's own latency phase and reuse that value.
                result = speedtest_run_one(
                    row["port_number"],
                    progress=_progress,
                    measure_latency=False,
                    cancel=cancel,
                )
                result["latency_ms"] = row["latency_ms"]
                row["speed_result"] = {"tested": True, "skip_reason": None, **result}

            if on_row is not None:
                on_row(row)
    elif on_row is not None:
        for row in rows:
            on_row(row)

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


def speedtest_run_one(
    port: int, progress=None, measure_latency: bool = True, cancel=None
) -> dict:
    """Drive one channel's proxy and return its latency/download/upload."""
    return throughput.run(
        port, progress=progress, measure_latency=measure_latency, cancel=cancel
    )


def _stop_all() -> bool:
    runner = singbox.Runner()
    was_singbox = runner.is_running()
    was_applier = daemon.stop()
    # Always run the (idempotent) stop rather than gating it on the earlier
    # liveness answer — a transiently unreadable identity check must not
    # leave a live sing-box behind.
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
    # A manual restart is the user's cue that they've dealt with whatever broke,
    # so clear any give-up flags and let dead channels be retried from scratch.
    cleared = Store.load().clear_reconnect_all()
    if daemonctl.is_installed():
        # One atomic manager restart instead of stop+start: no window where
        # the stop landed but the start was lost, and nothing for KeepAlive/
        # Restart= to resurrect mid-sequence. sing-box is stopped explicitly
        # first so the tunnels bounce deterministically on every platform
        # (launchd kickstart only recycles the daemon job itself).
        singbox.Runner().stop()
        if not daemonctl.restart_service():  # unit vanished behind our back
            daemon.ensure_running()
    else:
        _stop_all()
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
