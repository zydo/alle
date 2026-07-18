"""`alle upgrade`: install-channel detection, the never-self-update delegation,
the on-demand PyPI version check, and the refusal channels (checkout,
container, unknown). Subprocess and network are faked throughout."""

from __future__ import annotations

import subprocess
import sys

import pytest

from alle import service, upgrade


# ---- channel detection ------------------------------------------------------


def _no_container(monkeypatch):
    from alle import runtime

    monkeypatch.setattr(runtime, "in_container", lambda: False)


def test_detects_container_first(monkeypatch):
    from alle import runtime

    monkeypatch.setattr(runtime, "in_container", lambda: True)
    assert upgrade.detect_channel() == "container"


def test_detects_editable_checkout(monkeypatch):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: True)
    assert upgrade.detect_channel() == "checkout"


def test_detects_uv_tool_by_prefix(monkeypatch):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    monkeypatch.setattr(
        upgrade.sys, "prefix", "/Users/x/.local/share/uv/tools/alle-proxy"
    )
    assert upgrade.detect_channel() == "uv-tool"


def test_detects_pipx_by_prefix(monkeypatch):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    monkeypatch.setattr(upgrade.sys, "prefix", "/Users/x/.local/pipx/venvs/alle-proxy")
    assert upgrade.detect_channel() == "pipx"


def test_detects_homebrew_cellar_keg(monkeypatch):
    # macOS arm Cellar, Intel Cellar, and Linuxbrew all contain /Cellar/alle/.
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    for prefix in (
        "/opt/homebrew/Cellar/alle/0.1.8/libexec",
        "/usr/local/Cellar/alle/0.1.8/libexec",
        "/home/linuxbrew/.linuxbrew/Cellar/alle/0.1.8/libexec",
        "/opt/homebrew/opt/alle/libexec",
    ):
        monkeypatch.setattr(upgrade.sys, "prefix", prefix)
        assert upgrade.detect_channel() == "homebrew", prefix


def test_homebrew_does_not_shadow_uv_or_pipx(monkeypatch):
    # A uv/pipx install must not be misread as brew just because brew exists.
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    monkeypatch.setattr(
        upgrade.sys, "prefix", "/Users/x/.local/share/uv/tools/alle-proxy"
    )
    assert upgrade.detect_channel() == "uv-tool"


def test_detects_plain_pip_when_distribution_exists(monkeypatch):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    monkeypatch.setattr(upgrade.sys, "prefix", "/opt/venvs/tools")
    monkeypatch.setattr(upgrade, "_dist_exists", lambda: True)
    assert upgrade.detect_channel() == "pip"


def test_unknown_when_no_distribution(monkeypatch):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    monkeypatch.setattr(upgrade.sys, "prefix", "/opt/venvs/tools")
    monkeypatch.setattr(upgrade, "_dist_exists", lambda: False)
    assert upgrade.detect_channel() == "unknown"


# ---- refusals (never self-update; wrong channels refuse with the right hint)


def test_run_refuses_in_container(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "container")
    with pytest.raises(upgrade.UpgradeError, match="image"):
        upgrade.run()


def test_run_refuses_a_git_checkout(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "checkout")
    with pytest.raises(upgrade.UpgradeError, match="git"):
        upgrade.run()


def test_run_refuses_unknown_channel(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "unknown")
    with pytest.raises(upgrade.UpgradeError, match="install channel"):
        upgrade.run()


# ---- delegation -------------------------------------------------------------


def test_run_delegates_to_uv_and_reports_versions(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "uv-tool")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    versions = iter(["0.1.8", "0.1.9"])
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: next(versions))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    res = upgrade.run()
    # the owning tool runs by resolved absolute path, never bare-name PATH lookup
    assert calls == [["/usr/bin/uv", "tool", "upgrade", "alle-proxy"]]
    assert res["channel"] == "uv-tool"
    assert res["before"] == "0.1.8" and res["after"] == "0.1.9"
    assert res["changed"] is True


def test_run_surfaces_a_failed_command(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pipx")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no network")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    with pytest.raises(upgrade.UpgradeError, match="no network"):
        upgrade.run()


def test_run_requires_the_owning_tool_on_path(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "uv-tool")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: None)
    with pytest.raises(upgrade.UpgradeError, match="PATH"):
        upgrade.run()


def test_run_delegates_to_brew_upgrade(monkeypatch):
    # The keg is named for the formula (`alle`), not the PyPI package.
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "homebrew")
    monkeypatch.setattr(
        upgrade.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}"
    )
    versions = iter(["0.1.8", "0.1.9"])
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: next(versions))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    res = upgrade.run()
    assert calls == [["/opt/homebrew/bin/brew", "upgrade", "alle"]]
    assert res["channel"] == "homebrew"
    assert res["changed"] is True


def test_run_requires_brew_on_path(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "homebrew")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: None)
    with pytest.raises(upgrade.UpgradeError, match="PATH"):
        upgrade.run()


def test_pip_channel_upgrades_via_the_running_interpreter(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pip")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.run()
    assert calls == [
        [sys.executable, "-m", "pip", "install", "--upgrade", "alle-proxy"]
    ]


# ---- the on-demand version check -------------------------------------------


def test_check_latest_reports_update_available(monkeypatch):
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    monkeypatch.setattr(upgrade, "_fetch_pypi_version", lambda timeout: "0.1.9")
    res = upgrade.check_latest()
    assert res == {"current": "0.1.8", "latest": "0.1.9", "update_available": True}


def test_check_latest_reports_up_to_date(monkeypatch):
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.9")
    monkeypatch.setattr(upgrade, "_fetch_pypi_version", lambda timeout: "0.1.9")
    assert upgrade.check_latest()["update_available"] is False


def test_check_latest_never_downgrades(monkeypatch):
    # a dev version ahead of PyPI is not an "update available"
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upgrade, "_fetch_pypi_version", lambda timeout: "0.1.9")
    assert upgrade.check_latest()["update_available"] is False


def test_version_ordering():
    assert upgrade._version_newer("0.1.10", "0.1.9") is True
    assert upgrade._version_newer("0.1.9", "0.1.10") is False
    assert upgrade._version_newer("1.0.0", "0.9.9") is True
    assert upgrade._version_newer("0.1.9", "0.1.9") is False


# ---- service wrapper: restart lands on the new code -------------------------


def test_service_upgrade_restarts_when_version_changed(monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "run",
        lambda: {
            "channel": "uv-tool",
            "before": "0.1.8",
            "after": "0.1.9",
            "changed": True,
        },
    )
    monkeypatch.setattr(service.daemon, "is_running", lambda: True)
    restarts = []
    monkeypatch.setattr(service, "restart", lambda: restarts.append(1) or {"ok": True})
    res = service.upgrade_run()
    assert res["changed"] is True
    assert restarts == [1]


def test_service_upgrade_skips_restart_when_unchanged(monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "run",
        lambda: {
            "channel": "uv-tool",
            "before": "0.1.9",
            "after": "0.1.9",
            "changed": False,
        },
    )
    restarts = []
    monkeypatch.setattr(service, "restart", lambda: restarts.append(1))
    res = service.upgrade_run()
    assert res["changed"] is False
    assert restarts == []


def test_service_upgrade_maps_refusals_to_service_errors(monkeypatch):
    def refuse():
        raise upgrade.UpgradeError("this alle runs in a container image")

    monkeypatch.setattr(upgrade, "run", refuse)
    with pytest.raises(service.ServiceError, match="container image"):
        service.upgrade_run()
