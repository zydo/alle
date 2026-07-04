"""Unit tests for alle's operation log helpers."""

from __future__ import annotations

import builtins

import pytest

from alle import applog


def test_log_writes_timestamped_line(monkeypatch):
    monkeypatch.setattr(applog.time, "strftime", lambda fmt: "2026-07-04 12:34:56")

    applog.log("started")

    assert applog.tail() == "2026-07-04 12:34:56  started"


def test_log_swallows_os_errors(monkeypatch):
    def fail_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(builtins, "open", fail_open)

    applog.log("lost")


def test_tail_missing_empty_and_limited_lines():
    assert applog.tail() == "(no logs yet)"

    path = applog._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    assert applog.tail() == "(no logs yet)"

    path.write_text("one\ntwo\nthree\n")
    assert applog.tail(2) == "two\nthree"


def test_follow_prints_existing_tail_and_closes(monkeypatch, capsys):
    path = applog._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("one\ntwo\n")

    def stop(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(applog.time, "sleep", stop)

    with pytest.raises(KeyboardInterrupt):
        applog.follow(1)

    assert capsys.readouterr().out.splitlines() == ["two"]
