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

import json
import os
import re
import socket
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from alle import applog, paths

STATE_VERSION = 1


def _slug(text: str) -> str:
    """Filesystem/tag-safe lowercase slug: 'United States' -> 'united_states'."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "channel"


def _next_free_port(data: dict) -> int:
    """Ask the OS for an available loopback port that no channel already claims.

    Called inside a transaction so the allocation is made against the locked,
    current state. The temporary socket is closed before sing-box later binds the
    port, so another local process can theoretically race us, but this avoids
    hard-coded port ranges and lets the OS pick from its ephemeral pool.
    """
    used = {
        int(ch.get("port") or 0)
        for prov in (data.get("providers") or {}).values()
        for ch in (prov.get("channels") or {}).values()
    }
    used.add(int((data.get("router") or {}).get("port") or 0))
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
        return f"in-{self.provider}-{self.id}"

    @property
    def outbound_tag(self) -> str:
        return f"out-{self.provider}-{self.id}"

    @property
    def location(self) -> str:
        if self.city:
            return f"{self.city}, {self.country}"
        return self.country or "—"


def tag_to_ref(tag: str) -> tuple[str, str] | None:
    """Parse an ``in-/out-<provider>-<id>`` tag back into ``(provider, id)``."""
    parts = tag.split("-", 2)
    if len(parts) == 3 and parts[0] in ("in", "out"):
        return parts[1], parts[2]
    return None


# ---- the store -------------------------------------------------------------


def _state_path() -> Path:
    return paths.state_dir() / "state.json"


def _lock_path() -> Path:
    return paths.state_dir() / "state.lock"


def _blank() -> dict:
    return {"version": STATE_VERSION, "providers": {}, "router": _router_blank()}


def _router_blank() -> dict:
    """The router entrypoint's state: contract port (0 = not yet allocated),
    the explicit kill-switch flag, the built-in LAN-direct toggle, and the
    ordered rule list. ``lan_direct`` defaults **on** — readers must treat an
    absent key as True so pre-existing state files inherit the default."""
    return {"port": 0, "killswitch": False, "lan_direct": True, "rules": []}


class ReferencedError(RuntimeError):
    """Removal refused: routing rules still reference the channel(s).

    ``blockers`` maps ``"provider/cid"`` to the referencing rule dicts. Raised
    from *inside* the removal transaction so the restrict-only invariant holds
    under concurrent writers, not just at CLI plan time.
    """

    def __init__(self, blockers: dict[str, list[dict]]):
        self.blockers = blockers
        super().__init__("channel(s) are referenced by routing rules")


def _rules_referencing(data: dict, refs: set[tuple[str, str]]) -> dict[str, list[dict]]:
    """``"provider/cid" -> [rule, …]`` for rules targeting any of ``refs``."""
    out: dict[str, list[dict]] = {}
    for rule in (data.get("router") or {}).get("rules") or []:
        provider, _, cid = str(rule.get("target", "")).partition("/")
        if cid and (provider, cid) in refs:
            out.setdefault(f"{provider}/{cid}", []).append(dict(rule))
    return out


def _quarantine(p: Path, err: Exception) -> None:
    """Move an unparseable state/credentials file aside instead of losing it.

    Every mutation is a read-modify-write, so silently treating a corrupt file
    as empty would make the *next* write persist that emptiness — destroying
    every channel and its WireGuard keys with no trace. Renaming preserves the
    bytes for manual recovery and makes the failure loud.
    """
    backup = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
    try:
        os.replace(p, backup)
    except OSError:
        return  # can't move it; the caller still proceeds from a blank view
    msg = f"{p.name} is corrupt ({err}); moved to {backup.name}, starting empty"
    applog.log(msg)
    print(f"alle: {msg}", file=sys.stderr)


def _read_raw() -> dict:
    p = _state_path()
    if not p.exists():
        return _blank()
    try:
        text = p.read_text()
    except OSError:
        return _blank()
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("root is not a JSON object")
    except ValueError as e:
        _quarantine(p, e)
        return _blank()
    data.setdefault("version", STATE_VERSION)
    data.setdefault("providers", {})
    data.setdefault("router", _router_blank())
    return data


def _write_raw(data: dict) -> None:
    """Atomically replace state.json (write temp in the same dir, then rename)."""
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".state-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, p)
        os.chmod(p, 0o600)  # carries WireGuard private keys
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def transaction():
    """Exclusive read-modify-write of state.json under an OS file lock.

    Yields the mutable raw dict; whatever it looks like on exit is written back
    atomically. Serialises the CLI's structural edits against the daemon's probe
    writes so neither clobbers the other.
    """
    import fcntl

    lp = _lock_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    with open(lp, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = _read_raw()
            yield data
            _write_raw(data)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


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

    def remove_provider(self, provider: str) -> int:
        """Remove a provider and all its channels. Returns channels removed.

        Raises :class:`ReferencedError` if any of its channels is still the
        target of a routing rule (restrict-only removal — checked inside the
        transaction so a rule added concurrently cannot slip through).
        """
        with transaction() as data:
            chans = (data["providers"].get(provider) or {}).get("channels") or {}
            blockers = _rules_referencing(data, {(provider, cid) for cid in chans})
            if blockers:
                raise ReferencedError(blockers)
            prov = data["providers"].pop(provider, None)
            count = len(prov.get("channels", {})) if prov else 0
        self.data = _read_raw()
        return count

    # ---- channels ----------------------------------------------------------
    def channels(self) -> list[Channel]:
        out: list[Channel] = []
        for provider, pdata in sorted(self.providers.items()):
            for cid, ch in sorted((pdata.get("channels") or {}).items()):
                out.append(
                    Channel(
                        provider=provider,
                        id=cid,
                        port=int(ch.get("port", 0)),
                        country=ch.get("country", ""),
                        city=ch.get("city", ""),
                        label=ch.get("label", ""),
                        wg=ch.get("wg", {}),
                        probe=ch.get("probe", {}),
                        reconnect=ch.get("reconnect", {}),
                    )
                )
        return out

    def provider_channels(self, provider: str) -> list[Channel]:
        return [c for c in self.channels() if c.provider == provider]

    def get_channel(self, provider: str, cid: str) -> Channel | None:
        return next(
            (c for c in self.channels() if c.provider == provider and c.id == cid), None
        )

    def add_channel(
        self, provider: str, country: str, city: str, wg: dict, label: str = ""
    ) -> Channel:
        # id and port are allocated inside the transaction, against the locked
        # on-disk state — two concurrent adds can never pick the same slot.
        # ``label`` is the optional display label, not part of the id.
        with transaction() as data:
            prov = data["providers"].setdefault(provider, {"channels": {}})
            chans = prov.setdefault("channels", {})
            cid = _next_id(set(chans), country, city)
            port = _next_free_port(data)
            chans[cid] = {
                "country": country,
                "city": city,
                "port": port,
                "wg": wg,
                "probe": {},
            }
            if label:
                chans[cid]["label"] = label
        self.data = _read_raw()
        return self.get_channel(provider, cid)  # type: ignore[return-value]

    def upsert_channel(
        self,
        provider: str,
        filename: str,
        country: str,
        city: str,
        wg: dict,
        label: str = "",
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
        """
        cid = _slug(filename)
        with transaction() as data:
            prov = data["providers"].setdefault(provider, {"channels": {}})
            chans = prov.setdefault("channels", {})
            ch = chans.get(cid)
            created = ch is None
            if ch is None:
                chans[cid] = {
                    "country": country,
                    "city": city,
                    "port": _next_free_port(data),
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
                if label:  # explicit override; otherwise the existing label stays
                    ch["label"] = label
                # A re-import is human intervention: forget any reconnect give-up
                # state so the daemon tries the fresh config from scratch.
                ch.pop("reconnect", None)
        self.data = _read_raw()
        return self.get_channel(provider, cid), created  # type: ignore[return-value]

    def remove_channel(self, provider: str, cid: str) -> bool:
        """Remove one channel. Raises :class:`ReferencedError` if a routing rule
        still targets it (restrict-only removal, enforced in the transaction)."""
        removed = False
        with transaction() as data:
            blockers = _rules_referencing(data, {(provider, cid)})
            if blockers:
                raise ReferencedError(blockers)
            prov = data["providers"].get(provider) or {}
            removed = (prov.get("channels") or {}).pop(cid, None) is not None
        self.data = _read_raw()
        return removed

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
        with transaction() as data:
            prov = data["providers"].get(provider) or {}
            ch = (prov.get("channels") or {}).get(cid)
            if ch is not None:
                ch["probe"] = probe
        self.data = _read_raw()

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

    def add_rule(self, matcher_type: str, value: str, target: str) -> dict:
        """Append a routing rule (order is law) and return it with its id.

        A channel target must exist at append time — verified inside the
        transaction (raises ``ValueError``), the mirror image of the removal
        guard: a rule can never be born dangling, and a referenced channel can
        never be removed.
        """
        with transaction() as data:
            provider, _, cid = target.partition("/")
            if cid:
                chans = (data["providers"].get(provider) or {}).get("channels") or {}
                if cid not in chans:
                    raise ValueError(f"no channel {target!r} to route to")
            router = data.setdefault("router", _router_blank())
            rules = router.setdefault("rules", [])
            taken = [
                int(r["id"][1:])
                for r in rules
                if str(r.get("id", "")).startswith("r") and r["id"][1:].isdigit()
            ]
            rule = {
                "id": f"r{max(taken, default=0) + 1}",
                "type": matcher_type,
                "value": value,
                "target": target,
            }
            rules.append(rule)
        self.data = _read_raw()
        return dict(rule)

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
        """Replace rule evaluation order with a full id permutation."""
        with transaction() as data:
            router = data.setdefault("router", _router_blank())
            rules = router.get("rules") or []
            current = [str(rule.get("id")) for rule in rules]
            proposed = [str(i) for i in ids]
            seen: set[str] = set()
            dupes: list[str] = []
            for rid in proposed:
                if rid in seen and rid not in dupes:
                    dupes.append(rid)
                seen.add(rid)
            if dupes:
                raise ValueError(f"duplicate rule id(s): {', '.join(dupes)}")
            current_set = set(current)
            proposed_set = set(proposed)
            unknown = [rid for rid in proposed if rid not in current_set]
            if unknown:
                raise ValueError(f"unknown rule id(s): {', '.join(unknown)}")
            missing = [rid for rid in current if rid not in proposed_set]
            if missing:
                raise ValueError(f"missing rule id(s): {', '.join(missing)}")
            changed = proposed != current
            by_id = {str(rule.get("id")): rule for rule in rules}
            router["rules"] = [by_id[rid] for rid in proposed]
            ordered = [dict(rule) for rule in router["rules"]]
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
        if chans:  # an empty provider produces no inbounds, so it can't move the config
            relevant[provider] = chans
    router = data.get("router") or {}
    if router.get("port") or router.get("rules") or router.get("killswitch"):
        # "_router" cannot collide: provider keys are bare lowercase words.
        # lan_direct only matters once the router inbound exists (port set),
        # which the condition above already guarantees whenever it could
        # change the compiled config.
        relevant["_router"] = {
            "port": router.get("port"),
            "killswitch": bool(router.get("killswitch")),
            "lan_direct": bool(router.get("lan_direct", True)),
            "rules": router.get("rules") or [],
        }
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()
