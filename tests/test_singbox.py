"""sing-box config apply: a running process is guarded against a config sing-box
itself rejects (roll back + leave running), reloaded in place when valid, and
restarted only when a reload can't be signalled. The process methods are stubbed
so no real binary or subprocess is needed."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from alle import singbox


def _runner() -> singbox.Runner:
    r = singbox.Runner()
    r.config_path.parent.mkdir(parents=True, exist_ok=True)
    return r


def _outcome(result) -> singbox.ApplyOutcome:
    assert isinstance(result, singbox.ApplyResult)
    return result.outcome


def test_apply_is_noop_when_config_unchanged_and_running(monkeypatch):
    r = _runner()
    r.config_path.write_text(json.dumps({"a": 1}, indent=2))  # matches json.dumps form
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    rewrote: list[str] = []
    monkeypatch.setattr(r, "_write_protected", lambda text: rewrote.append(text))
    assert _outcome(r.apply({"a": 1})) is singbox.ApplyOutcome.UNCHANGED
    assert rewrote == []  # a no-op reconcile does not rewrite the file


def test_apply_bad_config_rolls_back_and_keeps_process_running(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"good": true}')  # what the running process is on
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "check", lambda: False)  # sing-box rejects the new config
    r._check_error = "unknown field frobnicate"
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))

    result = r.apply({"bad": True})
    assert result.outcome is singbox.ApplyOutcome.REJECTED
    assert result.detail == "unknown field frobnicate"  # rejection is reported
    assert restarted == []  # the working process was NOT stopped/restarted
    assert (
        r.config_path.read_text() == '{"good": true}'
    )  # rolled back to last-known-good


def test_apply_rejected_config_restarts_last_known_good_when_stopped(monkeypatch):
    # the process is dead and the desired config is refused: rather than leave
    # every tunnel down, the previous (accepted) generation is brought back up
    r = _runner()
    r.config_path.write_text('{"good": true}')
    monkeypatch.setattr(r, "running_pid", lambda: None)
    monkeypatch.setattr(r, "check", lambda: False)
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert _outcome(r.apply({"bad": True})) is singbox.ApplyOutcome.REJECTED
    assert restarted == [1]
    assert r.config_path.read_text() == '{"good": true}'


def test_apply_good_config_reloads_in_place(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"old": true}')
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "reload", lambda: True)
    monkeypatch.setattr(r, "_verify_healthy", lambda: True)
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert _outcome(r.apply({"new": True})) is singbox.ApplyOutcome.APPLIED
    assert restarted == []  # reload, not a full restart


def test_apply_falls_back_to_restart_when_reload_not_deliverable(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"old": true}')
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "reload", lambda: False)  # SIGHUP could not be sent
    monkeypatch.setattr(r, "_verify_healthy", lambda: True)
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert _outcome(r.apply({"new": True})) is singbox.ApplyOutcome.APPLIED
    assert restarted == [1]  # valid config, but a full restart was needed


def test_apply_restarts_when_reload_leaves_an_unhealthy_process(monkeypatch):
    # SIGHUP delivered but the replacement did not come up healthy (1.13.x can
    # close the old instance and exit): a delivered signal is not "applied"
    r = _runner()
    r.config_path.write_text('{"old": true}')
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "reload", lambda: True)
    healthy = iter([False, True])  # dead after reload, healthy after restart
    monkeypatch.setattr(r, "_verify_healthy", lambda: next(healthy))
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert _outcome(r.apply({"new": True})) is singbox.ApplyOutcome.APPLIED
    assert restarted == [1]


def test_apply_reports_runtime_failure_with_the_process_error(monkeypatch):
    r = _runner()
    monkeypatch.setattr(r, "running_pid", lambda: None)
    monkeypatch.setattr(r, "check", lambda: True)

    def boom():
        raise singbox.SingBoxRuntimeError("exited immediately: bind: in use")

    monkeypatch.setattr(r, "restart", boom)
    result = r.apply({"good": True})
    assert result.outcome is singbox.ApplyOutcome.RUNTIME_FAILED
    assert "bind: in use" in result.detail  # engine's port recovery reads this


def test_apply_runtime_failure_when_control_api_never_answers(monkeypatch):
    r = _runner()
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "reload", lambda: False)
    monkeypatch.setattr(r, "restart", lambda: None)
    monkeypatch.setattr(r, "_verify_healthy", lambda: False)
    result = r.apply({"new": True})
    assert result.outcome is singbox.ApplyOutcome.RUNTIME_FAILED
    assert "control API" in result.detail


def test_apply_bad_config_is_not_cold_started(monkeypatch):
    r = _runner()
    monkeypatch.setattr(r, "running_pid", lambda: None)  # nothing running
    monkeypatch.setattr(r, "check", lambda: False)  # sing-box rejects the config
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert _outcome(r.apply({"bad": True})) is singbox.ApplyOutcome.REJECTED
    assert restarted == []  # no last-known-good exists — nothing to start


def test_apply_cold_start_is_check_guarded(monkeypatch):
    r = _runner()
    monkeypatch.setattr(r, "running_pid", lambda: None)
    checked: list[int] = []
    monkeypatch.setattr(r, "check", lambda: checked.append(1) or True)
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    monkeypatch.setattr(r, "_verify_healthy", lambda: True)
    assert _outcome(r.apply({"good": True})) is singbox.ApplyOutcome.APPLIED
    assert checked == [1] and restarted == [1]


def test_verify_healthy_requires_a_live_process(monkeypatch):
    r = _runner()
    monkeypatch.setattr(r, "running_pid", lambda: None)
    assert r._verify_healthy(deadline=0.1) is False  # dead: no API poll needed


def test_verify_healthy_polls_the_control_api(monkeypatch):
    r = _runner()
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    answers = iter([False, True])  # API up on the second poll
    monkeypatch.setattr(r, "_control_alive", lambda: next(answers))
    assert r._verify_healthy(deadline=1.0) is True
    monkeypatch.setattr(r, "_control_alive", lambda: False)
    assert r._verify_healthy(deadline=0.1) is False


def test_start_raises_when_the_process_dies_immediately(monkeypatch):
    r = _runner()
    r.config_path.write_text("{}")
    r.log_path.write_text("FATAL: something broke\n")
    # a "sing-box" that exits instantly: python complains about the bogus args
    monkeypatch.setattr(singbox, "ensure_binary", lambda: Path(sys.executable))
    with pytest.raises(singbox.SingBoxRuntimeError, match="exited immediately"):
        r.start()
    assert r.running_pid() is None  # no dead PID left behind a fresh pidfile
    assert not (r.config_path.parent / "singbox.pid").exists()


def test_config_is_created_private_then_locked_readonly():
    r = _runner()
    r._write_protected('{"a": 1}')  # fresh file — no pre-existing mode to inherit
    assert stat.S_IMODE(r.config_path.stat().st_mode) == 0o400  # carries WG keys
    r._write_protected('{"a": 2}')  # our own rewrite of the read-only file works
    assert r.config_path.read_text() == '{"a": 2}'
    assert stat.S_IMODE(r.config_path.stat().st_mode) == 0o400


def test_clash_api_endpoint_is_generated_once_and_private():
    from alle import paths

    a = singbox.clash_api()
    assert a["address"].startswith("127.0.0.1:")
    assert len(a["secret"]) == 32  # 16 random bytes, hex
    assert singbox.clash_api() == a  # stable across calls — config stays put
    mode = stat.S_IMODE((paths.state_dir() / "clash_api.json").stat().st_mode)
    assert mode == 0o600  # the secret is what keeps other local users out


def test_clash_api_regenerates_after_corruption():
    from alle import paths

    (paths.state_dir() / "clash_api.json").write_text("not json")
    a = singbox.clash_api()  # regenerable — rebuilt rather than crashing
    assert a["address"].startswith("127.0.0.1:") and a["secret"]
    assert singbox.clash_api() == a


def test_connections_authenticates_with_the_clash_secret(monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"connections": [{"id": "c1"}]}'

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.get_header("Authorization")
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(singbox.urllib.request, "urlopen", fake_urlopen)
    assert singbox.Runner().connections() == [{"id": "c1"}]
    api = singbox.clash_api()
    assert captured["auth"] == f"Bearer {api['secret']}"
    assert api["address"] in captured["url"]


def _resp(body: bytes):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return body

    return lambda _req, timeout=None: _Resp()


def test_connections_returns_none_when_the_sample_fails(monkeypatch):
    """'Couldn't sample' must never read as 'no connections' — an empty list
    clears the accumulator's watermarks and the next good sample would
    re-bank whole lifetime counters."""
    runner = singbox.Runner()

    def unreachable(_req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(singbox.urllib.request, "urlopen", unreachable)
    assert runner.connections() is None

    monkeypatch.setattr(singbox.urllib.request, "urlopen", _resp(b"{not json"))
    assert runner.connections() is None

    monkeypatch.setattr(singbox.urllib.request, "urlopen", _resp(b'["array root"]'))
    assert runner.connections() is None

    monkeypatch.setattr(
        singbox.urllib.request, "urlopen", _resp(b'{"connections": "nope"}')
    )
    assert runner.connections() is None

    # a healthy API with zero connections IS an empty list
    monkeypatch.setattr(singbox.urllib.request, "urlopen", _resp(b"{}"))
    assert runner.connections() == []

    # non-dict entries are dropped, dict entries survive
    monkeypatch.setattr(
        singbox.urllib.request,
        "urlopen",
        _resp(b'{"connections": [{"id": "c1"}, "garbage", 7]}'),
    )
    assert runner.connections() == [{"id": "c1"}]


def test_generation_identifies_the_verified_instance(monkeypatch):
    import json as _json
    import os

    from alle import paths, proc

    runner = singbox.Runner()
    pid_path = paths.state_dir() / "singbox.pid"
    assert runner.generation() is None  # no pidfile — nothing provably running

    # a verified record (this test process stands in for sing-box)
    rec = proc.record(os.getpid())
    pid_path.write_text(_json.dumps(rec))
    monkeypatch.setattr(singbox.proc, "verify", lambda r, markers: True)
    gen = runner.generation()
    assert gen == f"{os.getpid()}/{rec.get('start') or ''}"

    # an unverifiable record (recycled PID / dead process) has no generation
    monkeypatch.setattr(singbox.proc, "verify", lambda r, markers: False)
    assert runner.generation() is None
