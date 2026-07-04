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
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from alle import paths

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
    for _ in range(100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in used:
            return port
    raise RuntimeError("could not allocate an unused local proxy port")


def _next_id(taken: set, country: str, city: str, label: str | None = None) -> str:
    """Auto-name like ``united_states_1`` / ``united_states_san_francisco_2``.

    API channels name themselves from country/city; imported config channels
    pass an explicit ``label`` (the .conf file name) since they have no location.
    ``taken`` is the set of ids already used within the provider.
    """
    if label:
        base = _slug(label)
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
    wg: dict = field(default_factory=dict)
    probe: dict = field(default_factory=dict)
    reconnect: dict = field(default_factory=dict)

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
    return {"version": STATE_VERSION, "providers": {}}


def _read_raw() -> dict:
    p = _state_path()
    if not p.exists():
        return _blank()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return _blank()
    data.setdefault("version", STATE_VERSION)
    data.setdefault("providers", {})
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
        """Remove a provider and all its channels. Returns channels removed."""
        with transaction() as data:
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
        self, provider: str, country: str, city: str, wg: dict, label: str | None = None
    ) -> Channel:
        # id and port are allocated inside the transaction, against the locked
        # on-disk state — two concurrent adds can never pick the same slot.
        with transaction() as data:
            prov = data["providers"].setdefault(provider, {"channels": {}})
            chans = prov.setdefault("channels", {})
            cid = _next_id(set(chans), country, city, label)
            port = _next_free_port(data)
            chans[cid] = {
                "country": country,
                "city": city,
                "port": port,
                "wg": wg,
                "probe": {},
            }
        self.data = _read_raw()
        return self.get_channel(provider, cid)  # type: ignore[return-value]

    def upsert_channel(
        self, provider: str, label: str, country: str, city: str, wg: dict
    ) -> tuple[Channel, bool]:
        """Create or update a channel with a *fixed* id derived from ``label``.

        Returns ``(channel, created)``. Unlike :meth:`add_channel` (which appends
        ``_1``/``_2`` so one location can hold several servers), this keys the
        channel on ``label`` — the config file name — so re-importing the same
        ``.conf`` updates the WireGuard params and re-parsed location *in place*,
        keeping the id and local port stable. A config's keys rotate on every
        regeneration while the server (the channel) stays the same, so the file
        name, not the key, is the identity.
        """
        cid = _slug(label)
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
            else:
                ch["country"] = country
                ch["city"] = city
                ch["wg"] = (
                    wg  # keep port + probe; the daemon re-probes on the wg change
                )
                # A re-import is human intervention: forget any reconnect give-up
                # state so the daemon tries the fresh config from scratch.
                ch.pop("reconnect", None)
        self.data = _read_raw()
        return self.get_channel(provider, cid), created  # type: ignore[return-value]

    def remove_channel(self, provider: str, cid: str) -> bool:
        removed = False
        with transaction() as data:
            prov = data["providers"].get(provider) or {}
            removed = (prov.get("channels") or {}).pop(cid, None) is not None
        self.data = _read_raw()
        return removed

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
    ports + WireGuard params) — deliberately excluding probe results, so the
    daemon's probe writes don't trigger a needless reconcile."""
    import hashlib

    relevant = {}
    for provider, pdata in sorted((data.get("providers") or {}).items()):
        chans = {}
        for cid, ch in sorted((pdata.get("channels") or {}).items()):
            chans[cid] = {"port": ch.get("port"), "wg": ch.get("wg")}
        if chans:  # an empty provider produces no inbounds, so it can't move the config
            relevant[provider] = chans
    blob = json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()
