"""The single consolidated state file: ``~/.alle/state.json``.

This is alle's one source of truth for the data model: **each provider holds a
list of channels**, and each channel carries its resolved WireGuard parameters,
its local proxy port, and the latest heartbeat-probe result. Tokens are the only
thing kept elsewhere (``credentials.yaml``), because they are secrets the user
types once and we never echo back.

Two writers touch this file — the CLI (add/remove providers and channels) and the
applier daemon (writes probe results) — so every mutation goes through
``transaction()``, which takes an exclusive ``flock`` and does an atomic
read-modify-write. Plain ``load()`` reads are lock-free snapshots.

Identity: a channel is unique *within* its provider by ``id`` (e.g.
``united_states_1``); the same id may exist under two providers. The globally
unique handle is therefore ``(provider, id)``, which also names the sing-box tags
(``in-<provider>-<id>`` / ``out-<provider>-<id>``) and so must stay free of the
``-`` separator — provider keys are bare lowercase words and ids are
underscore slugs, so the split is unambiguous.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from alle import applog, fsio, paths
from alle.constants import INBOUND_PREFIX, OUTBOUND_PREFIX

STATE_VERSION = 1
SETUP_COMMIT_KEY = "_setup_commit"


def _slug(text: str) -> str:
    """Filesystem/tag-safe lowercase slug: 'United States' -> 'united_states'."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "channel"


def channel_id_from_filename(filename: str) -> str:
    """The channel id a ``.conf`` filename resolves to (the same slug
    :meth:`Store.upsert_channel` keys on). Public so callers can look a config
    channel up by name before importing it."""
    return _slug(filename)


class PortInUseError(RuntimeError):
    """An explicitly requested port is already claimed by another channel (or
    the router entrypoint). ``holder`` names the claimant for the message."""

    def __init__(self, port: int, holder: str):
        self.port = port
        self.holder = holder
        super().__init__(f"port {port} is already used by {holder}")


def _used_ports(data: dict) -> dict[int, str]:
    """Every claimed port -> a human name for its claimant."""
    used: dict[int, str] = {}
    for provider, prov in (data.get("providers") or {}).items():
        for cid, ch in (prov.get("channels") or {}).items():
            port = int(ch.get("port") or 0)
            if port:
                used[port] = f"channel {provider}/{cid}"
    router_port = int((data.get("router") or {}).get("port") or 0)
    if router_port:
        used[router_port] = "the router entrypoint"
    return used


def _claim_port(
    data: dict, port: int, *, exclude: tuple[str, str] | None = None
) -> int:
    """Validate an explicitly requested ``port`` against the locked state.

    ``exclude`` names the ``(provider, cid)`` being (re)pointed, so a channel
    keeping its own declared port is never its own conflict. Raises
    :class:`PortInUseError` on a clash — explicit ports are a declaration, and
    silently moving a declared port would break the compose/firewall contract
    the declaration exists for.
    """
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        raise ValueError(f"port must be 1-65535, got {port!r}")
    used = _used_ports(data)
    if exclude is not None:
        own = f"channel {exclude[0]}/{exclude[1]}"
        used = {p: h for p, h in used.items() if h != own}
    if port in used:
        raise PortInUseError(port, used[port])
    return port


def _next_free_port(data: dict) -> int:
    """Allocate an unclaimed local proxy port against the locked state.

    Default: ask the OS for an ephemeral loopback port (the temporary socket is
    closed before sing-box later binds it, so another local process can
    theoretically race us, but this avoids hard-coded ranges). Opt-in
    (``ALLE_PORT_BASE=<n>``): allocate sequentially from ``n`` upward instead —
    deterministic ports for environments that must publish them ahead of time
    (the container image); unset means exactly the OS-assigned behavior.
    """
    used = set(_used_ports(data))
    base = os.environ.get("ALLE_PORT_BASE")
    if base:
        try:
            start = int(base)
        except ValueError as e:
            raise RuntimeError(f"ALLE_PORT_BASE must be a port number: {base!r}") from e
        if not 0 < start <= 65535:
            raise RuntimeError(f"ALLE_PORT_BASE must be 1-65535, got {start}")
        for port in range(start, 65536):
            if port not in used:
                return port
        raise RuntimeError(f"no free port at or above ALLE_PORT_BASE={start}")
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in used:
            return port
    raise RuntimeError("could not allocate an unused local proxy port")


def _next_id(taken: set, country: str, city: str, id_hint: str | None = None) -> str:
    """Auto-name like ``united_states_1`` / ``united_states_san_francisco_2``.

    API channels name themselves from country/city; imported config channels
    pass an explicit ``id_hint`` (the .conf file name) since they have no
    location. ``taken`` is the set of ids already used within the provider.
    This id is the channel's permanent handle — distinct from the optional
    display ``label`` (see :class:`Channel`), which is presentation-only.
    """
    if id_hint:
        base = _slug(id_hint)
    elif country or city:
        base = _slug(f"{country} {city}".strip())
    else:
        base = "channel"
    n = 1
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


# ---- channel view ----------------------------------------------------------


@dataclass
class Channel:
    """A flat, in-memory view of one channel with its provider attached.

    Built from the nested ``state.json`` shape so callers (engine, CLI) can treat
    channels as a single list while the file stays provider-grouped.
    """

    provider: str
    id: str
    port: int
    country: str = ""
    city: str = ""
    label: str = ""  # optional display label; the id stays the only handle
    wg: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)
    reconnect: dict = field(default_factory=dict)
    # Administrative intent, distinct from probe-derived liveness: a disabled
    # channel is not materialised in sing-box at all (no inbound, no WireGuard
    # endpoint, no keepalive — the provider sees no connection). On disk the
    # key is written only when False; absent reads True.
    enabled: bool = True

    @property
    def display(self) -> str:
        """What to show as the channel's name — the label, or the id when unset.

        Presentation only: commands, routing rules, sing-box tags, and metrics
        keys all use ``id``, never this. So relabelling touches nothing but the
        display and cannot cascade.
        """
        return self.label or self.id

    @property
    def inbound_tag(self) -> str:
        return f"{INBOUND_PREFIX}{self.provider}-{self.id}"

    @property
    def outbound_tag(self) -> str:
        return f"{OUTBOUND_PREFIX}{self.provider}-{self.id}"

    @property
    def location(self) -> str:
        if self.city:
            return f"{self.city}, {self.country}"
        return self.country or "—"


