"""sing-box config apply: a running process is guarded against a config sing-box
itself rejects (roll back + leave running), reloaded in place when valid, and
restarted only when a reload can't be signalled. The process methods are stubbed
so no real binary or subprocess is needed."""

from __future__ import annotations

import json

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
