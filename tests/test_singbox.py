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
    monkeypatch.setattr(r, "_verify_healthy", lambda: True)
    rewrote: list[str] = []
    monkeypatch.setattr(r, "_write_protected", lambda text: rewrote.append(text))
    assert _outcome(r.apply({"a": 1})) is singbox.ApplyOutcome.UNCHANGED
    assert rewrote == []  # a no-op reconcile does not rewrite the file


def test_identical_config_with_dead_control_plane_recovers(monkeypatch):
    r = _runner()
    r.config_path.write_text(json.dumps({"inbounds": []}, indent=2))
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "_verify_healthy", lambda: False)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "reload", lambda: False)
    restarted = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    result = r.apply({"inbounds": []})
    assert result.outcome is singbox.ApplyOutcome.RUNTIME_FAILED
    assert restarted == [1]


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


# ---- privileged-helper delegation (tun mode) --------------------------------


class _FakeHelper:
    """A stateful stand-in for alle.helper: tracks whether it 'owns' sing-box
    and records the request stream, so apply's on/off transitions can be
    asserted without a real helper or sing-box."""

    def __init__(self, *, owning=False):
        self._owning = owning
        self._generation = 1
        self.calls: list[str] = []

    def reachable(self) -> bool:
        return True

    def request(self, cmd, **fields):
        self.calls.append(cmd)
        if cmd == "status":
            return (
                {
                    "ok": True,
                    "running": True,
                    "pid": 777,
                    "generation": f"777/s{self._generation}",
                }
                if self._owning
                else {"ok": True, "running": False}
            )
        if cmd == "start":
            self._owning = True
            return {"ok": True, "pid": 777, "generation": "777/s"}
        if cmd == "stop":
            self._owning = False
            return {"ok": True}
        if cmd == "reload":
            self._generation += 1
            return {
                "ok": True,
                "reloaded": True,
                "generation": f"777/s{self._generation}",
            }
        return {"ok": False, "error": "nope"}


def _wire_helper(monkeypatch, fake):
    import alle.helper as helper_mod

    monkeypatch.setattr(helper_mod, "reachable", fake.reachable)
    monkeypatch.setattr(helper_mod, "request", fake.request)


def _tun_cfg():
    return {"inbounds": [{"type": "tun", "tag": "in-tun"}], "route": {}}


def test_apply_tun_on_delegates_start_to_helper(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"old": true}')
    fake = _FakeHelper(owning=False)
    _wire_helper(monkeypatch, fake)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "_verify_healthy", lambda: True)
    stopped: list[int] = []
    monkeypatch.setattr(r, "_stop_local", lambda: stopped.append(1))
    # no local pidfile → _stop_local not called
    monkeypatch.setattr(singbox.proc, "read_pidfile", lambda *a, **k: None)
    assert _outcome(r.apply(_tun_cfg())) is singbox.ApplyOutcome.APPLIED
    assert "start" in fake.calls and "stop" not in fake.calls
    assert stopped == []  # nothing local to clear


def test_apply_tun_on_stops_a_local_sing_box_before_helper_start(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"old": true}')
    fake = _FakeHelper(owning=False)
    _wire_helper(monkeypatch, fake)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "_verify_healthy", lambda: True)
    stopped: list[int] = []
    monkeypatch.setattr(r, "_stop_local", lambda: stopped.append(1))
    monkeypatch.setattr(singbox.proc, "read_pidfile", lambda *a, **k: 123)  # local up
    assert _outcome(r.apply(_tun_cfg())) is singbox.ApplyOutcome.APPLIED
    assert stopped == [1]  # the user sing-box was cleared before helper start
    assert "start" in fake.calls


def test_apply_tun_reload_via_helper_when_already_owned(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"old": true}')
    fake = _FakeHelper(owning=True)  # helper already running sing-box
    _wire_helper(monkeypatch, fake)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "_verify_healthy", lambda: True)
    monkeypatch.setattr(r, "_control_alive", lambda: True)
    monkeypatch.setattr(singbox.proc, "read_pidfile", lambda *a, **k: None)
    assert _outcome(r.apply(_tun_cfg())) is singbox.ApplyOutcome.APPLIED
    assert "reload" in fake.calls and "start" not in fake.calls  # in-place


def test_apply_tun_off_stops_helper_then_restarts_locally(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"old": true}')
    fake = _FakeHelper(owning=True)  # tun was on, helper owns root sing-box
    _wire_helper(monkeypatch, fake)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "_verify_healthy", lambda: True)
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    # no local sing-box running after the helper stop
    monkeypatch.setattr(singbox.proc, "read_pidfile", lambda *a, **k: None)
    assert (
        _outcome(r.apply({"inbounds": [], "route": {}})) is singbox.ApplyOutcome.APPLIED
    )
    assert "stop" in fake.calls  # root sing-box stopped via the helper
    assert restarted == [1]  # then a local user sing-box started


def test_running_pid_and_stop_delegate_when_helper_owns(monkeypatch):
    r = _runner()
    fake = _FakeHelper(owning=True)
    _wire_helper(monkeypatch, fake)
    assert r.running_pid() == 777  # helper's pid, not the pidfile
    r.stop()
    assert fake.calls[-1] == "stop"
    assert fake._owning is False


def test_running_pid_falls_to_pidfile_when_helper_idle(monkeypatch):
    r = _runner()
    fake = _FakeHelper(owning=False)  # helper installed but not running sing-box
    _wire_helper(monkeypatch, fake)
    monkeypatch.setattr(singbox.proc, "read_pidfile", lambda *a, **k: 42)
    assert r.running_pid() == 42  # local pidfile wins when helper owns nothing


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


def test_failed_atomic_config_publish_preserves_previous_bytes(monkeypatch):
    r = _runner()
    r._write_protected('{"old": true}')

    def fail_replace(_src, _dst):
        raise OSError("interrupted rename")

    monkeypatch.setattr(singbox.fsio.os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted rename"):
        r._write_protected('{"new": true}')
    assert r.config_path.read_text() == '{"old": true}'
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


def test_clash_api_rejects_a_shape_wrong_file():
    from alle import paths

    # parses as JSON but the fields aren't usable strings — strict validation,
    # not a loose truthiness check, decides whether to regenerate
    (paths.state_dir() / "clash_api.json").write_text(
        '{"address": 123, "secret": null}'
    )
    a = singbox.clash_api()
    assert isinstance(a["address"], str) and isinstance(a["secret"], str)


def test_clash_api_concurrent_callers_agree_on_one_endpoint():
    # Two callers racing to first-generate are serialized by the lock: both
    # return the SAME endpoint, not each their own port with one clobbering
    # the file.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: singbox.clash_api(), range(16)))
    assert len({r["address"] for r in results}) == 1
    assert len({r["secret"] for r in results}) == 1


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
