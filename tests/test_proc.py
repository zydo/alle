"""PID-recycling guards: a pidfile is only believed when the process behind the
number has the command line alle spawned — so a stale file can neither report a
dead daemon as running nor let stop() signal an unrelated process."""

from __future__ import annotations

import os
import subprocess
import sys

from alle import daemon, paths, proc, singbox


def test_command_of_reads_a_live_process():
    cmd = proc.command_of(os.getpid())
    assert cmd is not None
    assert sys.executable.rsplit("/", 1)[-1].split(".")[0] in cmd  # e.g. "python"


def test_alive_matching_requires_a_marker_hit():
    cmd = proc.command_of(os.getpid())
    assert cmd is not None
    token = cmd.split()[0]  # our own interpreter path always matches
    assert proc.alive_matching(os.getpid(), (token,)) is True
    assert proc.alive_matching(os.getpid(), ("definitely-not-in-argv",)) is False


def test_alive_matching_rejects_dead_pids():
    child = subprocess.Popen(["sleep", "0"])
    child.wait()  # reaped — the PID no longer exists
    assert proc.alive_matching(child.pid, ("sleep",)) is False


def test_alive_matching_rejects_nonsense_pids():
    assert proc.alive_matching(0, ("x",)) is False
    assert proc.alive_matching(-1, ("x",)) is False


def test_singbox_pidfile_pointing_at_foreign_process_is_ignored():
    # Simulate PID recycling: the pidfile holds a live PID (ours) that is not
    # a sing-box. It must read as "not running", not become a kill target.
    (paths.state_dir() / "singbox.pid").write_text(str(os.getpid()))
    assert singbox.Runner().running_pid() is None


def test_applier_pidfile_pointing_at_foreign_process_is_ignored():
    (paths.state_dir() / "applier.pid").write_text(str(os.getpid()))
    assert daemon.running_pid() is None
