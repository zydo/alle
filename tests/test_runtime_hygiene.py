"""Regression gates for pytest-owned appliers, sing-box, and temp homes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from alle import daemon, proc, singbox
from _runtime_hygiene import RuntimeSession, record_is_live, recover_stale_sessions

ROOT = Path(__file__).resolve().parents[1]
_FAILED_RUNTIME: dict = {}
DATA_PLANE_START_TIMEOUT = 60.0


class _ExpectedLifecycleFailure(Exception):
    """The one failure the teardown regression is allowed to expect."""


def test_background_start_is_inert_without_the_capability(runtime_guard):
    daemon.ensure_running()
    assert runtime_guard.blocked_calls == 1
    assert not (runtime_guard.home / "applier.pid").exists()
    assert not (runtime_guard.home / "bin").exists()


def test_real_runtime_is_reaped_before_its_home(real_background_runtime):
    handle = real_background_runtime
    parent = handle.home.parent
    handle.start()
    applier = handle.wait_for("applier")
    # Fresh hosted macOS runners can spend more than 20 seconds cold-starting
    # the detached interpreter and validating the staged sing-box executable.
    # This gate owns cleanup, not startup performance, but remains bounded.
    data_plane = handle.wait_for("singbox", timeout=DATA_PLANE_START_TIMEOUT)

    assert record_is_live(applier["record"])
    assert record_is_live(data_plane["record"])
    assert singbox.bin_path().parent != handle.home / "bin"

    handle.stop()
    assert not record_is_live(applier["record"])
    assert not record_is_live(data_plane["record"])
    assert not parent.exists()
    time.sleep(0.2)
    assert not parent.exists(), "a detached child recreated its deleted test home"


@pytest.mark.xfail(
    strict=True,
    raises=_ExpectedLifecycleFailure,
    reason="exercise fixture cleanup after a test failure",
)
def test_failing_test_still_reaps_its_runtime(real_background_runtime):
    handle = real_background_runtime
    handle.start()
    _FAILED_RUNTIME.update(
        parent=handle.home.parent,
        applier=handle.wait_for("applier")["record"],
        singbox=handle.wait_for("singbox", timeout=DATA_PLANE_START_TIMEOUT)["record"],
    )
    raise _ExpectedLifecycleFailure(
        "intentional failure after starting the opted-in runtime"
    )


def test_failed_test_left_no_runtime_or_home():
    assert _FAILED_RUNTIME, "the intentional failing lifecycle test did not run"
    assert not record_is_live(_FAILED_RUNTIME["applier"])
    assert not record_is_live(_FAILED_RUNTIME["singbox"])
    assert not _FAILED_RUNTIME["parent"].exists()


def test_runtime_timeout_reports_sanitized_process_and_log_state(runtime_guard):
    home = runtime_guard.home
    home.mkdir(parents=True)
    (home / "applier.log").write_text(f"startup failed below {home}\n")

    with pytest.raises(AssertionError) as error:
        runtime_guard.wait_for("singbox", timeout=0)

    diagnostic = str(error.value)
    assert "applier.pid=absent" in diagnostic
    assert "singbox.pid=absent" in diagnostic
    assert "applier.info=absent" in diagnostic
    assert "alle.log=absent" in diagnostic
    assert "startup failed below <test-home>" in diagnostic
    assert str(home) not in diagnostic
    assert runtime_guard.session.token not in diagnostic


def test_recovery_stops_a_hard_killed_session_and_preserves_unmarked_paths(
    tmp_path,
):
    base = tmp_path / "sessions"
    unrelated = base / "not-owned-by-alle"
    unrelated.mkdir(parents=True)
    script = """
import json, os, subprocess, sys
from pathlib import Path
from alle import proc
from _runtime_hygiene import RuntimeSession

session = RuntimeSession(Path(sys.argv[1]))
home = session.new_home()
child_env = dict(os.environ)
child_env.update(
    ALLE_HOME=str(home),
    ALLE_TEST_HOME=str(home),
    ALLE_TEST_SESSION=session.token,
)
child = subprocess.Popen(
    [
        sys.executable,
        '-c',
        'import time; time.sleep(60)',
        'sing-box',
        'run',
        '-c',
        str(home / 'singbox.json'),
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
    env=child_env,
)
record = proc.record(child.pid)
(home / 'singbox.pid').parent.mkdir(parents=True, exist_ok=True)
(home / 'singbox.pid').write_text(json.dumps(record))
session.refresh_registry()
print(json.dumps({'root': str(session.root), 'record': record}), flush=True)
os._exit(17)
"""
    env = dict(os.environ)
    tests_path = str(ROOT / "tests")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (tests_path, env.get("PYTHONPATH", "")) if part
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(base)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 17, result.stderr
    abandoned = json.loads(result.stdout)
    assert record_is_live(abandoned["record"])

    recovered = recover_stale_sessions(base)
    assert Path(abandoned["root"]) in recovered
    assert not record_is_live(abandoned["record"])
    assert not Path(abandoned["root"]).exists()
    assert unrelated.is_dir()


def test_recovery_preserves_a_live_marked_session_and_outside_home(tmp_path):
    base = tmp_path / "sessions"
    live = RuntimeSession(base)
    outside = tmp_path / "outside" / "state"
    outside.mkdir(parents=True)
    try:
        assert recover_stale_sessions(base) == []
        assert live.root.is_dir()
        with pytest.raises(RuntimeError, match="refusing cleanup"):
            live.cleanup_home(outside)
        assert outside.is_dir()
    finally:
        live.close()


def test_cleanup_never_signals_a_pidfile_that_names_pytest(tmp_path):
    session = RuntimeSession(tmp_path / "sessions")
    home = session.new_home()
    home.mkdir(parents=True)
    (home / "singbox.pid").write_text(json.dumps(proc.record(os.getpid())))
    session.cleanup_home(home)
    assert proc.start_time_of(os.getpid()) is not None
    session.close()


def test_cleanup_never_signals_an_alle_shaped_process_without_session_token(
    tmp_path,
):
    session = RuntimeSession(tmp_path / "sessions")
    home = session.new_home()
    home.mkdir(parents=True)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"ALLE_HOME", "ALLE_TEST_HOME", "ALLE_TEST_SESSION"}
    }
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "alle applier"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    try:
        record = proc.record(child.pid)
        (home / "applier.pid").write_text(json.dumps(record))
        session.cleanup_home(home)
        assert record_is_live(record), "cleanup claimed an unrelated alle process"
    finally:
        child.terminate()
        child.wait(timeout=5)
        session.close()


def test_pid_records_used_by_recovery_are_exact():
    record = proc.record(os.getpid())
    assert record_is_live(record)
    assert not record_is_live({"pid": os.getpid(), "start": "not-this-process"})
    assert not record_is_live({"pid": os.getpid(), "start": None})
