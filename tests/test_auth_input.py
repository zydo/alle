"""The masked secret reader: visible '*' feedback, paste survives intact
(including bracketed-paste escapes), and a clean fallback when stdin isn't a TTY."""

from __future__ import annotations

import pytest

import alle.cli as cli


def _drive(keystrokes: str):
    """Run the char assembler over a fixed input, returning (secret, echoes)."""
    it = iter(keystrokes)
    echoes: list[str] = []
    secret = cli._read_secret_chars(lambda: next(it, ""), echoes.append)
    return secret, echoes


def test_plain_typed_secret_is_masked():
    secret, echoes = _drive("nGx4Token\r")
    assert secret == "nGx4Token"
    assert echoes == ["*"] * len("nGx4Token")  # one star per char, never the chars


def test_bracketed_paste_wrappers_are_stripped():
    # what a terminal delivers when you paste with bracketed-paste enabled
    secret, echoes = _drive("\x1b[200~nGx4SECRETa91k\x1b[201~\r")
    assert secret == "nGx4SECRETa91k"
    assert echoes == ["*"] * len("nGx4SECRETa91k")


def test_backspace_erases():
    secret, _ = _drive("abc\x7fd\r")
    assert secret == "abd"


def test_arrow_key_escape_is_ignored():
    secret, _ = _drive("ab\x1b[Dc\r")  # ESC[D = left arrow
    assert secret == "abc"


def test_ctrl_c_aborts():
    with pytest.raises(KeyboardInterrupt):
        _drive("ab\x03")


def test_eof_ends_input():
    secret, _ = _drive("abc")  # no trailing newline
    assert secret == "abc"


def test_falls_back_to_getpass_without_tty(monkeypatch):
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "piped-secret")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert cli._read_secret("token: ") == "piped-secret"
