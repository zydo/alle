"""Cross-file setup transactions: one interprocess lock plus a rollback
journal spanning ``credentials.yaml`` and ``state.json``.

The two files have no shared file-level transaction, so any compound setup
change (provider add/remove, token replacement, bundle import/restore) writes
them in two steps. This module makes those compounds all-or-nothing:

* ``setup.lock`` serialises compound operations against each other, so e.g. a
  provider re-add can never interleave with a provider removal and resurrect
  a half-removed provider. (The per-file locks still serialise raw writes;
  this lock is one level up, around the multi-file sequence.)
* ``setup-journal.json`` records the pre-operation credentials before the
  first write. The **state transaction is the commit point**: callers invoke
  :meth:`SetupTxn.commit` immediately after it. Until then, any failure —
  an exception, or a crash healed by the next :func:`recover` — rolls
  ``credentials.yaml`` back to the journalled copy, leaving the whole setup
  exactly as it was. After commit, the journal is gone and later steps
  (metrics cleanup, daemon pokes) are best-effort post-commit work.

The residual window is a crash *between* the state commit and the journal
removal: recovery then restores the pre-op credentials against the already-
committed state. For every compound op the state side is authoritative
(tokens re-validate on the next provider op; removed providers just leave no
credential to restore into use), so the mismatch is at worst a stale-or-
orphaned credential — never a half-applied setup.

The journal holds credentials, so it is written 0600 and lives beside
``credentials.yaml`` under the 0700 state dir.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

from alle import applog, credentials, fsio, paths


def _lock_path() -> Path:
    return paths.state_dir() / "setup.lock"


def _journal_path() -> Path:
    return paths.state_dir() / "setup-journal.json"


class SetupTxn:
    """The in-flight compound operation. ``commit()`` marks the point of no
    rollback — call it immediately after the state transaction that makes the
    operation real."""

    def __init__(self) -> None:
        self.committed = False

    def commit(self) -> None:
        self.committed = True
        _clear_journal()


def _clear_journal() -> None:
    try:
        _journal_path().unlink()
    except FileNotFoundError:
        pass


def _restore_credentials(snapshot: dict) -> None:
    with credentials.transaction() as data:
        data.clear()
        data.update(snapshot)


def _recover_locked() -> bool:
    """Roll back the credentials of a compound op that crashed before its
    commit point. Caller holds the setup lock. True if anything was done."""
    p = _journal_path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        return False
    except OSError as e:
        applog.log(f"setup journal unreadable ({e}); recovery skipped")
        return False
    try:
        entry = json.loads(text)
        snapshot = entry["credentials"]
        if not isinstance(snapshot, dict):
            raise ValueError("credentials is not an object")
    except (ValueError, KeyError, TypeError) as e:
        # No usable pre-op copy — move the journal aside (never lose bytes
        # that might still help manual recovery) and stop blocking setup ops.
        backup = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
        try:
            p.rename(backup)
        except OSError:
            _clear_journal()
        applog.log(f"setup journal corrupt ({e}); moved to {backup.name}")
        return False
    op = str(entry.get("op", "unknown"))
    _restore_credentials(snapshot)
    _clear_journal()
    applog.log(
        f"rolled back credentials of an interrupted setup change ({op}); "
        "the operation did not reach its commit point"
    )
    return True


def recover() -> bool:
    """Heal a compound op that crashed mid-way (daemon startup calls this).

    Takes the setup lock, so it cannot race a live compound operation. True
    if a rollback was performed.
    """
    with fsio.locked(_lock_path()):
        return _recover_locked()


@contextmanager
def setup_transaction(op: str):
    """Run one all-or-nothing compound setup change.

    Usage::

        with setup_transaction("token update") as txn:
            ...stage / resolve (no writes)...
            credentials.set_(...)          # journalled — rolled back on failure
            store.update_channels_wg(...)  # ONE state txn: the commit point
            txn.commit()                   # no rollback past this line
        ...best-effort post-commit steps (metrics, daemon poke)...

    Never nest: the setup lock is a plain flock and self-deadlocks.
    """
    with fsio.locked(_lock_path()):
        _recover_locked()  # a crashed predecessor must not leak into this op
        snapshot = credentials.snapshot()
        fsio.write_durably(
            _journal_path(),
            lambda f: json.dump({"op": op, "credentials": snapshot}, f),
            prefix=".setup-journal-",
            suffix=".json",
            mode=0o600,  # carries credentials
        )
        txn = SetupTxn()
        try:
            yield txn
        except BaseException:
            if not txn.committed:
                _restore_credentials(snapshot)
            _clear_journal()
            raise
        _clear_journal()  # clean exit without commit() == nothing to roll back
