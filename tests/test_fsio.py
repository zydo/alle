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
