"""PID-recycling guards: a pidfile is only believed when the process behind the
number has the command line alle spawned — so a stale file can neither report a
dead daemon as running nor let stop() signal an unrelated process."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from alle import daemon, paths, proc, singbox


def test_command_of_reads_a_live_process():
    cmd = proc.command_of(os.getpid())
    assert cmd is not None
    executable = sys.executable.rsplit("/", 1)[-1].split(".")[0]
    assert executable.casefold() in cmd.casefold()  # e.g. "python" / "Python"


def test_marker_fallback_requires_a_marker_hit():
    # a record with no start time falls back to the command-line marker check
    cmd = proc.command_of(os.getpid())
    assert cmd is not None
    token = cmd.split()[0]  # our own interpreter path always matches
    assert proc.verify({"pid": os.getpid(), "start": None}, (token,)) is True
    assert (
        proc.verify({"pid": os.getpid(), "start": None}, ("definitely-not-in-argv",))
        is False
    )


def test_marker_fallback_rejects_dead_pids():
    child = subprocess.Popen(["sleep", "0"])
    child.wait()  # reaped — the PID no longer exists
    assert proc.verify({"pid": child.pid, "start": None}, ("sleep",)) is False


def test_marker_fallback_rejects_nonsense_pids():
    assert proc.verify({"pid": 0, "start": None}, ("x",)) is False
    assert proc.verify({"pid": -1, "start": None}, ("x",)) is False


def test_start_time_of_reads_a_live_process():
    assert proc.start_time_of(os.getpid()) is not None
    assert proc.start_time_of(-1) is None


def test_start_time_of_rejects_dead_pids():
    child = subprocess.Popen(["sleep", "0"])
    child.wait()  # reaped — the PID no longer exists
    assert proc.start_time_of(child.pid) is None


def test_start_time_of_rejects_an_unreaped_zombie():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and proc.process_state_of(child.pid) != "Z":
            time.sleep(0.01)
        if proc.process_state_of(child.pid) != "Z":
            pytest.skip("platform did not expose the child as a zombie")
        assert proc.start_time_of(child.pid) is None
        assert proc.verify({"pid": child.pid, "start": "any"}, ()) is False
    finally:
        child.wait()


def test_verify_matches_on_recorded_start_time():
    rec = proc.record(os.getpid())
    assert rec["start"] is not None
    # exact identity: markers are irrelevant once the start time matches …
    assert proc.verify(rec, ("definitely-not-in-argv",)) is True
    # … and a mismatched start time means a recycled PID, marker hit or not
    interp = (proc.command_of(os.getpid()) or "x").split()[0]
    fake = {"pid": os.getpid(), "start": "ticks:0-not-this-process"}
    assert proc.verify(fake, (interp,)) is False


def test_pidfile_roundtrip_and_reaped_child():
    path = paths.state_dir() / "roundtrip.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(["sleep", "30"])
    try:
        proc.write_pidfile(path, child.pid)
        assert proc.read_pidfile(path, ()) == child.pid  # no marker needed
    finally:
        child.kill()
        child.wait()
    assert proc.read_pidfile(path, ("sleep",)) is None  # dead = not ours


def test_read_pidfile_accepts_legacy_plain_int_via_marker():
    path = paths.state_dir() / "legacy.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n")
    interp = (proc.command_of(os.getpid()) or "x").split()[0]
    assert proc.read_pidfile(path, (interp,)) == os.getpid()
    assert proc.read_pidfile(path, ("definitely-not-in-argv",)) is None


def test_read_pidfile_rejects_garbage():
    path = paths.state_dir() / "garbage.pid"
    path.parent.mkdir(parents=True, exist_ok=True)
    for text in ("", "not-a-pid", '{"start": "x"}', '["list"]'):
        path.write_text(text)
        assert proc.read_pidfile(path, ("x",)) is None
    assert proc.read_pidfile(path.with_name("absent.pid"), ("x",)) is None


def test_singbox_pidfile_pointing_at_foreign_process_is_ignored():
    # Simulate PID recycling: the pidfile holds a live PID (ours) that is not
    # a sing-box. It must read as "not running", not become a kill target.
    (paths.state_dir() / "singbox.pid").write_text(str(os.getpid()))
    assert singbox.Runner().running_pid() is None


def test_applier_pidfile_pointing_at_foreign_process_is_ignored():
    (paths.state_dir() / "applier.pid").write_text(str(os.getpid()))
    assert daemon.running_pid() is None


def test_recorded_pidfile_with_stale_start_time_is_ignored():
    # Even a marker-perfect command line is rejected when the start time
    # says the PID was recycled since the record was written.
    import json

    rec = {"pid": os.getpid(), "start": "ticks:0-not-this-process"}
    (paths.state_dir() / "applier.pid").write_text(json.dumps(rec))
    assert daemon.running_pid() is None
