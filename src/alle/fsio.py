"""Shared filesystem primitives: interprocess file locks and durable atomic
replacement.

Every read-modify-write store — ``state.json``, ``credentials.yaml``, the
location cache, the setup journal, the generated endpoint contracts
(``control_api.json``/``clash_api.json``), the login-token store, the session
revocation record — serialises its writers with :func:`locked` and persists
with :func:`write_durably`, so the on-disk file is always either the old
complete version or the new complete version — never a torn write — and, once
a write returns, it survives a crash or power loss (both the file's bytes and
its directory entry are fsynced).
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable


def _preserve_owner(path: str | Path, like: Path) -> None:
    """Keep a state file's owner stable across a root write (best-effort).

    A management command run as root — ``docker exec <container> alle …``
    (root is the exec default), a stray ``sudo alle …`` — must not flip a
    0600 state file or a lock file to root-owned: the unprivileged daemon
    would be locked out of its own state dir until something re-chowns it
    (in the container, the next restart). Root inherits the owner of
    ``like`` (the file being replaced, or the state dir for a fresh file);
    for every other caller the file already has the right owner and only
    root may chown, so this is a no-op.
    """
    if os.geteuid() != 0:
        return
    try:
        st = os.stat(like)
        if st.st_uid != 0 or st.st_gid != 0:
            os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass  # never fail the write over ownership cosmetics


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
    existed = lock_path.exists()
    with open(lock_path, "w") as lock:
        if not existed:
            _preserve_owner(lock_path, lock_path.parent)
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
        # A root writer keeps the previous owner (or the state dir's, for a
        # fresh file) so an exec'd/sudo'd mutation never locks the
        # unprivileged daemon out of its own state.
        _preserve_owner(tmp, target if target.exists() else target.parent)
        os.replace(tmp, target)
        if mode is not None:
            os.chmod(target, mode)
        _fsync_dir(target.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def generated_endpoint(
    path: Path,
    validate: Callable[[object], dict | None],
    generate: Callable[[], dict],
) -> dict:
    """Read a generated JSON contract file, or mint and publish a fresh one.

    The shared read-or-generate primitive behind ``control_api.json`` and
    ``clash_api.json``: read, generate, and publish all happen under one
    interprocess lock, so two callers racing to first-generate agree on one
    endpoint (both return it) instead of each minting a different one and
    clobbering the file. ``validate`` maps the parsed JSON to the endpoint
    dict, or ``None`` for a missing/shape-wrong file — which ``generate``
    then replaces wholesale. Publishing goes through :func:`write_durably`
    (0600 — these files carry secrets), so a reader never sees a half-written
    file and a crash mid-write keeps the previous one.
    """
    with locked(path.with_name(path.name + ".lock")):
        cfg = None
        try:
            cfg = validate(json.loads(path.read_text()))
        except (ValueError, OSError):
            pass
        if cfg is not None:
            return cfg
        fresh = generate()
        write_durably(
            path,
            lambda f: (json.dump(fresh, f, indent=2), f.write("\n")),
            prefix=f".{path.stem}-",
            suffix=path.suffix,
            mode=0o600,
        )
        return fresh
