"""CLI surface for the Web UI: `alle ui` and the URL shown in `alle status`."""

from __future__ import annotations

from types import SimpleNamespace

from alle import cli, output, service


def test_ui_command_prints_login_url_when_not_opening(capsys, monkeypatch):
    monkeypatch.setattr(service, "ensure_web_ui", lambda: True)
    monkeypatch.setattr(
        service, "web_ui_login_url", lambda: "http://127.0.0.1:8123/?token=abc"
    )
    monkeypatch.setattr(service, "web_ui_url", lambda: "http://127.0.0.1:8123")
    cli.cmd_ui(SimpleNamespace(no_open=True))
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8123/?token=abc" in out  # one-time link printed
    assert "ssh -L" in out  # headless/remote guidance


def test_ui_command_errors_when_server_not_reachable(monkeypatch):
    import pytest

    monkeypatch.setattr(service, "ensure_web_ui", lambda: False)
    with pytest.raises(SystemExit, match="isn't responding"):
        cli.cmd_ui(SimpleNamespace(no_open=True))


def test_status_output_shows_web_ui_url():
    snap = {
        "running": True,
        "channels": [],
        "router": {
            "port": 5,
            "rule_count": 0,
            "killswitch": False,
            "unmatched": "direct",
        },
        "daemon": {
            "running": True,
            "version": "0.1.0",
            "cli_version": "0.1.0",
            "skew": False,
        },
        "web_ui": "http://127.0.0.1:8123",
        "rest_api": "http://127.0.0.1:8123/api/v1",
    }
    text = output.status(snap)
    assert "Web UI    http://127.0.0.1:8123" in text
    assert "REST API  http://127.0.0.1:8123/api/v1" in text
    assert "shares the Web UI listener; Bearer auth required" in text
    assert "alle ui" in text
