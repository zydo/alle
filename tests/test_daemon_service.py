"""Service + CLI + output for the login-service feature: daemon install/uninstall/
status and the CLI↔daemon version-skew surface, with daemonctl stubbed."""

from __future__ import annotations

import pytest

from alle import cli, daemon, daemonctl, output, service


def run_cli(args, capsys):
    cli.main(args)
    return capsys.readouterr().out.rstrip("\n")


# ---- service ops ---------------------------------------------------------------


def test_daemon_install_stops_then_installs_then_ensures(monkeypatch):
    events = []
    monkeypatch.setattr(daemon, "stop", lambda: events.append("stop") or True)
    monkeypatch.setattr(daemon, "ensure_running", lambda: events.append("ensure"))
    monkeypatch.setattr(
        daemonctl,
        "install",
        lambda linger=False: (
            events.append(f"install(linger={linger})")
            or {
                "manager": "launchd",
                "unit_path": "/x",
                "reinstalled": False,
                "linger": linger,
            }
        ),
    )
    result = service.daemon_install()
    # old daemon stopped first, then service installed, then handed off
    assert events == ["stop", "install(linger=False)", "ensure"]
    assert result["manager"] == "launchd"


def test_daemon_install_surfaces_ctl_errors(monkeypatch):
    monkeypatch.setattr(daemon, "stop", lambda: False)

    def boom(linger=False):
        raise daemonctl.DaemonCtlError("no backend")

    monkeypatch.setattr(daemonctl, "install", boom)
    with pytest.raises(service.ServiceError, match="no backend"):
        service.daemon_install()


def test_daemon_uninstall_delegates(monkeypatch):
    events = []
    monkeypatch.setattr(
        daemonctl,
        "uninstall",
        lambda: events.append("unit") or {"manager": "systemd", "removed": True},
    )
    monkeypatch.setattr(
        service.singbox,
        "Runner",
        lambda: type("Runner", (), {"stop": lambda self: events.append("sing-box")})(),
    )
    assert service.daemon_uninstall()["removed"] is True
    assert events == ["unit", "sing-box"]


def test_daemon_uninstall_does_not_stop_manual_runtime_when_no_unit_was_removed(
    monkeypatch,
):
    monkeypatch.setattr(
        daemonctl, "uninstall", lambda: {"manager": "systemd", "removed": False}
    )
    monkeypatch.setattr(
        service.singbox,
        "Runner",
        lambda: pytest.fail("no removed supervisor means no owned data plane"),
    )

    assert service.daemon_uninstall()["removed"] is False


# ---- version skew in status ----------------------------------------------------


def test_status_snapshot_reports_daemon_skew(monkeypatch):
    monkeypatch.setattr(service, "__version__", "0.2.0")
    monkeypatch.setattr(daemon, "daemon_info", lambda: {"pid": 1, "version": "0.1.0"})
    monkeypatch.setattr(daemonctl, "is_installed", lambda: False)
    d = service.status_snapshot()["daemon"]
    assert d["running"] is True and d["skew"] is True
    assert d["version"] == "0.1.0" and d["cli_version"] == "0.2.0"


def test_status_output_warns_on_skew():
    snap = {
        "running": True,
        "channels": [],
        "router": None,
        "daemon": {
            "running": True,
            "version": "0.1.0",
            "cli_version": "0.2.0",
            "skew": True,
        },
    }
    text = output.status(snap)
    assert "daemon running 0.1.0, CLI is 0.2.0" in text
    assert "alle restart" in text


def test_status_output_no_warning_without_skew():
    snap = {
        "running": True,
        "channels": [],
        "router": None,
        "daemon": {
            "running": True,
            "version": "0.2.0",
            "cli_version": "0.2.0",
            "skew": False,
        },
    }
    assert "⚠" not in output.status(snap)


# ---- CLI -----------------------------------------------------------------------


def test_cli_daemon_status_human_and_json(capsys, monkeypatch):
    monkeypatch.setattr(
        daemonctl,
        "status",
        lambda: {
            "supported": True,
            "manager": "launchd",
            "installed": True,
            "active": True,
            "unit_path": "/Users/x/Library/LaunchAgents/com.github.zydo.alle.plist",
        },
    )
    monkeypatch.setattr(daemon, "daemon_info", lambda: {"pid": 9, "version": "0.1.0"})
    monkeypatch.setattr(daemonctl, "is_installed", lambda: True)

    out = run_cli(["daemon", "status"], capsys)
    assert "Login service: active (launchd)" in out
    assert "Daemon: running, version 0.1.0" in out

    import json

    data = json.loads(run_cli(["daemon", "status", "--json"], capsys))
    assert data["service"]["installed"] is True
    assert data["daemon"]["running"] is True


def test_cli_daemon_install_uninstall_messages(capsys, monkeypatch):
    monkeypatch.setattr(
        service,
        "daemon_install",
        lambda linger=False: {
            "manager": "systemd",
            "unit_path": "/home/x/.config/systemd/user/alle.service",
            "reinstalled": False,
            "linger": linger,
        },
    )
    out = run_cli(["daemon", "install"], capsys)
    assert "Installed the alle login service (systemd)." in out
    assert "auto-starts at login" in out

    monkeypatch.setattr(
        service, "daemon_uninstall", lambda: {"manager": "systemd", "removed": True}
    )
    out = run_cli(["daemon", "uninstall"], capsys)
    assert "Removed the alle login service" in out
    assert "untouched" in out
