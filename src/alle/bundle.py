"""The declarative setup bundle: export, validate, and apply (import/restore).

A bundle is one YAML file describing the entire setup — providers (with their
credential), channels of both archetypes, rulesets, and the router toggles —
so a configuration can be backed up, moved to another machine, replayed after
a reinstall, or hand-written from scratch as a startup config. It is
declarative: ``alle export`` is a convenience that emits the declarative form
of the live setup, not a state dump.

Deliberately excluded from *exports*: everything runtime. Probe results,
reconnect bookkeeping, traffic/speed history, rule/ruleset ids, and
auto-assigned ports — those are local allocations, so applying a bundle
re-allocates them and apps pointing at old port numbers must be repointed.
(A hand-written bundle *may* carry explicit ``port:`` declarations — those
apply as written and clash loudly; see the validate/apply paths.) Secrets are
*included* in exports (WireGuard private keys, provider tokens), so the file
itself is a secret and callers write it ``0600``; a hand-written bundle can
instead reference its secrets indirectly (``token_env``/``token_file``).

Two apply modes share the format and one validation pass:

* import = merge/upsert by ``(provider, channel id)`` — the globally unique
  handle. Bundle rulesets append at the *bottom* of the priority order: under
  first-match-wins an appended block can never hijack existing routing.
* restore = replace the whole setup (destructive; callers confirm first).

Validation is all-or-nothing: every entry is checked before anything mutates,
and failures come back as one per-entry list (path + reason) — the same
pattern as restrict-only removal and ruleset validation. Network resolution
also runs before the first mutation, so a mid-apply failure cannot
half-apply a bundle.

Token providers must carry their credential (the access token) — validation
rejects a token provider without one, since alle needs it to resolve servers
and to add channels. Token-channel ``wg`` is derived state, and treated
accordingly at apply (see :func:`_resolve_token_wg`): an existing channel with
the same location keeps its live params, a new identity resolves a fresh
server via the token, and the bundle's snapshot is only the fallback that
keeps a restore working when fresh resolution *fails* (offline, API down,
token rejected). Config channels' ``wg`` *is* their configuration — required
in the bundle, restored verbatim, refreshed only by re-importing a newly
downloaded ``.conf``.

Channels round-trip their administrative ``enabled`` state. Exports write it
**explicitly on every channel** (a bundle reader must never need to know any
absent-key rule — same discipline as ``lan_direct``); a hand-written channel
may omit it, and the meaning is per apply mode: on **import (merge)** an
omitted ``enabled`` leaves an existing channel's state untouched — like the
unstated router toggles, so an ad-hoc ``channels disable`` survives a
re-import (e.g. the Docker entrypoint re-applying the bundle on every
container start) — while a new channel defaults to enabled; on **restore**
(a whole-setup replace) omitted means enabled. A **disabled** channel is
imported without ever touching the provider: no server resolution (no API
call to pick one) and no probe — its country/city are instead checked against
the provider's location catalog when reachable. It keeps the live params of
an existing same-location channel, else the bundle's ``wg`` snapshot, else it
lands wg-less and a later ``alle channels enable`` resolves it. A bundle
ruleset can never target a channel the same bundle disables, mirroring the
live restrict-only invariant.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

from alle import credentials, metrics, routes, txn, wgconf
from alle.providers import (
    ProviderError,
    WireGuardResolver,
    auth_fields,
    display_name,
    is_functional,
    kind,
    known,
    provider_resolver,
)
from alle.state import PortInUseError, ReferencedError, Store, _slug

BUNDLE_KIND = "alle-bundle"
BUNDLE_VERSION = 1


class BundleError(Exception):
    """The bundle was rejected as a whole; nothing was changed.

    ``entries`` holds every problem as ``(path, reason)`` pairs — all
    blockers in one pass, never one-error-per-attempt. ``line_index`` maps a
    path to the 1-based source line so the message can point at it.
    """

    def __init__(self, entries, line_index: dict | None = None):
        self.entries = [(p, r) for p, r in entries]
        self.line_index = dict(line_index or {})
        noun = "problem" if len(self.entries) == 1 else "problems"
        head = f"bundle rejected ({len(self.entries)} {noun}) — nothing was changed:"
        rows = []
        for path, reason in self.entries:
            line = _nearest_line(path, self.line_index)
            rows.append((line if line is not None else 1 << 30, path, reason, line))
        rows.sort(key=lambda t: (t[0], t[1]))
        body = [
            (
                f"  line {line}  {path} — {reason}"
                if line is not None
                else f"  {path} — {reason}"
            )
            for _, path, reason, line in rows
        ]
        super().__init__("\n".join([head, *body]))


def _line_index(text: str) -> dict[str, int]:
    """Map each dotted path (``providers.nordvpn.channels.us_1.wg.private_key``,
    ``router.rulesets[0].target``) to the 1-based line its key sits on, so
    validation errors can name a line. Best-effort: unparseable YAML yields {}."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return {}
    index: dict[str, int] = {}

    def walk(node, path):
        if node is None:
            return
        if path and path not in index:
            index[path] = node.start_mark.line + 1
        if isinstance(node, yaml.MappingNode):
            for key_node, val_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                child = f"{path}.{key_node.value}" if path else str(key_node.value)
                index.setdefault(child, key_node.start_mark.line + 1)
                walk(val_node, child)
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                child = f"{path}[{i}]"
                index.setdefault(child, item.start_mark.line + 1)
                walk(item, child)

    walk(root, "")
    return index


