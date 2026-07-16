"""Shared filesystem primitives: interprocess locks and durable atomic replace."""

from __future__ import annotations

import fcntl
import os
import stat

import pytest

from alle import fsio


def test_locked_excludes_other_lockers(tmp_path):
    lock = tmp_path / "x.lock"
    with fsio.locked(lock):
        with open(lock, "w") as second:  # a second open file description
            with pytest.raises(OSError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(lock, "w") as second:  # released on exit
        fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_write_durably_replaces_atomically_and_fsyncs(tmp_path, monkeypatch):
    target = tmp_path / "data.json"
    target.write_text("old")
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])

    fsio.write_durably(
        target, lambda f: f.write("new"), prefix=".t-", suffix=".json", mode=0o600
    )

    assert target.read_text() == "new"
    assert len(synced) >= 2  # the replacement file AND the parent directory
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [target]  # no temp leftovers


def test_write_durably_failure_keeps_previous_file(tmp_path):
    target = tmp_path / "data.json"
    target.write_text("old")

    def boom(f):
        f.write("partial")
        raise RuntimeError("mid-write crash")

    with pytest.raises(RuntimeError, match="mid-write crash"):
        fsio.write_durably(target, boom, prefix=".t-", suffix=".json")

    assert target.read_text() == "old"  # the previous complete file survives
    assert list(tmp_path.iterdir()) == [target]


def test_root_writer_preserves_the_previous_owner(tmp_path, monkeypatch):
    """A root-run CLI (`docker exec`, sudo) rewriting a state file must chown
    it back to the unprivileged owner — otherwise the daemon is locked out of
    its own 0600 state until something heals the ownership."""
    target = tmp_path / "state.json"
    target.write_text("old")

    chowned = []
    monkeypatch.setattr(fsio.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        fsio.os, "chown", lambda p, uid, gid: chowned.append((uid, gid))
    )
    # report every path as alle-owned (uid 1000) without disturbing st_mode —
    # pathlib's mkdir/is_dir consult the same os.stat
    monkeypatch.setattr(fsio.os, "stat", _stat_as_uid_1000(os.stat))

    fsio.write_durably(target, lambda f: f.write("new"), prefix=".t-", suffix=".json")

    assert target.read_text() == "new"
    assert chowned == [(1000, 1000)]  # temp chowned to the old owner pre-replace


def test_non_root_writer_never_chowns(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text("old")
    monkeypatch.setattr(fsio.os, "geteuid", lambda: 12345)
    monkeypatch.setattr(
        fsio.os, "chown", lambda *a: pytest.fail("non-root must not chown")
    )
    fsio.write_durably(target, lambda f: f.write("new"), prefix=".t-", suffix=".json")
    assert target.read_text() == "new"


def _stat_as_uid_1000(real_stat):
    """A stat that reports uid/gid 1000 but keeps every other field real, so
    pathlib's own stat-based checks (is_dir, mkdir exist_ok) stay correct."""

    def fake(p, *a, **k):
        s = real_stat(p, *a, **k)
        return os.stat_result(
            (
                s.st_mode,
                s.st_ino,
                s.st_dev,
                s.st_nlink,
                1000,
                1000,
                s.st_size,
                s.st_atime,
                s.st_mtime,
                s.st_ctime,
            )
        )

    return fake


def test_root_lock_creation_inherits_the_state_dir_owner(tmp_path, monkeypatch):
    chowned = []
    monkeypatch.setattr(fsio.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        fsio.os, "chown", lambda p, uid, gid: chowned.append((uid, gid))
    )
    monkeypatch.setattr(fsio.os, "stat", _stat_as_uid_1000(os.stat))
    with fsio.locked(tmp_path / "state.lock"):
        pass
    assert chowned == [(1000, 1000)]
