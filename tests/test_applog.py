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


def test_reverse_tail_handles_partial_long_and_invalid_lines():
    path = applog._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"one\n" + b"x" * 40 + b"\nbad-\xff\npartial")

    assert applog.reverse_tail(path, 3, block_size=8) == [
        "x" * 40,
        "bad-\ufffd",
        "partial",
    ]
    assert applog.reverse_tail(path, 0, block_size=8) == []


def test_reverse_tail_reads_only_trailing_blocks(monkeypatch):
    path = applog._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((b"old padding\n" * 1000) + b"one\ntwo\nthree\n")
    real_open = open
    bytes_read = 0

    class CountingFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            nonlocal bytes_read
            data = self.stream.read(size)
            bytes_read += len(data)
            return data

    monkeypatch.setattr(
        builtins,
        "open",
        lambda target, mode="r", *args, **kwargs: CountingFile(
            real_open(target, mode, *args, **kwargs)
        ),
    )

    assert applog.reverse_tail(path, 2, block_size=32) == ["two", "three"]
    assert bytes_read == 32


def test_log_rotates_past_max_size(monkeypatch):
    monkeypatch.setattr(applog, "MAX_LOG_BYTES", 128)
    for i in range(20):
        applog.log(f"line {i:02d} with some padding to grow the file")

    path = applog._log_path()
    backup = path.with_name(path.name + ".1")
    assert backup.exists()  # the oversized file was moved aside, not truncated
    assert path.stat().st_size < 256  # current file restarted small
    assert "line 19" in applog.tail()  # latest lines are in the current file


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


def test_log_lines_are_sanitized(tmp_path, monkeypatch):
    """A hostile provider/user string must not smuggle ANSI into the log —
    `alle logs -f` replays the file raw into a terminal."""
    from alle import applog

    monkeypatch.setenv("ALLE_HOME", str(tmp_path))
    applog.log("channel \x1b[2J\x9bHed label\x07 added")
    text = (tmp_path / "alle.log").read_text()
    assert "\x1b" not in text and "\x9b" not in text and "\x07" not in text
    assert "label" in text