def _nearest_line(path: str, index: dict[str, int]) -> int | None:
    """The line for ``path``, or the nearest ancestor's when the exact path
    isn't in the file (e.g. a required-but-missing field)."""
    while path:
        if path in index:
            return index[path]
        cut = max(path.rfind("."), path.rfind("["))
        if cut <= 0:
            break
        path = path[:cut]
    return index.get(path)


def _duplicate_key_errors(text: str) -> list[tuple[str, str, int]]:
    """YAML silently keeps the last of duplicate keys, so channel ids (or any
    key) repeated in one mapping would collapse unseen. Report each duplicate
    with the line of its *second* occurrence."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return []
    out: list[tuple[str, str, int]] = []

    def walk(node, path):
        if isinstance(node, yaml.MappingNode):
            seen: set[str] = set()
            for key_node, val_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                key = str(key_node.value)
                child = f"{path}.{key}" if path else key
                if key in seen:
                    if path.startswith("providers.") and path.endswith(".channels"):
                        reason = (
                            f"duplicate channel id {key!r} — ids must be unique "
                            "within a provider"
                        )
                    else:
                        reason = f"duplicate key {key!r}"
                    out.append((child, reason, key_node.start_mark.line + 1))
                seen.add(key)
                walk(val_node, child)
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                walk(item, f"{path}[{i}]")

    walk(root, "")
    return out


# ---- export --------------------------------------------------------------------


def export_bundle() -> dict:
    """The live setup as a bundle dict (see :func:`dumps` for the file form)."""
    store = Store.load()
    providers_out: dict[str, dict] = {}
    for provider in store.provider_names():
        entry: dict = {}
        creds = credentials.get(provider)
        if creds:
            entry["credential"] = dict(creds)
        entry["channels"] = {
            ch.id: _export_channel(ch) for ch in store.provider_channels(provider)
        }
        providers_out[provider] = entry
    router = store.router
    return {
        "kind": BUNDLE_KIND,
        "bundle_version": BUNDLE_VERSION,
        "providers": providers_out,
        "router": {
            "killswitch": bool(router.get("killswitch")),
            # written explicitly: a bundle reader must never need to know
            # state.json's absent-means-on inheritance rule
            "lan_direct": bool(router.get("lan_direct", True)),
            "rulesets": [
                {
                    "name": block["name"],
                    "target": block["target"],
                    "matchers": [_export_matcher(r) for r in block["rules"]],
                }
                for block in store.rulesets()
            ],
        },
    }


def _export_channel(ch) -> dict:
    # `enabled` is written explicitly (never relying on an absent-key rule):
    # on a merge, an *omitted* key means "leave the channel's state alone",
    # so an export — a faithful backup — must always state it.
    out: dict = {"country": ch.country, "city": ch.city, "enabled": ch.enabled}
    if ch.label:
        out["label"] = ch.label
    if ch.wg:
        out["wg"] = copy.deepcopy(ch.wg)
    return out


def _export_matcher(rule: dict) -> dict:
    if rule["type"] == "all":  # the catch-all has no value
        return {"type": "all"}
    return {"type": rule["type"], "value": rule["value"]}


def dumps(data: dict) -> str:
    """Serialize a bundle dict to its YAML file form, with a header comment."""
    lines = [
        "# alle setup bundle — apply with `alle import <file>` (merge into the",
        "# current setup) or `alle import <file> --replace (overwrite it).",
        "# Contains WireGuard private keys and provider tokens — keep it private.",
    ]
    body = yaml.safe_dump(
        data, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    return "\n".join(lines) + "\n" + body


# ---- parse + validate ------------------------------------------------------------


def loads(text: str) -> dict:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise BundleError([("bundle", f"not valid YAML: {e}")]) from e
    if not isinstance(data, dict):
        raise BundleError([("bundle", "not an alle bundle (root is not a mapping)")])
    return data


def validate(
    data: dict,
    *,
    text: str = "",
    extra_channel_refs: frozenset[str] | set[str] = frozenset(),
    stored_credentials_ok: bool = False,
    strict: bool = False,
    location_lookup=None,
    check_locations: str = "all",
) -> dict:
    """Check the whole bundle; raise :class:`BundleError` with every problem.

    Returns the normalized parse: ``{"providers": {provider: {"credential",
    "channels": {id: {"country", "city", "label", "wg", "enabled"}}}},
    "router": {"killswitch", "lan_direct", "rulesets"}}`` where ``wg`` is None
    for resolve-at-apply entries and each router key is None when the bundle
    leaves it unstated.

    ``text`` is the raw YAML, used only to annotate errors with line numbers
    and to catch duplicate keys. ``extra_channel_refs`` are ``provider/cid``
    targets valid beyond the bundle's own channels (the existing setup, on a
    merge). ``stored_credentials_ok`` lets a provider lean on an
    already-stored credential instead of one in the bundle. ``strict`` (a
    self-contained restore/validate) requires the router toggles to be
    explicit when a router block is present. ``location_lookup(provider)``, if
    given, returns ``{country_lower: {city_lower, …}}`` so country/city are
    checked against the provider's real locations; ``check_locations``
    narrows that check to ``"disabled"`` channels only (the apply paths:
    an enabled channel's location is proven by resolution itself, a disabled
    one is never resolved so the catalog check is its only correctness gate).
    The lookup is consulted lazily — a bundle with nothing to check fetches
    no location list.
    """
    line_index = _line_index(text) if text else {}
    errors: list[tuple[str, str]] = []
    for dpath, reason, line in _duplicate_key_errors(text) if text else []:
        errors.append((dpath, reason))
        line_index[dpath] = line  # point at the duplicate, not the first key

    _header_errors(data, errors)

    raw_providers = data.get("providers")
    if raw_providers is None:
        raw_providers = {}
    if not isinstance(raw_providers, dict):
        errors.append(("providers", "must be a mapping of provider -> entry"))
        raw_providers = {}
    parsed_providers: dict[str, dict] = {}
    for provider, entry in raw_providers.items():
        parsed = _validate_provider(
            str(provider),
            entry,
            errors,
            stored_credentials_ok=stored_credentials_ok,
            location_lookup=location_lookup,
            check_locations=check_locations,
        )
        if parsed is not None:
            parsed_providers[str(provider)] = parsed

    channel_refs = {
        f"{provider}/{cid}"
        for provider, entry in parsed_providers.items()
        for cid in entry["channels"]
    } | set(extra_channel_refs)
    # A ruleset may not target a channel this same bundle disables — the
    # bundle-level mirror of the live restrict-only invariant. (A merge
    # targeting an *existing* disabled channel is refused at apply, where the
    # existing setup's enabled state is known.)
    disabled_refs = {
        f"{provider}/{cid}"
        for provider, entry in parsed_providers.items()
        for cid, ch in entry["channels"].items()
        if ch.get("enabled") is False  # explicit only; unstated may be either
    }

    parsed_router: dict = {
        "killswitch": None,
        "lan_direct": None,
        "rulesets": None,
        "port": None,
    }
    raw_router = data.get("router")
    if raw_router is not None and not isinstance(raw_router, dict):
        errors.append(("router", "must be a mapping"))
    elif isinstance(raw_router, dict):
        for key in ("killswitch", "lan_direct"):
            value = raw_router.get(key)
            if value is None:
                if strict:
                    errors.append(
                        (f"router.{key}", "must be set explicitly to true or false")
                    )
                continue
            if not isinstance(value, bool):
                errors.append((f"router.{key}", "must be true or false"))
            else:
                parsed_router[key] = value
        # port stays optional even in strict mode: absent means "keep/allocate
        # locally", the pre-declaration behavior every bundle had.
        parsed_router["port"] = (
            _validate_port(raw_router.get("port"), "router.port", errors) or None
        )
        raw_rulesets = raw_router.get("rulesets")
        if raw_rulesets is not None:
            parsed_router["rulesets"] = _validate_rulesets(
                raw_rulesets, channel_refs, disabled_refs, errors
            )

    # Declared ports must be unique across the whole bundle (channels + the
    # router) — two declarations of one port can never both hold.
    declared: dict[int, str] = {}
    if parsed_router["port"]:
        declared[parsed_router["port"]] = "router.port"
    for provider, entry in parsed_providers.items():
        for cid, ch in entry["channels"].items():
            port = ch.get("port") or 0
            if not port:
                continue
            cpath = f"providers.{provider}.channels.{cid}.port"
            if port in declared:
                errors.append(
                    (cpath, f"port {port} is also declared at {declared[port]}")
                )
            else:
                declared[port] = cpath

    if errors:
        raise BundleError(errors, line_index)
    return {"providers": parsed_providers, "router": parsed_router}


def _header_errors(data: dict, errors: list) -> None:
    if data.get("kind") != BUNDLE_KIND:
        errors.append(("kind", f"not an alle bundle (expected kind: {BUNDLE_KIND})"))
    version = data.get("bundle_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        errors.append(("bundle_version", "missing or not a positive integer"))
    elif version > BUNDLE_VERSION:
        errors.append(
            (
                "bundle_version",
                f"bundle version {version} is newer than this alle understands "
                f"(max {BUNDLE_VERSION}) — upgrade alle",
            )
        )


def _validate_provider(
    provider: str,
    entry,
    errors: list,
    *,
    stored_credentials_ok: bool,
    location_lookup=None,
    check_locations: str = "all",
) -> dict | None:
    path = f"providers.{provider}"
    if provider not in known():
        names = ", ".join(f"{p} ({display_name(p)})" for p in known())
        errors.append((path, f"unknown provider (supported: {names})"))
        return None
    if entry is None:
        entry = {}
    if not isinstance(entry, dict):
        errors.append((path, "must be a mapping"))
        return None
    token_provider = kind(provider) == "token" and is_functional(provider)

    credential = entry.get("credential")
    if credential is not None:
        if kind(provider) == "config":
            errors.append((f"{path}.credential", "config providers have no credential"))
            credential = None
        elif not (
            isinstance(credential, dict)
            and credential
            and all(
                isinstance(k, str) and isinstance(v, str) and v.strip()
                for k, v in credential.items()
            )
        ):
            errors.append(
                (f"{path}.credential", "must be a mapping of non-empty strings")
            )
            credential = None
        else:
            credential = {k: v.strip() for k, v in credential.items()}
            credential = _resolve_credential(credential, f"{path}.credential", errors)
            missing = [
                f.key for f in auth_fields(provider) if not credential.get(f.key)
            ]
            if missing:
                errors.append(
                    (f"{path}.credential", f"missing field(s): {', '.join(missing)}")
                )
    # Token providers must carry their credential — unless one is already
    # stored (a merge that keeps the existing token).
    if token_provider and credential is None:
        stored = stored_credentials_ok and credentials.get(provider) is not None
        if not stored:
            keys = ", ".join(f.key for f in auth_fields(provider)) or "token"
            errors.append(
                (
                    f"{path}.credential",
                    f"a non-empty {keys} is required for {display_name(provider)}",
                )
            )

    # Lazy: the location list (a possible network fetch) is only pulled when a
    # channel actually needs the check — e.g. an import whose channels are all
    # enabled never fetches it.
    locations_memo: list = []

    def locations_for():
        if not locations_memo:
            locations_memo.append(
                location_lookup(provider) if location_lookup else None
            )
        return locations_memo[0]

    channels: dict[str, dict] = {}
    raw_channels = entry.get("channels")
    if raw_channels is None:
        raw_channels = {}
    if not isinstance(raw_channels, dict):
        errors.append(
            (f"{path}.channels", "must be a mapping of channel id -> channel")
        )
        raw_channels = {}
    for cid, spec in raw_channels.items():
        cpath = f"{path}.channels.{cid}"
        if not isinstance(cid, str) or cid != _slug(cid):
            errors.append(
                (cpath, "channel id must be a lowercase slug (letters, digits, _)")
            )
            continue
        channel = _validate_channel(
            provider,
            spec,
            cpath,
            errors,
            token_provider=token_provider,
            locations_for=locations_for,
            check_locations=check_locations,
        )
        if channel is not None:
            channels[cid] = channel
    return {"credential": credential, "channels": channels}


def _resolve_credential(credential: dict, path: str, errors: list) -> dict:
    """Settle credential *indirection*: for any field ``k``, a bundle may carry
    ``k`` (inline, as always), ``k_env`` (read an environment variable), or
    ``k_file`` (read a file, e.g. a compose/k8s secret mount) — exactly one.

    Resolution happens at validate time so every missing variable or unreadable
    file is reported up front with the rest of the blockers, and everything
    downstream (apply, credential storage) keeps seeing plain values. Inline
    values keep working unchanged — indirection is opt-in per field.
    """
    resolved: dict[str, str] = {}
    for key, value in credential.items():
        if key.endswith("_env"):
            field_name, source = key[: -len("_env")], "env"
        elif key.endswith("_file"):
            field_name, source = key[: -len("_file")], "file"
        else:
            field_name, source = key, "inline"
        if field_name in resolved:
            errors.append(
                (
                    f"{path}.{key}",
                    f"give exactly one of {field_name}, {field_name}_env, "
                    f"{field_name}_file",
                )
            )
            continue
        if source == "env":
            got = os.environ.get(value)
            if not (got or "").strip():
                errors.append(
                    (f"{path}.{key}", f"environment variable {value!r} is not set")
                )
                continue
            resolved[field_name] = got.strip()
        elif source == "file":
            try:
                got = Path(value).expanduser().read_text()
            except OSError as e:
                errors.append((f"{path}.{key}", f"could not read {value!r}: {e}"))
                continue
            if not got.strip():
                errors.append((f"{path}.{key}", f"{value!r} is empty"))
                continue
            resolved[field_name] = got.strip()
        else:
            resolved[field_name] = value
    return resolved


def _validate_channel(
    provider: str,
    spec,
    path: str,
    errors: list,
    *,
    token_provider: bool,
    locations_for=None,
    check_locations: str = "all",
) -> dict | None:
    if spec is None:
        spec = {}
    if not isinstance(spec, dict):
        errors.append((path, "must be a mapping"))
        return None
    fields: dict[str, str] = {}
    for key in ("country", "city", "label"):
        value = spec.get(key)
        if value is not None and not isinstance(value, str):
            errors.append((f"{path}.{key}", "must be a string"))
            value = ""
        fields[key] = (value or "").strip()

    # Tri-state: True / False / None. None = the bundle leaves it unstated —
    # a merge then keeps an existing channel's state (a new one is enabled),
    # like the unstated router toggles; a restore reads it as enabled.
    enabled = spec.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        errors.append((f"{path}.enabled", "must be true or false"))
        enabled = None

    if token_provider:
        # country is the input the API resolves from — required, and checked
        # against the provider's real list when we have it; city is optional
        # but must be a real city in that country when given. With
        # check_locations="disabled" (the apply paths) only disabled channels
        # are checked: an enabled one is about to be resolved, which proves
        # its location anyway, while a disabled one is deliberately never
        # resolved, so this format check is all the validation it gets.
        if not fields["country"]:
            errors.append((f"{path}.country", "is required for a token provider"))
        elif locations_for is not None and (
            check_locations == "all" or enabled is False
        ):
            locations = locations_for()
            if locations is None:
                pass  # catalog unreachable — the caller noted it; shape-only
            elif (cities := locations.get(fields["country"].lower())) is None:
                errors.append(
                    (
                        f"{path}.country",
                        f"{fields['country']!r} is not a known "
                        f"{display_name(provider)} country",
                    )
                )
            elif fields["city"] and fields["city"].lower() not in cities:
                errors.append(
                    (
                        f"{path}.city",
                        f"{fields['city']!r} is not a known "
                        f"{display_name(provider)} city in {fields['country']}",
                    )
                )

    port = _validate_port(spec.get("port"), f"{path}.port", errors)

    wg = spec.get("wg")
    if wg is not None:
        wg = _validate_wg(wg, f"{path}.wg", errors)
    elif not token_provider:
        errors.append(
            (
                path,
                "wg is required — only a functional token provider (e.g. nordvpn) "
                "can resolve a channel without a snapshot",
            )
        )
    return {**fields, "port": port, "wg": wg, "enabled": enabled}


def _validate_port(value, path: str, errors: list) -> int:
    """An optional explicit port declaration: absent/0 means "let alle
    allocate" (the default, and the only behavior a bundle without ``port``
    keys ever sees); otherwise it must be a real port number."""
    if value is None:
        return 0
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 65535:
        errors.append((path, "must be a port number (1-65535)"))
        return 0
    return value


def _check_wg_key(path: str, value, errors: list) -> str:
    if not (isinstance(value, str) and value.strip()):
        errors.append((path, "is required (a WireGuard key)"))
        return ""
    value = value.strip()
    try:
        wgconf._require_wg_key("key", value)
    except wgconf.ConfError:
        errors.append((path, "is not a valid WireGuard key (32 bytes, base64)"))
    return value


def _str_list(value) -> list[str] | None:
    """A YAML list of non-empty strings (a bare string counts as one entry)."""
    if isinstance(value, str):
        value = [value]
    if not (
        isinstance(value, list)
        and value
        and all(isinstance(v, str) and v.strip() for v in value)
    ):
        return None
    return [v.strip() for v in value]


def _validate_wg(wg, path: str, errors: list) -> dict | None:
    """Same checks the ``.conf`` import path applies, on the dict form."""
    if not isinstance(wg, dict):
        errors.append((path, "must be a mapping of WireGuard parameters"))
        return None
    before = len(errors)

    private_key = _check_wg_key(f"{path}.private_key", wg.get("private_key"), errors)
    address = _str_list(wg.get("address"))
    if address is None:
        errors.append(
            (f"{path}.address", "must be a non-empty list of interface addresses")
        )
    else:
        bad = wgconf.check_addresses(address)
        if bad is not None:
            errors.append(
                (f"{path}.address", f"{bad!r} is not an IP interface address")
            )

    peer = wg.get("peer")
    if not isinstance(peer, dict):
        errors.append((f"{path}.peer", "must be a mapping with the peer parameters"))
        return None
    public_key = _check_wg_key(
        f"{path}.peer.public_key", peer.get("public_key"), errors
    )
    preshared = peer.get("preshared_key") or None
    if preshared is not None:
        preshared = _check_wg_key(f"{path}.peer.preshared_key", preshared, errors)

    host = peer.get("endpoint_host")
    if not (isinstance(host, str) and host.strip() and wgconf.valid_host(host.strip())):
        errors.append(
            (f"{path}.peer.endpoint_host", "must be the server host (IP or DNS name)")
        )
        host = ""
    port = peer.get("endpoint_port")
    if isinstance(port, str) and port.isdigit():
        port = int(port)
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        errors.append((f"{path}.peer.endpoint_port", "must be a port number (1-65535)"))

    allowed = peer.get("allowed_ips")
    if allowed is None:
        allowed = list(wgconf.DEFAULT_ALLOWED_IPS)
    else:
        allowed = _str_list(allowed)
        if allowed is None:
            errors.append(
                (f"{path}.peer.allowed_ips", "must be a non-empty list of CIDRs")
            )
        else:
            bad = wgconf.check_cidrs(allowed)
            if bad is not None:
                errors.append((f"{path}.peer.allowed_ips", f"{bad!r} is not a CIDR"))
    keepalive = peer.get("keepalive", wgconf.WG_KEEPALIVE)
    if isinstance(keepalive, str) and keepalive.isdigit():
        keepalive = int(keepalive)
    if not isinstance(keepalive, int) or isinstance(keepalive, bool) or keepalive < 0:
        errors.append((f"{path}.peer.keepalive", "must be a number of seconds"))

    if len(errors) > before:
        return None
    return {
        "private_key": private_key,
        "address": address,
        "peer": {
            "public_key": public_key,
            "endpoint_host": host.strip(),
            "endpoint_port": port,
            "preshared_key": preshared,
            "allowed_ips": allowed,
            "keepalive": keepalive,
        },
    }


def _validate_rulesets(
    raw, channel_refs: set[str], disabled_refs: set[str], errors: list
) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw, list):
        errors.append(("router.rulesets", "must be a list"))
        return out
    for i, block in enumerate(raw):
        path = f"router.rulesets[{i}]"
        if not isinstance(block, dict):
            errors.append((path, "must be a mapping with name/target/matchers"))
            continue
        name = str(block.get("name") or "").strip()
        if not name:
            errors.append((f"{path}.name", "ruleset name cannot be empty"))

        target = str(block.get("target") or "").strip()
        try:
            _, ref = routes.parse_target(target)
            if ref is not None:
                target = f"{ref[0]}/{ref[1]}"
                if target not in channel_refs:
                    errors.append(
                        (f"{path}.target", f"no channel {target!r} to route to")
                    )
                elif target in disabled_refs:
                    errors.append(
                        (
                            f"{path}.target",
                            f"channel {target!r} is disabled in this bundle — "
                            "a rule cannot target a disabled channel",
                        )
                    )
        except routes.RuleError as e:
            errors.append((f"{path}.target", str(e)))

        matchers: list[tuple[str, str]] = []
        raw_matchers = block.get("matchers")
        if not isinstance(raw_matchers, list) or not raw_matchers:
            errors.append((f"{path}.matchers", "at least one matcher is required"))
            raw_matchers = []
        for j, entry in enumerate(raw_matchers):
            mpath = f"{path}.matchers[{j}]"
            if isinstance(entry, dict):
                value = str(entry.get("value") or "")
                explicit = entry.get("type")
                matcher_type = str(explicit) if explicit else None
            elif isinstance(entry, str):
                value, matcher_type = entry, None
            else:
                errors.append((mpath, "must be a string or a {type, value} mapping"))
                continue
            try:
                matchers.append(routes.infer_matcher(value, matcher_type))
            except routes.RuleError as e:
                errors.append((mpath, str(e)))
        out.append({"name": name, "target": target, "matchers": matchers})
    return out


# ---- apply ---------------------------------------------------------------------


def _resolve_token_wg(
    parsed: dict, store: Store, *, stored_credentials_ok: bool, merge: bool = True
) -> tuple[list[str], list[str]]:
    """Settle every token channel's ``wg`` (in place) before any mutation.

    ``merge`` selects how an *unstated* ``enabled`` reads: on a merge it
    inherits the existing channel's state, on a restore it means enabled.

    A token channel's WireGuard params are *derived* state, not configuration
    — the authoritative fields are the provider, token, and location. So, per
    channel, in order of preference:

    1. **Already exists locally with the same location** — keep its live
       params: no API call, no key churn, no needless reconcile when the same
       bundle is re-applied.
    2. **New identity or changed location** — resolve a fresh server via the
       provider token (the migration case; the account key is derived once
       per provider).
    3. **Fresh resolve fails** (API down, token rejected — the token itself is
       required, so it's present) — fall back to the bundle's snapshot so the
       apply still succeeds; the daemon's probe + auto-reconnect refresh it
       later. Only a wg-less channel with no snapshot to fall back on fails the
       whole apply.

    **Disabled channels are exempt from fresh resolution** — the provider API
    is never asked to pick a server for one. After the keep-existing check
    (step 1, no API call), a disabled channel keeps the bundle's snapshot if
    it has one, else stays wg-less: it can't be materialised anyway, and a
    later ``alle channels enable`` resolves it then.

    This is the only networked step of an apply. Config channels are never
    touched — their snapshot is the source of truth. Returns ``(resolved,
    fallback)`` channel refs for the apply summary.
    """
    resolved: list[str] = []
    fallback: list[str] = []
    resolvers: dict[str, WireGuardResolver | ProviderError] = {}

    def resolver_for(
        provider: str, creds: dict | None
    ) -> WireGuardResolver | ProviderError:
        if provider not in resolvers:
            try:
                resolvers[provider] = provider_resolver(provider, creds or {})
            except ProviderError as e:  # cached: never re-derive a failing key
                resolvers[provider] = e
        return resolvers[provider]

    for provider, entry in parsed["providers"].items():
        if kind(provider) != "token" or not is_functional(provider):
            continue
        creds = entry["credential"]
        if creds is None and stored_credentials_ok:
            creds = credentials.get(provider)
        for cid, ch in entry["channels"].items():
            ref = f"{provider}/{cid}"
            existing = store.get_channel(provider, cid)
            if (
                existing is not None
                and existing.wg
                and (existing.country, existing.city) == (ch["country"], ch["city"])
            ):
                ch["wg"] = copy.deepcopy(existing.wg)
                continue
            enabled = ch.get("enabled")
            if enabled is None:
                # unstated: a merge keeps the existing channel's state; a
                # restore (and a brand-new channel) reads it as enabled
                enabled = existing.enabled if merge and existing is not None else True
            if not enabled:
                # never resolve a channel that will be (or stay) disabled;
                # keep the snapshot (which may be None — wg-less until
                # enabled)
                continue
            snapshot = ch["wg"]
            resolver = resolver_for(provider, creds)
            try:
                if isinstance(resolver, ProviderError):
                    raise resolver
                ch["wg"] = resolver(ch["country"], ch["city"])
                resolved.append(ref)
            except ProviderError as e:
                if snapshot is not None:
                    ch["wg"] = snapshot
                    fallback.append(ref)
                    continue
                raise BundleError(
                    [
                        (
                            f"providers.{provider}.channels.{cid}",
                            f"could not resolve a server: {e}",
                        )
                    ]
                ) from e
    return resolved, fallback


def apply_import(text: str, *, location_lookup=None) -> dict:
    """Merge a bundle into the current setup (upsert by ``(provider, id)``).

    All-or-nothing: validation + network resolution stage everything first,
    then the whole merge commits as credentials writes + ONE state
    transaction (:meth:`Store.merge_setup`) inside a setup transaction — a
    failure or crash anywhere before the state commit rolls the credentials
    back and leaves the setup untouched.

    ``location_lookup`` (see :func:`validate`) backs the country/city check
    for **disabled** channels — the ones this apply deliberately never
    resolves, so the catalog is their only correctness gate.
    """
    data = loads(text)
    store = Store.load()
    existing_refs = {f"{c.provider}/{c.id}" for c in store.channels()}
    parsed = validate(
        data,
        text=text,
        extra_channel_refs=existing_refs,
        stored_credentials_ok=True,
        location_lookup=location_lookup,
        check_locations="disabled",
    )
    wg_resolved, wg_fallback = _resolve_token_wg(
        parsed, store, stored_credentials_ok=True
    )

    creds_added: list[str] = []
    creds_replaced: list[str] = []
    router = parsed["router"]
    try:
        with txn.setup_transaction("bundle import") as t:
            for provider, entry in parsed["providers"].items():
                if entry["credential"] is not None:
                    old = credentials.get(provider)
                    if old != entry["credential"]:
                        (creds_added if old is None else creds_replaced).append(
                            provider
                        )
                        credentials.set_(provider, entry["credential"])
            merged = store.merge_setup(
                {p: e["channels"] for p, e in parsed["providers"].items()},
                router["rulesets"] or [],
                killswitch=router["killswitch"],
                lan_direct=router["lan_direct"],
                router_port=router["port"],
            )
            t.commit()
    except PortInUseError as e:
        # A declared port clashing with the *existing* setup is only knowable
        # at merge time; surface it like any other blocker — nothing changed.
        raise BundleError([("providers", str(e))]) from e
    except ReferencedError as e:
        # The bundle disables a channel an *existing* routing rule targets —
        # only knowable at merge time. Nothing changed; list every blocker.
        raise BundleError(
            [
                (
                    "providers.{}.channels.{}.enabled".format(*ref.split("/", 1)),
                    "cannot disable — routing rule(s) still reference it: "
                    + ", ".join(r["id"] for r in rules_)
                    + " (retarget or remove them first)",
                )
                for ref, rules_ in sorted(e.blockers.items())
            ]
        ) from e

    # Post-commit: re-created identities lift their metrics tombstones so
    # their traffic counts again.
    for provider in merged["providers_added"]:
        metrics.revive_provider(provider)
    for ref in merged["created"]:
        provider, _, cid = ref.partition("/")
        metrics.revive_channel(provider, cid)

    return {
        "mode": "import",
        "providers_added": merged["providers_added"],
        "credentials": {"added": creds_added, "replaced": creds_replaced},
        "channels": {
            "created": merged["created"],
            "updated": merged["updated"],
            "unchanged": merged["unchanged"],
        },
        "rulesets_added": merged["rulesets_added"],
        "wg_resolved": wg_resolved,
        "wg_fallback": wg_fallback,
        "killswitch": router["killswitch"],
        "lan_direct": router["lan_direct"],
    }


def validate_file(text: str, *, location_lookup=None) -> dict:
    """Validate a bundle as a self-contained file — the strict, restore-style
    checks — without applying it. Raises :class:`BundleError` with every
    problem (line-annotated). ``location_lookup`` enables country/city checks."""
    data = loads(text)
    return validate(
        data,
        text=text,
        stored_credentials_ok=False,
        strict=True,
        location_lookup=location_lookup,
    )


def plan_restore(text: str) -> dict:
    """Validate a bundle for restore and summarize both sides — the
    confirmation step. Touches neither the network nor the state."""
    parsed = validate(loads(text), text=text, strict=True)
    store = Store.load()
    return {
        "bundle": _setup_counts(
            {p: set(e["channels"]) for p, e in parsed["providers"].items()},
            len(parsed["router"]["rulesets"] or []),
        ),
        "current": _setup_counts(
            {
                p: {c.id for c in store.provider_channels(p)}
                for p in store.provider_names()
            },
            len(store.rulesets()),
        ),
    }


def _setup_counts(channels_by_provider: dict[str, set], rulesets: int) -> dict:
    return {
        "providers": len(channels_by_provider),
        "channels": sum(len(c) for c in channels_by_provider.values()),
        "rulesets": rulesets,
    }


def apply_restore(text: str, *, location_lookup=None) -> dict:
    """Replace the whole setup with the bundle. Destructive — callers confirm.

    ``location_lookup`` backs the disabled-channel country/city check, exactly
    as in :func:`apply_import`.
    """
    data = loads(text)
    parsed = validate(
        data,
        text=text,
        stored_credentials_ok=False,
        strict=True,
        location_lookup=location_lookup,
        check_locations="disabled",
    )
    store = Store.load()
    wg_resolved, wg_fallback = _resolve_token_wg(
        parsed, store, stored_credentials_ok=False, merge=False
    )

    old_providers = set(store.provider_names())
    old_channels = {f"{c.provider}/{c.id}" for c in store.channels()}

    # Credentials first (journalled — rolled back on any failure or crash),
    # then state in ONE transaction: the commit point (it moves
    # config_signature and triggers the reconcile). Metrics cleanup is
    # best-effort post-commit work.
    router = parsed["router"]
    try:
        with txn.setup_transaction("bundle restore") as t:
            credentials.replace_all(
                {
                    provider: entry["credential"]
                    for provider, entry in parsed["providers"].items()
                    if entry["credential"] is not None
                }
            )
            store.restore_setup(
                {p: e["channels"] for p, e in parsed["providers"].items()},
                router["rulesets"] or [],
                killswitch=bool(router["killswitch"]),
                lan_direct=router["lan_direct"]
                if router["lan_direct"] is not None
                else True,
                router_port=router["port"],
            )
            t.commit()
    except PortInUseError as e:
        raise BundleError([("providers", str(e))]) from e
    new_channels = {
        f"{provider}/{cid}"
        for provider, entry in parsed["providers"].items()
        for cid in entry["channels"]
    }
    removed_providers = sorted(old_providers - set(parsed["providers"]))
    # Post-commit metrics reconciliation (best-effort): forget + tombstone
    # everything the restore removed — including channels dropped from
    # *retained* providers, not just removed providers — and lift tombstones
    # for every identity the new setup contains.
    for provider in removed_providers:
        metrics.remove_provider(provider)
    for ref in sorted(old_channels - new_channels):
        provider, _, cid = ref.partition("/")
        if provider in parsed["providers"]:  # retained provider, dropped channel
            metrics.remove_channel(provider, cid)
    for provider in parsed["providers"]:
        metrics.revive_provider(provider)
    for ref in new_channels:
        provider, _, cid = ref.partition("/")
        metrics.revive_channel(provider, cid)

    return {
        "mode": "restore",
        "providers": sorted(parsed["providers"]),
        "channels": sorted(new_channels),
        "rulesets": [block["name"] for block in router["rulesets"] or []],
        "credentials": sorted(
            p for p, e in parsed["providers"].items() if e["credential"] is not None
        ),
        "wg_resolved": wg_resolved,
        "wg_fallback": wg_fallback,
        "removed": {
            "providers": removed_providers,
            "channels": sorted(old_channels - new_channels),
        },
    }
