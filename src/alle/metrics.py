"""Durable per-channel traffic counters in ``~/.alle/metrics.db`` (SQLite).

This is the one place alle uses SQLite: cumulative sent/received byte totals per
channel are append-heavy time-series data that outlives any single sing-box
process, so a tiny database beats hammering ``state.json``. Everything else
(state, credentials) stays in human-readable files.

The Clash API only reports *live* connections, each with a lifetime-cumulative
``upload``/``download`` counter that vanishes when the connection closes and
resets when sing-box restarts. So there is no running total to read — the daemon
samples ``/connections`` every probe cycle and the :class:`Accumulator` turns the
per-connection counters into monotonic deltas, which it folds into the durable
per-channel totals here. Short-lived connections that open and close entirely
between two samples are missed; for cumulative usage that approximation is fine.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from alle import paths
from alle.state import tag_to_ref


def _db_path() -> Path:
    return paths.state_dir() / "metrics.db"


@contextmanager
def _db():
    conn = sqlite3.connect(str(_db_path()))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_traffic (
                provider   TEXT    NOT NULL,
                channel    TEXT    NOT NULL,
                sent       INTEGER NOT NULL DEFAULT 0,
                received   INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (provider, channel)
            )
            """
        )
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_delta(provider: str, channel: str, sent: int, received: int) -> None:
    """Fold a (non-negative) byte delta into a channel's cumulative totals."""
    if sent <= 0 and received <= 0:
        return
    now = int(time.time())
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO channel_traffic (provider, channel, sent, received, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, channel) DO UPDATE SET
                sent       = sent + excluded.sent,
                received   = received + excluded.received,
                updated_at = excluded.updated_at
            """,
            (provider, channel, max(0, sent), max(0, received), now),
        )


def totals() -> dict[tuple[str, str], dict]:
    """All stored totals, keyed by ``(provider, channel)``."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT provider, channel, sent, received, updated_at FROM channel_traffic"
        ).fetchall()
    return {
        (provider, channel): {
            "sent": sent,
            "received": received,
            "updated_at": updated_at,
        }
        for provider, channel, sent, received, updated_at in rows
    }


def remove_channel(provider: str, channel: str) -> None:
    """Forget a channel's totals (called when the channel is removed)."""
    with _db() as conn:
        conn.execute(
            "DELETE FROM channel_traffic WHERE provider = ? AND channel = ?",
            (provider, channel),
        )


def remove_provider(provider: str) -> None:
    """Forget every channel's totals under a provider (called on provider removal)."""
    with _db() as conn:
        conn.execute("DELETE FROM channel_traffic WHERE provider = ?", (provider,))


class Accumulator:
    """Turns successive Clash ``/connections`` snapshots into durable deltas.

    Held for the daemon's lifetime. Each connection's cumulative counters only
    grow while it is alive; we remember the last value seen per connection ``id``
    and persist the increase since then. A connection we stop seeing has closed —
    we simply drop it (its bytes were already banked). A counter that goes
    *backwards* (new sing-box process reusing an id, or an id reset) is treated as
    a fresh connection so we bank the new value rather than a negative delta.
    """

    def __init__(self) -> None:
        self._seen: dict[str, tuple[int, int]] = {}  # conn id -> (upload, download)

    @staticmethod
    def _ref(conn: dict) -> tuple[str, str] | None:
        for tag in conn.get("chains") or []:
            ref = tag_to_ref(tag)
            if ref:
                return ref
        return None

    def observe(
        self, connections: list[dict]
    ) -> dict[tuple[str, str], tuple[int, int]]:
        """Bank deltas from one snapshot and return them, keyed by channel ref."""
        deltas: dict[tuple[str, str], list[int]] = {}
        alive: set[str] = set()
        for conn in connections:
            cid = conn.get("id")
            ref = self._ref(conn)
            if not cid or ref is None:
                continue
            alive.add(cid)
            up = int(conn.get("upload") or 0)
            down = int(conn.get("download") or 0)
            last_up, last_down = self._seen.get(cid, (0, 0))
            d_up = up - last_up if up >= last_up else up
            d_down = down - last_down if down >= last_down else down
            self._seen[cid] = (up, down)
            acc = deltas.setdefault(ref, [0, 0])
            acc[0] += d_up
            acc[1] += d_down
        for cid in self._seen.keys() - alive:  # closed connections
            del self._seen[cid]
        banked = {ref: (up, down) for ref, (up, down) in deltas.items() if up or down}
        for (provider, channel), (up, down) in banked.items():
            add_delta(provider, channel, up, down)
        return banked
