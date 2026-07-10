"""Shared filesystem primitives: interprocess file locks and durable atomic
replacement.

Every read-modify-write store (``state.json``, ``credentials.yaml``, the
location cache) serialises its writers with :func:`locked` and persists with
:func:`write_durably`, so the on-disk file is always either the old complete
version or the new complete version — never a torn write — and, once a write
returns, it survives a crash or power loss (both the file's bytes and its
directory entry are fsynced).
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


@contextmanager
def locked(lock_path: Path):
    """Hold an exclusive interprocess ``flock`` on ``lock_path``.

    Blocks until the lock is free. The lock file itself carries no data — it
    exists only to be locked — and is deliberately left in place (removing it
    would let a new opener lock a fresh inode while an old holder still holds
    the removed one).
    """
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _fsync_dir(path: Path) -> None:
    """Fsync a directory so a just-renamed entry survives a crash.

    Best-effort: some filesystems refuse to fsync a directory fd; by that
    point the data file itself is already synced, so a refusal only widens
    the (tiny) window in which the *rename* could be lost, never corrupts.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_durably(
    target: Path,
    write: Callable,
    *,
    prefix: str,
    suffix: str,
    mode: int | None = None,
) -> None:
    """Atomically and durably replace ``target``.

    ``write(f)`` receives the open temp file. The temp file is created by
    ``mkstemp`` (0600 from the first byte — safe for secrets under any umask),
    fsynced before the rename, and the parent directory is fsynced after it,
    so a crash at any point leaves either the previous file or the new one,
    both complete and durable.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=prefix, suffix=suffix)
    try:
        with os.fdopen(fd, "w") as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        if mode is not None:
            os.chmod(target, mode)
        _fsync_dir(target.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
