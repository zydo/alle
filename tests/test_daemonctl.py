"""Login-service install/uninstall/status — unit-file generation and the
service-manager command sequence, with the real launchctl/systemctl calls and
the sing-box pre-fetch stubbed."""

from __future__ import annotations

import plistlib

import pytest

from alle import daemonctl


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Point Path.home() at a throwaway dir so unit files land under tmp."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def recorded_run(monkeypatch):
    """Record every service-manager command and return success by default."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake(cmd):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(daemonctl, "_run", fake)
    return calls


@pytest.fixture(autouse=True)
def stub_binary(monkeypatch):
    """Don't actually download sing-box during install()."""
    import alle.singbox

    monkeypatch.setattr(alle.singbox, "ensure_binary", lambda: None)


# ---- launchd (macOS) -----------------------------------------------------------


def test_launchd_plist_execs_the_stable_shim(fake_home):
    plist = plistlib.loads(daemonctl.LaunchdManager()._plist_bytes())
    assert plist["Label"] == "com.github.zydo.alle"
    assert plist["ProgramArguments"][-1] == "applier"  # `<alle> applier`
    assert plist["EnvironmentVariables"]["ALLE_SERVICE"] == "1"
    assert plist["KeepAlive"] is True  # supervisor respawns on crash / upgrade-exit
    assert plist["RunAtLoad"] is True


def test_launchd_install_writes_plist_and_loads(fake_home, recorded_run, monkeypatch):
    monkeypatch.setattr(daemonctl.platform, "system", lambda: "Darwin")
    m = daemonctl.LaunchdManager()
    assert not m.is_installed()
    daemonctl.install()
    assert m.is_installed()
    assert m.unit_path().suffix == ".plist"
    # last command loads the freshly written plist
    assert recorded_run[-1][:3] == ["launchctl", "load", "-w"]
    assert str(m.unit_path()) in recorded_run[-1]


def test_launchd_uninstall_unloads_and_removes(fake_home, recorded_run, monkeypatch):
    monkeypatch.setattr(daemonctl.platform, "system", lambda: "Darwin")
    daemonctl.install()
    m = daemonctl.LaunchdManager()
    assert m.is_installed()
    daemonctl.uninstall()
    assert not m.is_installed()
    assert ["launchctl", "unload", "-w", str(m.unit_path())] in recorded_run


def test_launchd_linger_is_rejected(fake_home, recorded_run):
    with pytest.raises(daemonctl.DaemonCtlError, match="Linux-only"):
        daemonctl.LaunchdManager().install(linger=True)


# ---- systemd (Linux) -----------------------------------------------------------


def test_systemd_unit_execs_shim_with_service_env_and_restart(fake_home):
    text = daemonctl.SystemdManager()._unit_text()
    assert "ExecStart=" in text and "applier" in text
    assert "Environment=ALLE_SERVICE=1" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text
    assert "StandardOutput=journal" in text


def test_systemd_install_enables_now(fake_home, recorded_run, monkeypatch):
    monkeypatch.setattr(daemonctl.shutil, "which", lambda name: f"/usr/bin/{name}")
    m = daemonctl.SystemdManager()
    m.install()
    assert m.is_installed()
    assert ["systemctl", "--user", "enable", "--now", "alle.service"] in recorded_run
    assert ["systemctl", "--user", "daemon-reload"] in recorded_run


def test_systemd_install_linger_enables_linger(fake_home, recorded_run, monkeypatch):
    monkeypatch.setattr(daemonctl.shutil, "which", lambda name: f"/usr/bin/{name}")
    daemonctl.SystemdManager().install(linger=True)
    assert ["loginctl", "enable-linger"] in recorded_run


# ---- ALLE_HOME carry-through ---------------------------------------------------


def test_service_env_carries_overridden_alle_home(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLE_HOME", str(tmp_path / "state"))
    env = daemonctl._service_env()
    assert env["ALLE_SERVICE"] == "1"
    assert env["ALLE_HOME"].endswith("state")


def test_service_env_omits_alle_home_when_default(monkeypatch):
    monkeypatch.delenv("ALLE_HOME", raising=False)
    assert "ALLE_HOME" not in daemonctl._service_env()


# ---- platform dispatch ---------------------------------------------------------


def test_manager_selects_by_platform(monkeypatch):
    monkeypatch.setattr(daemonctl.platform, "system", lambda: "Darwin")
    assert isinstance(daemonctl.manager(), daemonctl.LaunchdManager)
    monkeypatch.setattr(daemonctl.platform, "system", lambda: "Linux")
    assert isinstance(daemonctl.manager(), daemonctl.SystemdManager)
    monkeypatch.setattr(daemonctl.platform, "system", lambda: "Windows")
    assert daemonctl.manager() is None


def test_install_on_unsupported_platform_errors(monkeypatch):
    monkeypatch.setattr(daemonctl.platform, "system", lambda: "Windows")
    with pytest.raises(daemonctl.DaemonCtlError, match="no user-level service backend"):
        daemonctl.install()