def _channel_fingerprint_raw(ch: dict) -> str:
    """Identity of the channel configuration a background result observed."""
    material = {
        "enabled": bool(ch.get("enabled", True)),
        "port": int(ch.get("port", 0)),
        "wg": ch.get("wg") or {},
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def channel_fingerprint(ch: Channel) -> str:
    return _channel_fingerprint_raw(
        {"enabled": ch.enabled, "port": ch.port, "wg": ch.wg}
    )


def tag_to_ref(tag: str) -> tuple[str, str] | None:
    """Parse an ``in-/out-<provider>-<id>`` tag back into ``(provider, id)``."""
    parts = tag.split("-", 2)
    if len(parts) == 3 and parts[0] in ("in", "out"):
        return parts[1], parts[2]
    return None


def _channel_view(provider: str, cid: str, ch: dict) -> Channel:
    """Build the public view from one raw record already captured by a caller."""
    return Channel(
        provider=provider,
        id=cid,
        port=int(ch.get("port", 0)),
        country=ch.get("country", ""),
        city=ch.get("city", ""),
        label=ch.get("label", ""),
        wg=ch.get("wg") or {},
        probe=ch.get("probe", {}),
        reconnect=ch.get("reconnect", {}),
        enabled=bool(ch.get("enabled", True)),
    )


# ---- the store -------------------------------------------------------------


def _state_path() -> Path:
    return paths.state_dir() / "state.json"


def _lock_path() -> Path:
    return paths.state_dir() / "state.lock"


def _blank() -> dict:
    return {"version": STATE_VERSION, "providers": {}, "router": _router_blank()}


def _router_blank() -> dict:
    """The router entrypoint's state: contract port (0 = not yet allocated),
    the explicit kill-switch flag, the built-in LAN-direct toggle, the TUN-mode
    flag, and the ordered rule list. ``lan_direct`` defaults **on** — readers
    must treat an absent key as True so pre-existing state files inherit the
    default. ``tun`` defaults off; absent reads as False."""
    return {
        "port": 0,
        "killswitch": False,
        "lan_direct": True,
        "tun": False,
        "rules": [],
    }


def _rule_num(rule: dict) -> int:
    rid = str(rule.get("id", ""))
    return int(rid[1:]) if rid.startswith("r") and rid[1:].isdigit() else 0


def _ruleset_num(rule: dict) -> int:
    rsid = str(rule.get("ruleset", ""))
    return int(rsid[2:]) if rsid.startswith("rs") and rsid[2:].isdigit() else 0


def _next_rule_id(rules: list[dict]) -> str:
    return f"r{max((_rule_num(r) for r in rules), default=0) + 1}"


def _next_ruleset_id(rules: list[dict]) -> str:
    return f"rs{max((_ruleset_num(r) for r in rules), default=0) + 1}"


def _target_name(target: str) -> str:
    if target == "direct":
        return "Direct"
    if target == "block":
        return "Block"
    return target


def _require_provider(data: dict, provider: str) -> dict:
    """The provider's dict, or ``ValueError`` if it is not added.

    Channel writes never create their provider implicitly: a channel add
    racing a provider removal must fail, not silently resurrect the provider
    without its credential.
    """
    prov = data["providers"].get(provider)
    if prov is None:
        raise ValueError(f"provider {provider!r} is not added")
    return prov


def _ensure_channel_target(data: dict, target: str) -> None:
    provider, _, cid = target.partition("/")
    if cid:
        chans = (data["providers"].get(provider) or {}).get("channels") or {}
        if cid not in chans:
            raise ValueError(f"no channel {target!r} to route to")
        if not chans[cid].get("enabled", True):
            raise ValueError(
                f"channel {target!r} is disabled — enable it first: "
                f"alle channels enable {target}"
            )


def _normalize_rulesets(data: dict) -> None:
    """Make every loaded rule belong to a contiguous ruleset.

    Legacy flat rows are folded into one auto-named ruleset per contiguous
    same-target run. Rows that already carry ruleset metadata are left intact,
    except missing names inherit a target-derived display name. Mixed old/new
    files are rare but safe: old rows become their own runs without disturbing
    existing grouped blocks.

    Matcher types are normalized too: the legacy exact ``domain`` type reads
    as ``domain_suffix`` — alle has one domain semantic (the domain and its
    subdomains), so an old exact rule gains subdomain matching instead of
    keeping a second, subtly different behavior alive.
    """
    router = data.setdefault("router", _router_blank())
    rules = router.setdefault("rules", [])
    if not rules:
        return
    max_rsid = max((_ruleset_num(r) for r in rules), default=0)
    last_legacy_target = object()
    legacy_rsid = ""
    legacy_name = ""
    for rule in rules:
        if rule.get("type") == "domain":  # legacy exact matcher
            rule["type"] = "domain_suffix"
        if rule.get("ruleset"):
            rule.setdefault("ruleset_name", _target_name(str(rule.get("target", ""))))
            last_legacy_target = object()
            continue
        target = str(rule.get("target", ""))
        if target != last_legacy_target:
            max_rsid += 1
            legacy_rsid = f"rs{max_rsid}"
            legacy_name = _target_name(target)
            last_legacy_target = target
        rule["ruleset"] = legacy_rsid
        rule["ruleset_name"] = legacy_name


def _ruleset_blocks(rules: list[dict]) -> list[dict]:
    """Ordered contiguous ruleset blocks from a normalized rule list."""
    blocks: list[dict] = []
    by_id: dict[str, dict] = {}
    seen_closed: set[str] = set()
    current = ""
    for rule in rules:
        rsid = str(rule.get("ruleset", ""))
        if not rsid:
            raise ValueError(f"rule {rule.get('id')} has no ruleset")
        if rsid != current:
            if rsid in seen_closed:
                raise ValueError(f"ruleset {rsid} is not contiguous")
            if current:
                seen_closed.add(current)
            block = {
                "id": rsid,
                "name": str(rule.get("ruleset_name") or _target_name(rule["target"])),
                "target": rule["target"],
                "rules": [],
            }
            blocks.append(block)
            by_id[rsid] = block
            current = rsid
        block = by_id[rsid]
        if rule["target"] != block["target"]:
            raise ValueError(f"ruleset {rsid} has mixed targets")
        block["rules"].append(dict(rule))
    return blocks


class ReferencedError(RuntimeError):
    """Removal refused: routing rules still reference the channel(s).

    ``blockers`` maps ``"provider/cid"`` to the referencing rule dicts. Raised
    from *inside* the removal transaction so the restrict-only invariant holds
    under concurrent writers, not just at CLI plan time.
    """

    def __init__(self, blockers: dict[str, list[dict]]):
        self.blockers = blockers
        super().__init__("channel(s) are referenced by routing rules")


def _check_permutation(current: list[str], proposed: list[str], noun: str) -> None:
    """Require ``proposed`` to be a full permutation of ``current`` — no
    duplicates, no unknown ids, none missing — or raise ``ValueError`` naming
    every offender (``noun`` is "rule" or "ruleset")."""
    seen: set[str] = set()
    dupes: list[str] = []
    for rid in proposed:
        if rid in seen and rid not in dupes:
            dupes.append(rid)
        seen.add(rid)
    if dupes:
        raise ValueError(f"duplicate {noun} id(s): {', '.join(dupes)}")
    current_set = set(current)
    proposed_set = set(proposed)
    unknown = [rid for rid in proposed if rid not in current_set]
    if unknown:
        raise ValueError(f"unknown {noun} id(s): {', '.join(unknown)}")
    missing = [rid for rid in current if rid not in proposed_set]
    if missing:
        raise ValueError(f"missing {noun} id(s): {', '.join(missing)}")


def _rules_referencing(data: dict, refs: set[tuple[str, str]]) -> dict[str, list[dict]]:
    """``"provider/cid" -> [rule, …]`` for rules targeting any of ``refs``."""
    out: dict[str, list[dict]] = {}
    for rule in (data.get("router") or {}).get("rules") or []:
        provider, _, cid = str(rule.get("target", "")).partition("/")
        if cid and (provider, cid) in refs:
            out.setdefault(f"{provider}/{cid}", []).append(dict(rule))
    return out


class StoreReadError(RuntimeError):
    """A store file exists but could not be read (permissions, transient I/O).

    Deliberately not treated as empty: every mutation is read-modify-write, so
    proceeding from a blank view would let the next write persist the emptiness
    and silently destroy the real data (channels, WireGuard keys, credentials).
    Callers abort instead; the file on disk stays intact. Distinct from a
    *corrupt* file, which :func:`_quarantine` moves aside loudly.
    """


class StateVersionError(StoreReadError):
    """state.json was written by a newer alle than this one.

    Not corruption — the data is presumably fine, this alle just cannot be
    trusted to interpret (let alone read-modify-write) it. Never quarantined;
    reads and mutations abort until alle is upgraded.
    """


FileIdentity = tuple[int, int, int, int]


def _read_store_text(p: Path) -> tuple[str, FileIdentity]:
    """Read one complete file generation and return its stable identity.

    Opening first and calling ``fstat`` on that descriptor makes the bytes and
    identity describe the same inode even if an atomic replacement happens
    immediately after the read.
    """
    with p.open() as f:
        text = f.read()
        st = os.fstat(f.fileno())
    return text, (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _fsync_dir_strict(path: Path) -> None:
    """Fsync ``path`` and propagate failure for evidence-preserving moves."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _quarantine(
    p: Path,
    err: Exception,
    *,
    failed_text: str,
    failed_identity: FileIdentity,
    lock_path: Path,
    validate: Callable[[str], object],
    lock_held: bool = False,
) -> bool:
    """Preserve one corrupt generation under the store's writer lock.

    Returns ``True`` only when the exact generation the caller failed to parse
    was moved aside. A concurrent replacement makes this return ``False`` so
    the caller retries the newer generation. Preservation uses an exclusive
    hard link followed by unlink: unlike ``rename``/``replace``, an existing
    backup can never be overwritten. Any inability to preserve and fsync the
    evidence aborts the read instead of exposing a blank mutable view.
    """
    held = nullcontext() if lock_held else fsio.locked(lock_path)
    with held:
        try:
            current_text, current_identity = _read_store_text(p)
        except FileNotFoundError:
            return False
        except OSError as e:
            raise StoreReadError(f"cannot re-read {p.name} for quarantine: {e}") from e

        if current_identity != failed_identity or current_text != failed_text:
            return False
        try:
            validate(current_text)
        except Exception:  # the store parser defines the accepted exception type
            pass
        else:
            return False

        backup = p.with_name(f"{p.name}.corrupt-{time.time_ns()}-{os.getpid()}")
        try:
            os.link(p, backup, follow_symlinks=False)
            p.unlink()
            _fsync_dir_strict(p.parent)
        except OSError as e:
            raise StoreReadError(
                f"cannot preserve corrupt {p.name} as {backup.name}: {e}"
            ) from e
        # Parser messages can quote source lines; credentials must never reach
        # stderr or logs. The exception class is enough context alongside the
        # preserved backup path.
        msg = (
            f"{p.name} is corrupt ({type(err).__name__}); "
            f"moved to {backup.name}, starting empty"
        )
        applog.log(msg)
        print(f"alle: {msg}", file=sys.stderr)
        return True


def _check_schema(data: dict) -> None:
    """Reject a parsed state whose container shapes are unusable.

    Raises ``ValueError`` — the same class the JSON-corruption path uses — so
    a file that *parses* but cannot be safely read-modify-written (providers
    as a list, a channel as a string, …) is quarantined loudly instead of
    crashing mid-mutation or, worse, being partially rewritten around.
    Container defaults apply only when a key is absent. A present ``null``,
    false, scalar, or list is malformed rather than an alias for an empty
    object/list.
    """
    version = data.get("version", STATE_VERSION)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("version is not an integer")
    if SETUP_COMMIT_KEY in data and not isinstance(data[SETUP_COMMIT_KEY], str):
        raise ValueError(f"{SETUP_COMMIT_KEY} is not a string")
    providers = data.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("providers is not an object")
    for provider, prov in providers.items():
        if not isinstance(provider, str):
            raise ValueError("provider key is not a string")
        if not isinstance(prov, dict):
            raise ValueError(f"provider {provider!r} is not an object")
        chans = prov.get("channels", {})
        if not isinstance(chans, dict):
            raise ValueError(f"provider {provider!r} channels is not an object")
        for cid, ch in chans.items():
            if not isinstance(cid, str):
                raise ValueError(f"provider {provider!r} channel key is not a string")
            if not isinstance(ch, dict):
                raise ValueError(f"channel {provider!r}/{cid!r} is not an object")
            for key in ("wg", "probe", "reconnect"):
                if key in ch and not isinstance(ch[key], dict):
                    raise ValueError(
                        f"channel {provider!r}/{cid!r} {key} is not an object"
                    )
    router = data.get("router", {})
    if not isinstance(router, dict):
        raise ValueError("router is not an object")
    rules = router.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("router.rules is not a list")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("router.rules entry is not an object")


def _parse_state_text(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("root is not a JSON object")
    _check_schema(data)
    return data


def _read_raw(*, lock_held: bool = False) -> dict:
    p = _state_path()
    while True:
        try:
            text, identity = _read_store_text(p)
        except FileNotFoundError:
            return _blank()  # genuinely absent — a fresh install starts blank
        except OSError as e:
            raise StoreReadError(f"cannot read {p.name}: {e}") from e
        try:
            data = _parse_state_text(text)
        except ValueError as e:
            moved = _quarantine(
                p,
                e,
                failed_text=text,
                failed_identity=identity,
                lock_path=_lock_path(),
                validate=_parse_state_text,
                lock_held=lock_held,
            )
            if moved:
                return _blank()
            continue  # a concurrent writer published another generation
        break
    version = data.setdefault("version", STATE_VERSION)
    if version > STATE_VERSION:
        raise StateVersionError(
            f"{p.name} is version {version}, newer than this alle understands "
            f"(max {STATE_VERSION}) — upgrade alle"
        )
    data.setdefault("providers", {})
    data.setdefault("router", _router_blank())
    _normalize_rulesets(data)
    return data


def _write_raw(data: dict) -> None:
    """Atomically and durably replace state.json (temp + fsync + rename)."""

    def dump(f):
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    fsio.write_durably(
        _state_path(),
        dump,
        prefix=".state-",
        suffix=".json",
        mode=0o600,  # carries WireGuard private keys
    )


@contextmanager
def transaction():
    """Exclusive read-modify-write of state.json under an OS file lock.

    Yields the mutable raw dict; whatever it looks like on exit is written back
    atomically. Serialises the CLI's structural edits against the daemon's probe
    writes so neither clobbers the other.
    """
    with fsio.locked(_lock_path()):
        data = _read_raw(lock_held=True)
        yield data
        # A compound setup operation publishes its transaction identity in the
        # same atomic state replacement as the actual mutation. Recovery can
        # therefore distinguish a crash before this write from one after it.
        txn_module = sys.modules.get("alle.txn")
        if txn_module is not None:
            setup_id = txn_module.active_setup_id()
            if setup_id is not None:
                data[SETUP_COMMIT_KEY] = setup_id
        _write_raw(data)


@dataclass
class Store:
    """In-memory snapshot of state.json, plus mutators that persist transactionally."""

    data: dict = field(default_factory=_blank)

    @classmethod
    def load(cls) -> Store:
        return cls(data=_read_raw())

    # ---- providers ---------------------------------------------------------
    @property
    def providers(self) -> dict[str, dict]:
        return self.data.get("providers", {})

    def provider_names(self) -> list[str]:
        return sorted(self.providers)

    def has_provider(self, provider: str) -> bool:
        return provider in self.providers

    def add_provider(self, provider: str) -> None:
        with transaction() as data:
            data["providers"].setdefault(provider, {"channels": {}})
        self.data = _read_raw()

    def remove_providers(self, providers: list[str]) -> dict[str, int]:
        """Remove several providers (and all their channels) in ONE transaction.

        All-or-nothing: the restrict-only blocker check runs across every
        provider's channels inside the transaction, so either the whole batch
        is removed or — on any :class:`ReferencedError` — none of it is.
        Returns ``{provider: channels_removed}``.
        """
        with transaction() as data:
            refs = {
                (provider, cid)
                for provider in providers
                for cid in (data["providers"].get(provider) or {}).get("channels") or {}
            }
            blockers = _rules_referencing(data, refs)
            if blockers:
                raise ReferencedError(blockers)
            counts: dict[str, int] = {}
            for provider in providers:
                prov = data["providers"].pop(provider, None)
                counts[provider] = len(prov.get("channels", {})) if prov else 0
        self.data = _read_raw()
        return counts

    # ---- channels ----------------------------------------------------------
    def channels(self) -> list[Channel]:
        out: list[Channel] = []
        for provider, pdata in sorted(self.providers.items()):
            for cid, ch in sorted((pdata.get("channels") or {}).items()):
                out.append(_channel_view(provider, cid, ch))
        return out

    def provider_channels(self, provider: str) -> list[Channel]:
        return [c for c in self.channels() if c.provider == provider]

    def get_channel(self, provider: str, cid: str) -> Channel | None:
        return next(
            (c for c in self.channels() if c.provider == provider and c.id == cid), None
        )

    def add_channel(
        self,
        provider: str,
        country: str,
        city: str,
        wg: dict,
        label: str = "",
        port: int = 0,
    ) -> Channel:
        # id and port are allocated inside the transaction, against the locked
        # on-disk state — two concurrent adds can never pick the same slot.
        # ``label`` is the optional display label, not part of the id.
        # ``port`` (optional) is an explicit declaration; 0 keeps the default
        # allocation. A declared port that clashes raises PortInUseError.
        with transaction() as data:
            prov = _require_provider(data, provider)
            chans = prov.setdefault("channels", {})
            cid = _next_id(set(chans), country, city)
            port = _claim_port(data, port) if port else _next_free_port(data)
            chans[cid] = {
                "country": country,
                "city": city,
                "port": port,
                "wg": wg,
                "probe": {},
            }
            if label:
                chans[cid]["label"] = label
            committed = _channel_view(provider, cid, chans[cid])
        self.data = _read_raw()
        return committed

    def upsert_channel(
        self,
        provider: str,
        filename: str,
        country: str,
        city: str,
        wg: dict,
        label: str = "",
        port: int = 0,
    ) -> tuple[Channel, bool]:
        """Create or update a channel with a *fixed* id derived from ``filename``.

        Returns ``(channel, created)``. Unlike :meth:`add_channel` (which appends
        ``_1``/``_2`` so one location can hold several servers), this keys the
        channel on ``filename`` — the config file name — so re-importing the same
        ``.conf`` updates the WireGuard params and re-parsed location *in place*,
        keeping the id and local port stable. A config's keys rotate on every
        regeneration while the server (the channel) stays the same, so the file
        name, not the key, is the identity.

        ``label`` is the optional display label. On re-import an existing label
        is **preserved** (a re-import replaces keys, not the user's naming);
        passing a new ``label`` explicitly overrides it.

        ``port`` (optional) is an explicit declaration: on create it is the
        channel's port; on update it *re-points* the channel (the declaration
        wins over the earlier allocation — that is what declaring is for).
        0 means allocate on create / keep the current port on update.
        """
        cid = _slug(filename)
        with transaction() as data:
            prov = _require_provider(data, provider)
            chans = prov.setdefault("channels", {})
            ch = chans.get(cid)
            created = ch is None
            if ch is None:
                chans[cid] = {
                    "country": country,
                    "city": city,
                    "port": _claim_port(data, port) if port else _next_free_port(data),
                    "wg": wg,
                    "probe": {},
                }
                if label:
                    chans[cid]["label"] = label
            else:
                ch["country"] = country
                ch["city"] = city
                ch["wg"] = (
                    wg  # keep port + probe; the daemon re-probes on the wg change
                )
                if port and port != int(ch.get("port") or 0):
                    ch["port"] = _claim_port(data, port, exclude=(provider, cid))
                if label:  # explicit override; otherwise the existing label stays
                    ch["label"] = label
                # A re-import is human intervention: forget any reconnect give-up
                # state so the daemon tries the fresh config from scratch.
                ch.pop("reconnect", None)
            committed = _channel_view(provider, cid, chans[cid])
        self.data = _read_raw()
        return committed, created

    def remove_channels(self, refs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Remove several channels in ONE transaction; returns those removed.

        All-or-nothing: the restrict-only blocker check covers the whole batch
        inside the transaction, so a rule added concurrently blocks the batch
        rather than slipping a half-removed set through.
        """
        removed: list[tuple[str, str]] = []
        with transaction() as data:
            blockers = _rules_referencing(data, set(refs))
            if blockers:
                raise ReferencedError(blockers)
            for provider, cid in refs:
                prov = data["providers"].get(provider) or {}
                if (prov.get("channels") or {}).pop(cid, None) is not None:
                    removed.append((provider, cid))
        self.data = _read_raw()
        return removed

    def set_channels_enabled(
        self, refs: list[tuple[str, str]], enabled: bool
    ) -> list[tuple[str, str]]:
        """Enable/disable several channels in ONE transaction; returns those
        whose state actually changed (already-there channels are no-ops).

        Disabling is as safe as deleting: the same restrict-only blocker check
        covers the whole batch inside the transaction (a rule added
        concurrently blocks the batch), because a disabled channel must never
        be referenced by a router rule. Enabling carries no reference check.

        On disk, ``enabled`` is written only when False — enabling removes the
        key, so a default-enabled channel keeps its original shape. Disabling
        also drops the channel's probe + reconnect bookkeeping: a disabled
        channel is inert, and a stale failing probe would otherwise show a
        misleading "failed" and give auto-reconnect something to act on.
        """
        changed: list[tuple[str, str]] = []
        with transaction() as data:
            if not enabled:
                blockers = _rules_referencing(data, set(refs))
                if blockers:
                    raise ReferencedError(blockers)
            for provider, cid in refs:
                prov = data["providers"].get(provider) or {}
                ch = (prov.get("channels") or {}).get(cid)
                if ch is None or bool(ch.get("enabled", True)) == enabled:
                    continue
                if enabled:
                    ch.pop("enabled", None)
                else:
                    ch["enabled"] = False
                    ch.pop("probe", None)
                    ch.pop("reconnect", None)
                changed.append((provider, cid))
        self.data = _read_raw()
        return changed

    def set_label(self, provider: str, cid: str, label: str) -> bool:
        """Set (or, with an empty ``label``, clear) a channel's display label.

        Pure metadata: not part of the id, tags, or ``config_signature``, so it
        never triggers a reconcile. Returns True if the channel exists.
        """
        found = False
        with transaction() as data:
            prov = data["providers"].get(provider) or {}
            ch = (prov.get("channels") or {}).get(cid)
            if ch is not None:
                found = True
                if label:
                    ch["label"] = label
                else:
                    ch.pop("label", None)  # cleared → falls back to the id
        self.data = _read_raw()
        return found

    def set_probe(self, provider: str, cid: str, probe: dict) -> None:
        """Set one probe unconditionally (interactive/test compatibility).

        Background probe passes use :meth:`set_probes`, whose captured
        identity check is what prevents delayed network results from landing.
        """
        with transaction() as data:
            prov = data["providers"].get(provider) or {}
            ch = (prov.get("channels") or {}).get(cid)
            if ch is not None:
                ch["probe"] = probe
        self.data = _read_raw()

    def set_probes(
        self,
        updates: dict[tuple[str, str], tuple[str, dict]],
    ) -> list[tuple[str, str]]:
        """Publish a probe pass atomically when channel identity still matches.

        Each result is paired with the configuration fingerprint captured
        before its network request.  A concurrent disable, port move, reimport,
        or key rotation makes that result stale, so it is skipped while valid
        siblings from the same pass still commit in one state transaction.
        """
        if not updates:
            return []
        applied: list[tuple[str, str]] = []
        with transaction() as data:
            for (provider, cid), (expected, result) in updates.items():
                prov = data["providers"].get(provider) or {}
                ch = (prov.get("channels") or {}).get(cid)
                if ch is None or _channel_fingerprint_raw(ch) != expected:
                    continue
                ch["probe"] = result
                applied.append((provider, cid))
        self.data = _read_raw()
        return applied

    def set_reconnect(self, provider: str, cid: str, reconnect: dict) -> None:
        """Persist a channel's reconnect state machine dict.

        An empty dict drops the key entirely — a healthy channel carries no
        reconnect bookkeeping. Like ``set_probe`` this leaves the config-relevant
        fields (port, wg) untouched, so it never triggers a reconcile.
        """
        with transaction() as data:
            prov = data["providers"].get(provider) or {}
            ch = (prov.get("channels") or {}).get(cid)
            if ch is not None:
                if reconnect:
                    ch["reconnect"] = reconnect
                else:
                    ch.pop("reconnect", None)
        self.data = _read_raw()

    def prepare_reconnects(
        self,
        updates: dict[tuple[str, str], tuple[str, dict, dict]],
    ) -> list[tuple[str, str]]:
        """Atomically claim reconnect bookkeeping derived from one snapshot.

        Values are ``(channel fingerprint, expected reconnect, replacement)``.
        Comparing both identities prevents overlapping daemon passes or a
        concurrent human edit from claiming the same attempt twice.
        """
        if not updates:
            return []
        applied: list[tuple[str, str]] = []
        with transaction() as data:
            for (provider, cid), (
                fingerprint,
                expected,
                replacement,
            ) in updates.items():
                chans = (data["providers"].get(provider) or {}).get("channels") or {}
                ch = chans.get(cid)
                if (
                    ch is None
                    or _channel_fingerprint_raw(ch) != fingerprint
                    or (ch.get("reconnect") or {}) != expected
                ):
                    continue
                if replacement:
                    ch["reconnect"] = replacement
                else:
                    ch.pop("reconnect", None)
                applied.append((provider, cid))
        self.data = _read_raw()
        return applied

    def finish_reconnect_attempt(
        self,
        provider: str,
        cid: str,
        fingerprint: str,
        nonce: str,
        reconnect: dict,
        *,
        wg: dict | None = None,
    ) -> bool:
        """Conditionally commit one attempt's result and optional WG config."""
        applied = False
        with transaction() as data:
            chans = (data["providers"].get(provider) or {}).get("channels") or {}
            ch = chans.get(cid)
            current = (ch or {}).get("reconnect") or {}
            if (
                ch is not None
                and _channel_fingerprint_raw(ch) == fingerprint
                and current.get("attempt_nonce") == nonce
            ):
                if wg is not None:
                    ch["wg"] = wg
                ch["reconnect"] = reconnect
                applied = True
        self.data = _read_raw()
        return applied

    def reallocate_channel_ports(
        self, ports: set[int]
    ) -> list[tuple[str, str, int, int]]:
        """Move every channel sitting on one of ``ports`` to a fresh free port.

        Recovery for a port another process grabbed between allocation and a
        sing-box (re)start — sing-box treats one unbindable inbound as fatal,
        so a single stolen port would otherwise take every channel down.
        Returns ``(provider, cid, old_port, new_port)`` per moved channel; the
        state change moves the config signature, so the daemon re-reconciles.
        The router entrypoint's contract port is covered too (reported as
        ``("router", "entrypoint", …)``) — stolen-port recovery is the one
        sanctioned way that port may ever change.
        """
        moved = []
        with transaction() as data:
            for provider, prov in sorted((data.get("providers") or {}).items()):
                for cid, ch in sorted((prov.get("channels") or {}).items()):
                    old = int(ch.get("port") or 0)
                    if old in ports:
                        new = _next_free_port(data)
                        ch["port"] = new
                        moved.append((provider, cid, old, new))
            router = data.setdefault("router", _router_blank())
            old = int(router.get("port") or 0)
            if old and old in ports:
                new = _next_free_port(data)
                router["port"] = new
                moved.append(("router", "entrypoint", old, new))
        self.data = _read_raw()
        return moved

    # ---- router entrypoint + rules -------------------------------------------
    @property
    def router(self) -> dict:
        return self.data.get("router") or _router_blank()

    def rules(self) -> list[dict]:
        """The ordered routing rules (evaluation order; first match wins)."""
        return [dict(r) for r in self.router.get("rules") or []]

    def rulesets(self) -> list[dict]:
        """Rules grouped into ordered, contiguous rulesets."""
        return _ruleset_blocks(self.rules())

    def ensure_router_port(self) -> int:
        """Allocate the router entrypoint's contract port once; return it.

        The port is a contract with external configuration (apps, OS profiles
        point at it), so after first allocation it is never changed here —
        only stolen-port recovery may move it.
        """
        port = int(self.router.get("port") or 0)
        if port:
            return port
        with transaction() as data:
            router = data.setdefault("router", _router_blank())
            if not int(router.get("port") or 0):
                router["port"] = _next_free_port(data)
            port = router["port"]
        self.data = _read_raw()
        return port

    def create_ruleset(
        self, name: str, target: str, matchers: list[tuple[str, str]]
    ) -> dict:
        """Create one ruleset atomically with one or more matchers."""
        if not matchers:
            raise ValueError("at least one matcher is required")
        label = (name or "").strip()
        if not label:
            raise ValueError("ruleset name cannot be empty")
        with transaction() as data:
            _ensure_channel_target(data, target)
            router = data.setdefault("router", _router_blank())
            rules = router.setdefault("rules", [])
            rsid = _next_ruleset_id(rules)
            created: list[dict] = []
            for matcher_type, value in matchers:
                rule = {
                    "id": _next_rule_id(rules + created),
                    "type": matcher_type,
                    "value": value,
                    "target": target,
                    "ruleset": rsid,
                    "ruleset_name": label,
                }
                created.append(rule)
            rules.extend(created)
            block = _ruleset_blocks(rules)[-1]
        self.data = _read_raw()
        return block

    def add_ruleset_matchers(
        self, ruleset_id: str, matchers: list[tuple[str, str]]
    ) -> dict:
        """Append matchers inside an existing ruleset block."""
        if not matchers:
            raise ValueError("at least one matcher is required")
        with transaction() as data:
            router = data.setdefault("router", _router_blank())
            rules = router.setdefault("rules", [])
            blocks = _ruleset_blocks(rules)
            block = next((b for b in blocks if b["id"] == ruleset_id), None)
            if block is None:
                raise ValueError(f"unknown ruleset {ruleset_id!r}")
            end = (
                max(i for i, r in enumerate(rules) if r.get("ruleset") == ruleset_id)
                + 1
            )
            insert: list[dict] = []
            for matcher_type, value in matchers:
                insert.append(
                    {
                        "id": _next_rule_id(rules + insert),
                        "type": matcher_type,
                        "value": value,
                        "target": block["target"],
                        "ruleset": ruleset_id,
                        "ruleset_name": block["name"],
                    }
                )
            rules[end:end] = insert
            block = next(b for b in _ruleset_blocks(rules) if b["id"] == ruleset_id)
        self.data = _read_raw()
        return block

    def remove_ruleset(self, ruleset_id: str) -> list[dict]:
        """Remove a whole ruleset block; returns removed rule rows."""
        removed: list[dict] = []
        with transaction() as data:
            router = data.setdefault("router", _router_blank())
            rules = router.setdefault("rules", [])
            _ruleset_blocks(rules)  # validates contiguity before mutating
            kept = []
            for rule in rules:
                if rule.get("ruleset") == ruleset_id:
                    removed.append(dict(rule))
                else:
                    kept.append(rule)
            if not removed:
                raise ValueError(f"unknown ruleset {ruleset_id!r}")
            router["rules"] = kept
        self.data = _read_raw()
        return removed

    def rename_ruleset(self, ruleset_id: str, name: str) -> dict:
        """Rename a ruleset block."""
        if not name:
            raise ValueError("ruleset name cannot be empty")
        with transaction() as data:
            router = data.setdefault("router", _router_blank())
            rules = router.setdefault("rules", [])
            _ruleset_blocks(rules)
            found = False
            for rule in rules:
                if rule.get("ruleset") == ruleset_id:
                    rule["ruleset_name"] = name
                    found = True
            if not found:
                raise ValueError(f"unknown ruleset {ruleset_id!r}")
            block = next(b for b in _ruleset_blocks(rules) if b["id"] == ruleset_id)
        self.data = _read_raw()
        return block

    def retarget_ruleset(self, ruleset_id: str, target: str) -> dict:
        """Change the target for every matcher in a ruleset."""
        with transaction() as data:
            _ensure_channel_target(data, target)
            router = data.setdefault("router", _router_blank())
            rules = router.setdefault("rules", [])
            _ruleset_blocks(rules)
            found = False
            for rule in rules:
                if rule.get("ruleset") == ruleset_id:
                    rule["target"] = target
                    found = True
            if not found:
                raise ValueError(f"unknown ruleset {ruleset_id!r}")
            block = next(b for b in _ruleset_blocks(rules) if b["id"] == ruleset_id)
        self.data = _read_raw()
        return block

    def update_ruleset(
        self,
        ruleset_id: str,
        name: str,
        target: str,
        matchers: list[tuple[str, str]],
    ) -> dict:
        """Set one ruleset's name, target, and matchers in one transaction,
        keeping its id and position. The per-ruleset editor's Apply."""
        if not name:
            raise ValueError("ruleset name cannot be empty")
        if not matchers:
            raise ValueError("at least one matcher is required")
        with transaction() as data:
            _ensure_channel_target(data, target)
            router = data.setdefault("router", _router_blank())
            rules = router.setdefault("rules", [])
            if not any(r.get("ruleset") == ruleset_id for r in rules):
                raise ValueError(f"unknown ruleset {ruleset_id!r}")
            first_idx = next(
                i for i, r in enumerate(rules) if r.get("ruleset") == ruleset_id
            )
            kept = [r for r in rules if r.get("ruleset") != ruleset_id]
            insert_at = min(first_idx, len(kept))
            new_rules: list[dict] = []
            for matcher_type, value in matchers:
                new_rules.append(
                    {
                        "id": _next_rule_id(kept + new_rules),
                        "type": matcher_type,
                        "value": value,
                        "target": target,
                        "ruleset": ruleset_id,
                        "ruleset_name": name,
                    }
                )
            kept[insert_at:insert_at] = new_rules
            router["rules"] = kept
            block = next(b for b in _ruleset_blocks(kept) if b["id"] == ruleset_id)
        self.data = _read_raw()
        return block

    def remove_rules(self, ids: list[str]) -> list[dict]:
        """Remove rules by id; returns the removed rules (missing ids ignored)."""
        wanted = set(ids)
        removed: list[dict] = []
        with transaction() as data:
            router = data.setdefault("router", _router_blank())
            kept = []
            for rule in router.get("rules") or []:
                if rule.get("id") in wanted:
                    removed.append(dict(rule))
                else:
                    kept.append(rule)
            router["rules"] = kept
        self.data = _read_raw()
        return removed

    def reorder_rules(self, ids: list[str]) -> tuple[list[dict], bool]:
        """Replace rule evaluation order with a full id permutation.

        Low-level primitive retained for flat/debug operations. A permutation
        that would split a ruleset block is rejected; user-facing reorder works
        at ruleset-block level via :meth:`reorder_rulesets`.
        """
        with transaction() as data:
            router = data.setdefault("router", _router_blank())
            rules = router.get("rules") or []
            _ruleset_blocks(rules)
            current = [str(rule.get("id")) for rule in rules]
            proposed = [str(i) for i in ids]
            _check_permutation(current, proposed, "rule")
            changed = proposed != current
            by_id = {str(rule.get("id")): rule for rule in rules}
            ordered = [by_id[rid] for rid in proposed]
            _ruleset_blocks(ordered)  # validates the proposed contiguity invariant
            router["rules"] = ordered
            ordered_copy = [dict(rule) for rule in router["rules"]]
        self.data = _read_raw()
        return ordered_copy, changed

    def reorder_rulesets(self, ids: list[str]) -> tuple[list[dict], bool]:
        """Replace ruleset block order with a full ruleset-id permutation."""
        with transaction() as data:
            router = data.setdefault("router", _router_blank())
            rules = router.get("rules") or []
            blocks = _ruleset_blocks(rules)
            current = [block["id"] for block in blocks]
            proposed = [str(i) for i in ids]
            _check_permutation(current, proposed, "ruleset")
            changed = proposed != current
            by_id = {block["id"]: block["rules"] for block in blocks}
            router["rules"] = [rule for rsid in proposed for rule in by_id[rsid]]
            ordered = _ruleset_blocks(router["rules"])
        self.data = _read_raw()
        return ordered, changed

    def set_killswitch(self, enabled: bool) -> None:
        """Toggle unmatched-traffic blocking on the router inbound only."""
        with transaction() as data:
            data.setdefault("router", _router_blank())["killswitch"] = bool(enabled)
        self.data = _read_raw()

    def set_lan_direct(self, enabled: bool) -> None:
        """Toggle the built-in LAN/local default-direct rule block (router
        inbound only). The built-in rules themselves are not editable — this
        flag is the whole surface."""
        with transaction() as data:
            data.setdefault("router", _router_blank())["lan_direct"] = bool(enabled)
        self.data = _read_raw()

    def set_tun(self, enabled: bool) -> None:
        """Toggle system-wide TUN mode. The flag is config-relevant (it moves
        ``config_signature``), so the daemon reconciles sing-box on the flip."""
        with transaction() as data:
            data.setdefault("router", _router_blank())["tun"] = bool(enabled)
        self.data = _read_raw()

    def rules_referencing(self, refs: set[tuple[str, str]]) -> dict[str, list[dict]]:
        """``"provider/cid" -> [rule, …]`` from this snapshot (plan-time view;
        the authoritative check re-runs inside the removal transaction)."""
        return _rules_referencing(self.data, refs)

    def update_channel_wg(self, provider: str, cid: str, wg: dict) -> bool:
        """Replace a channel's WireGuard params (used by auto-reconnect to swap in a
        freshly resolved server). Changes the config signature so the daemon
        reconciles sing-box. True if the channel existed."""
        updated = False
        with transaction() as data:
            prov = data["providers"].get(provider) or {}
            ch = (prov.get("channels") or {}).get(cid)
            if ch is not None:
                ch["wg"] = wg
                updated = True
        self.data = _read_raw()
        return updated

    def update_channels_wg(
        self, provider: str, wg_by_cid: dict[str, dict]
    ) -> list[str]:
        """Replace several channels' WireGuard params in ONE transaction.

        The token-replacement commit: every re-resolved channel lands (or none
        does), so a crash can never persist the new credential with only *some*
        of its derived channels. Channels that no longer exist are skipped;
        returns the ids actually updated.
        """
        updated: list[str] = []
        with transaction() as data:
            chans = (data["providers"].get(provider) or {}).get("channels") or {}
            for cid, wg in wg_by_cid.items():
                ch = chans.get(cid)
                if ch is not None:
                    ch["wg"] = wg
                    updated.append(cid)
            # Replacing a token is explicit human intervention. Clear every
            # give-up flag, including channels whose immediate refresh failed,
            # so the daemon may retry them with the new credential.
            for ch in chans.values():
                ch.pop("reconnect", None)
        self.data = _read_raw()
        return updated

    def merge_setup(
        self,
        providers: dict[str, dict],
        rulesets: list[dict],
        killswitch: bool | None,
        lan_direct: bool | None,
        router_port: int | None = None,
    ) -> dict:
        """Merge a validated bundle into the setup in ONE transaction — the
        bundle-import commit point (the replace twin is :meth:`restore_setup`).

        ``providers`` maps provider -> {channel id -> {country, city, label,
        wg, enabled}} (channels upserted by ``(provider, id)`` with
        :meth:`upsert_channel` semantics: port/probe kept, reconnect reset,
        label preserved unless the spec carries one); missing providers are
        created. A spec's optional explicit ``port`` is a declaration: it is
        used on create and re-points an existing channel (clashes raise
        :class:`PortInUseError`, aborting the whole transaction). A spec's
        ``enabled`` is tri-state: explicit True/False applies it, ``None``
        (unstated) keeps an existing channel's state — like the unstated
        router toggles, so a re-applied bundle never undoes an ad-hoc
        ``channels disable`` — and reads enabled for a new channel. A spec
        that disables a channel a routing rule still references raises
        :class:`ReferencedError` — the same restrict-only check as
        :meth:`set_channels_enabled`, run across the whole batch first, so an
        import can never leave a rule pointing at a disabled channel. A
        disabled spec's ``wg`` may be None (resolve-at-enable); it is stored
        as ``{}``. ``rulesets`` ({name, target, matchers: [(type, value), …]})
        append as new blocks at the bottom of the priority order. ``None``
        toggles mean "bundle leaves it unstated" and change nothing;
        ``router_port`` (optional) declares the router entrypoint's contract
        port the same way. Returns the apply summary pieces:
        ``{providers_added, created, updated, unchanged, rulesets_added}``
        (channel entries as ``provider/cid`` refs).
        """
        summary: dict = {
            "providers_added": [],
            "created": [],
            "updated": [],
            "unchanged": [],
            "rulesets_added": [],
        }
        with transaction() as data:
            # The restrict-only check runs before any channel write so a
            # blocked disable aborts the whole merge, not half of it. Only an
            # *explicit* enabled: false disables — unstated changes nothing.
            disabling = {
                (provider, cid)
                for provider, channels in providers.items()
                for cid, spec in channels.items()
                if spec.get("enabled") is False
                and ((data["providers"].get(provider) or {}).get("channels") or {})
                .get(cid, {})
                .get("enabled", True)
            }
            if disabling:
                blockers = _rules_referencing(data, disabling)
                if blockers:
                    raise ReferencedError(blockers)
            for provider, channels in providers.items():
                prov = data["providers"].get(provider)
                if prov is None:
                    prov = data["providers"][provider] = {"channels": {}}
                    summary["providers_added"].append(provider)
                chans = prov.setdefault("channels", {})
                for cid, spec in channels.items():
                    ref = f"{provider}/{cid}"
                    ch = chans.get(cid)
                    label = spec.get("label") or ""
                    port = int(spec.get("port") or 0)
                    # tri-state: None (unstated) resolves to the existing
                    # channel's state, or enabled for a new one
                    enabled = spec.get("enabled")
                    if enabled is None:
                        enabled = ch.get("enabled", True) if ch is not None else True
                    wg = spec["wg"] or {}
                    if ch is None:
                        entry = {
                            "country": spec.get("country", ""),
                            "city": spec.get("city", ""),
                            "port": _claim_port(data, port)
                            if port
                            else _next_free_port(data),
                            "wg": wg,
                        }
                        if enabled:
                            entry["probe"] = {}
                        else:
                            entry["enabled"] = False
                        if label:
                            entry["label"] = label
                        chans[cid] = entry
                        summary["created"].append(ref)
                    elif (
                        ch.get("country", "") == spec.get("country", "")
                        and ch.get("city", "") == spec.get("city", "")
                        and ch.get("wg") == wg
                        and ch.get("enabled", True) == enabled
                        and (not label or ch.get("label", "") == label)
                        and (not port or int(ch.get("port") or 0) == port)
                    ):
                        summary["unchanged"].append(ref)
                    else:
                        ch["country"] = spec.get("country", "")
                        ch["city"] = spec.get("city", "")
                        ch["wg"] = wg
                        if port and port != int(ch.get("port") or 0):
                            ch["port"] = _claim_port(
                                data, port, exclude=(provider, cid)
                            )
                        if label:  # explicit override; otherwise keep the old
                            ch["label"] = label
                        if enabled:
                            ch.pop("enabled", None)
                        else:
                            # same shape as set_channels_enabled: a disabled
                            # channel carries no probe/reconnect bookkeeping
                            ch["enabled"] = False
                            ch.pop("probe", None)
                        # An import is human intervention: forget any reconnect
                        # give-up state so the daemon retries from scratch.
                        ch.pop("reconnect", None)
                        summary["updated"].append(ref)
            for block in rulesets:
                _ensure_channel_target(data, block["target"])
            router = data.setdefault("router", _router_blank())
            rules = router.setdefault("rules", [])
            for block in rulesets:
                rsid = _next_ruleset_id(rules)
                new_rules: list[dict] = []
                for matcher_type, value in block["matchers"]:
                    new_rules.append(
                        {
                            "id": _next_rule_id(rules + new_rules),
                            "type": matcher_type,
                            "value": value,
                            "target": block["target"],
                            "ruleset": rsid,
                            "ruleset_name": block["name"],
                        }
                    )
                rules.extend(new_rules)
                summary["rulesets_added"].append(block["name"])
            if killswitch is not None:
                router["killswitch"] = bool(killswitch)
            if lan_direct is not None:
                router["lan_direct"] = bool(lan_direct)
            if router_port and router_port != int(router.get("port") or 0):
                used = _used_ports(data)
                used.pop(int(router.get("port") or 0), None)
                if router_port in used:
                    raise PortInUseError(router_port, used[router_port])
                router["port"] = router_port
        self.data = _read_raw()
        return summary

    def restore_setup(
        self,
        providers: dict[str, dict],
        rulesets: list[dict],
        killswitch: bool,
        lan_direct: bool,
        router_port: int | None = None,
    ) -> None:
        """Replace the entire setup in one transaction — the bundle-restore
        commit point.

        ``providers`` maps provider -> {channel id -> {country, city, label,
        wg}}; ``rulesets`` is an ordered list of {name, target, matchers:
        [(type, value), …]}. Runtime state is reset (fresh probe, no
        reconnect) and rule/ruleset ids are minted fresh. Auto-assigned ports
        are local allocations, never part of a bundle: a channel whose
        ``(provider, id)`` already exists keeps its current port (a
        same-machine restore preserves the local contract), anything else
        gets a fresh one, and the router contract port is untouched. A spec's
        optional explicit ``port`` (and ``router_port``) is a declaration and
        wins over both; duplicate declarations abort the transaction.
        """
        with transaction() as data:
            old_ports = {
                (provider, cid): int(ch.get("port") or 0)
                for provider, prov in (data.get("providers") or {}).items()
                for cid, ch in (prov.get("channels") or {}).items()
            }
            new_providers: dict[str, dict] = {}
            for provider, channels in providers.items():
                chans: dict[str, dict] = {}
                for cid, spec in channels.items():
                    entry = {
                        "country": spec.get("country", ""),
                        "city": spec.get("city", ""),
                        "port": int(spec.get("port") or 0)
                        or old_ports.get((provider, cid), 0),
                        # None (a disabled resolve-at-enable spec) stores as {}
                        "wg": spec["wg"] or {},
                    }
                    # tri-state: only an explicit false disables; unstated
                    # reads enabled on a whole-setup replace
                    if spec.get("enabled") is not False:
                        entry["probe"] = {}
                    else:
                        entry["enabled"] = False
                    if spec.get("label"):
                        entry["label"] = spec["label"]
                    chans[cid] = entry
                new_providers[provider] = {"channels": chans}
            data["providers"] = new_providers
            router = data.setdefault("router", _router_blank())
            if router_port:
                router["port"] = router_port
            # Declared ports are already in place, so the duplicate check and
            # the fresh allocations below both see them. Ports for new
            # identities are allocated only after the old channel set is gone,
            # so ports freed by the replace are reusable while every kept port
            # (and the router's) stays reserved.
            seen: dict[int, str] = {}
            if int(router.get("port") or 0):
                seen[int(router["port"])] = "the router entrypoint"
            for provider, prov in new_providers.items():
                for cid, ch in prov["channels"].items():
                    port = int(ch.get("port") or 0)
                    if not port:
                        continue
                    if port in seen:
                        raise PortInUseError(port, seen[port])
                    seen[port] = f"channel {provider}/{cid}"
            for prov in new_providers.values():
                for ch in prov["channels"].values():
                    if not ch["port"]:
                        ch["port"] = _next_free_port(data)
            router["killswitch"] = bool(killswitch)
            router["lan_direct"] = bool(lan_direct)
            rules: list[dict] = []
            for i, block in enumerate(rulesets, 1):
                for matcher_type, value in block["matchers"]:
                    rules.append(
                        {
                            "id": f"r{len(rules) + 1}",
                            "type": matcher_type,
                            "value": value,
                            "target": block["target"],
                            "ruleset": f"rs{i}",
                            "ruleset_name": block["name"],
                        }
                    )
            router["rules"] = rules
        self.data = _read_raw()

    def clear_reconnect_all(self) -> int:
        """Drop reconnect bookkeeping from every channel (e.g. on ``alle restart``),
        clearing any ``reconnect_failed`` give-up flags. Returns channels touched."""
        cleared = 0
        with transaction() as data:
            for prov in (data.get("providers") or {}).values():
                for ch in (prov.get("channels") or {}).values():
                    if ch.pop("reconnect", None) is not None:
                        cleared += 1
        self.data = _read_raw()
        return cleared


def config_signature(data: dict) -> str:
    """Digest of only the sing-box-relevant parts of state (providers, channel
    ports + WireGuard params, and the router section) — deliberately excluding
    probe results, so the daemon's probe writes don't trigger a needless
    reconcile. Router rules/kill-switch/port are included so a route edit
    reconciles like a channel edit."""
    import hashlib

    relevant: dict = {}
    for provider, pdata in sorted((data.get("providers") or {}).items()):
        chans = {}
        for cid, ch in sorted((pdata.get("channels") or {}).items()):
            chans[cid] = {"port": ch.get("port"), "wg": ch.get("wg")}
            # Only when disabled (absent-reads-True, like the on-disk key), so
            # existing default-enabled states keep their digest across upgrades
            # while any toggle still moves it and reconciles sing-box.
            if not ch.get("enabled", True):
                chans[cid]["enabled"] = False
        if chans:  # an empty provider produces no inbounds, so it can't move the config
            relevant[provider] = chans
    router = data.get("router") or {}
    if (
        router.get("port")
        or router.get("rules")
        or router.get("killswitch")
        or router.get("tun")
    ):
        # "_router" cannot collide: provider keys are bare lowercase words.
        # lan_direct only matters once a shared-rule inbound exists (router
        # port set, or tun on), which the condition above already guarantees
        # whenever it could change the compiled config.
        relevant["_router"] = {
            "port": router.get("port"),
            "killswitch": bool(router.get("killswitch")),
            "lan_direct": bool(router.get("lan_direct", True)),
            "tun": bool(router.get("tun")),
            "rules": [
                {
                    "id": rule.get("id"),
                    "type": rule.get("type"),
                    "value": rule.get("value"),
                    "target": rule.get("target"),
                }
                for rule in (router.get("rules") or [])
            ],
        }
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()
