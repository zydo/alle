"""Scheduled local backups of the setup bundle.

Settings live in ``state.json`` under the top-level ``backup`` key (absent =
disabled). The key is deliberately outside :func:`alle.state.config_signature`,
so backup configuration and backup runs never trigger a sing-box reconcile.

The daemon calls :func:`run_due` on a slow cadence; due-ness is derived from
the newest backup file's mtime, never from a stored timestamp — the schedule
self-heals (an emptied directory means "back up now") and a run writes nothing
but the backup itself. Backups only happen while the daemon runs: the daemon
*is* alle's runtime, and alle installs no timers of its own. A backup pass is
pure local file I/O — the no-background-traffic posture holds.

A bundle is a secret (WireGuard private keys, provider tokens), so the
destination must never be a weakly protected location: the directory is
created ``0o700`` and must be a real user-owned directory (no symlink, no
group/world write), and every backup file is written ``0o600`` from the first
byte. Retention prunes only ``alle-backup-*.yaml`` files, never anything else.
Once bundle encryption exists, scheduled backups will default to the
encrypted form.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from alle import applog, bundle, paths
from alle.state import Store

DEFAULT_EVERY_HOURS = 24.0
DEFAULT_KEEP = 7
_PREFIX = "alle-backup-"
_SUFFIX = ".yaml"


class BackupError(Exception):
    """A backup could not be configured or written."""


def default_dir() -> Path:
    return paths.state_dir() / "backups"


def settings(store: Store | None = None) -> dict:
    """The resolved backup settings (defaults filled in, ``dir`` absolute)."""
    raw = (store or Store.load()).data.get("backup") or {}
    directory = str(raw.get("dir") or default_dir())
    return {
        "enabled": bool(raw.get("enabled")),
        "dir": str(Path(directory).expanduser()),
        "every_hours": float(raw.get("every_hours") or DEFAULT_EVERY_HOURS),
        "keep": int(raw.get("keep") or DEFAULT_KEEP),
    }


def prepare_dir(directory: str | Path) -> Path:
    """Create/verify the destination directory; raise :class:`BackupError`
    unless it is a real, user-owned, non-group/world-writable directory."""
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise BackupError(f"backup dir must be an absolute path: {path}")
    # parents may pre-exist with their own modes; only the leaf is created
    # strict. exist_ok covers the common re-run; the checks below are the gate.
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode):
        raise BackupError(f"backup dir must not be a symlink: {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise BackupError(f"backup dir is not a directory: {path}")
    if st.st_uid != os.getuid():
        raise BackupError(f"backup dir must be owned by you: {path}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise BackupError(
            f"backup dir must not be group/world-writable (bundles are "
            f"secrets): chmod go-w {path}"
        )
    return path


def backup_files(directory: Path) -> list[Path]:
    """This rotation's backup files, newest first. Only ``alle-backup-*.yaml``
    is ever considered ours — retention must not touch anything else."""
    try:
        found = [
            p
            for p in directory.iterdir()
            if p.name.startswith(_PREFIX) and p.name.endswith(_SUFFIX) and p.is_file()
        ]
    except FileNotFoundError:
        return []
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _write_secret(path: Path, text: str) -> None:
    # 0600 from the first byte; O_EXCL — the timestamped name never exists
    # unless two exports collide within a second, and then losing one is right
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def run(store: Store | None = None, *, force: bool = False) -> dict | None:
    """Write one backup if due (or ``force``); prune; return a report.

    Returns ``None`` when there is nothing to do (disabled without ``force``,
    or the newest backup is younger than the interval). Raises
    :class:`BackupError` on an unusable destination.
    """
    conf = settings(store)
    if not conf["enabled"] and not force:
        return None
    directory = prepare_dir(conf["dir"])
    existing = backup_files(directory)
    if not force and existing:
        age = time.time() - existing[0].stat().st_mtime
        if age < conf["every_hours"] * 3600.0:
            return None
    text = bundle.dumps(bundle.export_bundle())
    name = _PREFIX + time.strftime("%Y%m%d-%H%M%S") + _SUFFIX
    target = directory / name
    if target.exists():  # same-second rerun (force twice, tests): keep the first
        return {"path": str(target), "pruned": [], "kept": len(existing)}
    _write_secret(target, text)
    keep = max(1, conf["keep"])
    pruned: list[str] = []
    for old in backup_files(directory)[keep:]:
        old.unlink(missing_ok=True)
        pruned.append(old.name)
    kept = len(backup_files(directory))
    applog.log(
        f"backup written: {target}"
        + (f"; pruned {len(pruned)} old backup(s)" if pruned else "")
    )
    return {"path": str(target), "pruned": pruned, "kept": kept}


_last_error: dict = {"text": None, "at": float("-inf")}


def run_due() -> None:
    """The daemon's pass: back up when due, never raise, don't spam the log
    (a persistent failure is logged on change and then at most hourly)."""
    try:
        run()
    except Exception as e:  # noqa: BLE001 — a failed backup must not hurt the daemon
        message = str(e)
        now = time.monotonic()
        if message != _last_error["text"] or now - _last_error["at"] >= 3600:
            applog.log(f"scheduled backup failed: {e}")
            _last_error.update(text=message, at=now)
    else:
        _last_error["text"] = None
