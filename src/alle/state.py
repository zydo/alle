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
from alle.constants import INBOUND_PREFIX, OUTBOUND_PREFIX

STATE_VERSION = 1


def _slug(text: str) -> str:
    """Filesystem/tag-safe lowercase slug: 'United States' -> 'united_states'."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "channel"


def channel_id_from_filename(filename: str) -> str:
    """The channel id a ``.conf`` filename resolves to (the same slug
    :meth:`Store.upsert_channel` keys on). Public so callers can look a config
    channel up by name before importing it."""
    return _slug(filename)


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
        return f"{INBOUND_PREFIX}{self.provider}-{self.id}"

    @property
    def outbound_tag(self) -> str:
        return f"{OUTBOUND_PREFIX}{self.provider}-{self.id}"

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


def _ensure_channel_target(data: dict, target: str) -> None:
    provider, _, cid = target.partition("/")
    if cid:
        chans = (data["providers"].get(provider) or {}).get("channels") or {}
        if cid not in chans:
            raise ValueError(f"no channel {target!r} to route to")


def _normalize_rulesets(data: dict) -> None:
    """Make every loaded rule belong to a contiguous ruleset.

    Legacy flat rows are folded into one auto-named ruleset per contiguous
    same-target run. Rows that already carry ruleset metadata are left intact,
    except missing names inherit a target-derived display name. Mixed old/new
    files are rare but safe: old rows become their own runs without disturbing
    existing grouped blocks.
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
    _normalize_rulesets(data)
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
            block = next(
                (b for b in _ruleset_blocks(kept) if b["id"] == ruleset_id), None
            )
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
            seen: set[str] = set()
            dupes: list[str] = []
            for rsid in proposed:
                if rsid in seen and rsid not in dupes:
                    dupes.append(rsid)
                seen.add(rsid)
            if dupes:
                raise ValueError(f"duplicate ruleset id(s): {', '.join(dupes)}")
            current_set = set(current)
            proposed_set = set(proposed)
            unknown = [rsid for rsid in proposed if rsid not in current_set]
            if unknown:
                raise ValueError(f"unknown ruleset id(s): {', '.join(unknown)}")
            missing = [rsid for rsid in current if rsid not in proposed_set]
            if missing:
                raise ValueError(f"missing ruleset id(s): {', '.join(missing)}")
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

    def restore_setup(
        self,
        providers: dict[str, dict],
        rulesets: list[dict],
        killswitch: bool,
        lan_direct: bool,
    ) -> None:
        """Replace the entire setup in one transaction — the bundle-restore
        commit point.

        ``providers`` maps provider -> {channel id -> {country, city, label,
        wg}}; ``rulesets`` is an ordered list of {name, target, matchers:
        [(type, value), …]}. Runtime state is reset (fresh probe, no
        reconnect) and rule/ruleset ids are minted fresh. Ports are local
        allocations, never part of a bundle: a channel whose ``(provider,
        id)`` already exists keeps its current port (a same-machine restore
        preserves the local contract), anything else gets a fresh one, and
        the router contract port is untouched.
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
                        "port": old_ports.get((provider, cid), 0),
                        "wg": spec["wg"],
                        "probe": {},
                    }
                    if spec.get("label"):
                        entry["label"] = spec["label"]
                    chans[cid] = entry
                new_providers[provider] = {"channels": chans}
            data["providers"] = new_providers
            # Ports for new identities are allocated only after the old
            # channel set is gone, so ports freed by the replace are reusable
            # while every kept port (and the router's) stays reserved.
            for prov in new_providers.values():
                for ch in prov["channels"].values():
                    if not ch["port"]:
                        ch["port"] = _next_free_port(data)
            router = data.setdefault("router", _router_blank())
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
