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


def test_apply_is_noop_when_config_unchanged_and_running(monkeypatch):
    r = _runner()
    r.config_path.write_text(json.dumps({"a": 1}, indent=2))  # matches json.dumps form
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    rewrote: list[str] = []
    monkeypatch.setattr(r, "_write_protected", lambda text: rewrote.append(text))
    assert r.apply({"a": 1}) is False
    assert rewrote == []  # a no-op reconcile does not rewrite the file


def test_apply_bad_config_rolls_back_and_keeps_process_running(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"good": true}')  # what the running process is on
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "check", lambda: False)  # sing-box rejects the new config
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))

    assert r.apply({"bad": True}) is False
    assert restarted == []  # the working process was NOT stopped/restarted
    assert (
        r.config_path.read_text() == '{"good": true}'
    )  # rolled back to last-known-good


def test_apply_good_config_reloads_in_place(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"old": true}')
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "reload", lambda: True)
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert r.apply({"new": True}) is True
    assert restarted == []  # reload, not a full restart


def test_apply_falls_back_to_restart_when_reload_not_deliverable(monkeypatch):
    r = _runner()
    r.config_path.write_text('{"old": true}')
    monkeypatch.setattr(r, "running_pid", lambda: 123)
    monkeypatch.setattr(r, "check", lambda: True)
    monkeypatch.setattr(r, "reload", lambda: False)  # SIGHUP could not be sent
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert r.apply({"new": True}) is True
    assert restarted == [1]  # valid config, but a full restart was needed


def test_apply_bad_config_is_not_cold_started(monkeypatch):
    r = _runner()
    monkeypatch.setattr(r, "running_pid", lambda: None)  # nothing running
    monkeypatch.setattr(r, "check", lambda: False)  # sing-box rejects the config
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert r.apply({"bad": True}) is False
    assert restarted == []  # a config known to be bad is never started


def test_apply_cold_start_is_check_guarded(monkeypatch):
    r = _runner()
    monkeypatch.setattr(r, "running_pid", lambda: None)
    checked: list[int] = []
    monkeypatch.setattr(r, "check", lambda: checked.append(1) or True)
    restarted: list[int] = []
    monkeypatch.setattr(r, "restart", lambda: restarted.append(1))
    assert r.apply({"good": True}) is True
    assert checked == [1] and restarted == [1]


def test_start_raises_when_the_process_dies_immediately(monkeypatch):
    r = _runner()
    r.config_path.write_text("{}")
    r.log_path.write_text("FATAL: something broke\n")
    # a "sing-box" that exits instantly: python complains about the bogus args
    monkeypatch.setattr(singbox, "ensure_binary", lambda: Path(sys.executable))
    with pytest.raises(singbox.SingBoxError, match="exited immediately"):
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
