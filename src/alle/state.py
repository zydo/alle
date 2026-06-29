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

BASE_PORT = 8888  # local proxy ports are auto-assigned from here upward
STATE_VERSION = 1


def _slug(text: str) -> str:
    """Filesystem/tag-safe lowercase slug: 'United States' -> 'united_states'."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "channel"


def _port_available(port: int) -> bool:
    """True if nothing is currently bound to 127.0.0.1:<port>."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


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
                    )
                )
        return out

    def provider_channels(self, provider: str) -> list[Channel]:
        return [c for c in self.channels() if c.provider == provider]

    def get_channel(self, provider: str, cid: str) -> Channel | None:
        return next(
            (c for c in self.channels() if c.provider == provider and c.id == cid), None
        )

    def _used_ports(self) -> set[int]:
        return {c.port for c in self.channels() if c.port}

    def _next_port(self) -> int:
        used = self._used_ports()
        port = BASE_PORT
        while port in used or not _port_available(port):
            port += 1
        return port

    def _next_id(self, provider: str, country: str, city: str) -> str:
        """Auto-name like ``united_states_1`` / ``united_states_san_francisco_2``."""
        base = _slug(f"{country} {city}".strip()) if (country or city) else "channel"
        taken = {c.id for c in self.provider_channels(provider)}
        n = 1
        while f"{base}_{n}" in taken:
            n += 1
        return f"{base}_{n}"

    def add_channel(self, provider: str, country: str, city: str, wg: dict) -> Channel:
        cid = self._next_id(provider, country, city)
        port = self._next_port()
        with transaction() as data:
            prov = data["providers"].setdefault(provider, {"channels": {}})
            prov.setdefault("channels", {})[cid] = {
                "country": country,
                "city": city,
                "port": port,
                "wg": wg,
                "probe": {},
            }
        self.data = _read_raw()
        return self.get_channel(provider, cid)  # type: ignore[return-value]

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
