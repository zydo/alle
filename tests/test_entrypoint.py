"""Executable module entrypoint."""

from __future__ import annotations

import runpy

from alle import cli


def test_python_m_alle_calls_cli_main(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "main", lambda: called.append(True))

    runpy.run_module("alle.__main__", run_name="__main__")

    assert called == [True]
