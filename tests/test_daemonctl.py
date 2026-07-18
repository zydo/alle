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
    assert 'Environment="ALLE_SERVICE=1"' in text
    # always, not on-failure: the self-restart-on-upgrade exit must respawn
    assert "Restart=always" in text
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


# ---- failure propagation + atomic restart ---------------------------------------


def _failing_run(monkeypatch, fail: dict[str, str]):
    """_run stub failing commands whose joined form contains a key of ``fail``
    (value = stderr); everything else succeeds. Returns the call log."""
    calls: list[list[str]] = []

    def fake(cmd):
        calls.append(cmd)

        class _R:
            returncode = 0
            stderr = ""
            stdout = ""

        joined = " ".join(cmd)
        for marker, err in fail.items():
            if marker in joined:
                _R.returncode = 1
                _R.stderr = err
        return _R()

    monkeypatch.setattr(daemonctl, "_run", fake)
    return calls


def test_systemd_start_failure_is_raised(monkeypatch, fake_home):
    _failing_run(monkeypatch, {"start": "Failed to start alle.service"})
    with pytest.raises(daemonctl.DaemonCtlError, match="systemctl start failed"):
        daemonctl.SystemdManager().start()


def test_systemd_stop_failure_raises_only_when_still_active(monkeypatch, fake_home):
    m = daemonctl.SystemdManager()
    # stop fails and the unit is still active → a real, surfaced failure
    _failing_run(monkeypatch, {"stop": "boom"})
    monkeypatch.setattr(m, "is_active", lambda: True)
    with pytest.raises(daemonctl.DaemonCtlError, match="systemctl stop failed"):
        m.stop()
    # stop "fails" but nothing is running (not-loaded noise) → tolerated
    monkeypatch.setattr(m, "is_active", lambda: False)
    m.stop()


def test_systemd_restart_is_one_noblock_job(monkeypatch, fake_home):
    calls = _failing_run(monkeypatch, {})
    daemonctl.SystemdManager().restart()
    assert calls == [["systemctl", "--user", "restart", "--no-block", "alle.service"]]


def test_systemd_restart_failure_is_raised(monkeypatch, fake_home):
    _failing_run(monkeypatch, {"restart": "no such unit"})
    with pytest.raises(daemonctl.DaemonCtlError, match="systemctl restart failed"):
        daemonctl.SystemdManager().restart()


def test_systemd_linger_failure_fails_the_install(monkeypatch, fake_home):
    monkeypatch.setattr(daemonctl.shutil, "which", lambda name: f"/usr/bin/{name}")
    _failing_run(monkeypatch, {"enable-linger": "access denied"})
    with pytest.raises(daemonctl.DaemonCtlError, match="enable-linger failed"):
        daemonctl.SystemdManager().install(linger=True)


def test_systemd_linger_requires_loginctl(monkeypatch, fake_home, recorded_run):
    monkeypatch.setattr(
        daemonctl.shutil,
        "which",
        lambda name: None if name == "loginctl" else f"/usr/bin/{name}",
    )
    with pytest.raises(daemonctl.DaemonCtlError, match="loginctl not found"):
        daemonctl.SystemdManager().install(linger=True)


def test_launchd_start_failure_is_raised(monkeypatch, fake_home):
    m = daemonctl.LaunchdManager()
    _failing_run(monkeypatch, {"load": "nope", "list": "not running"})
    with pytest.raises(daemonctl.DaemonCtlError, match="launchctl load failed"):
        m.start()


def test_launchd_start_tolerates_already_loaded(monkeypatch, fake_home):
    m = daemonctl.LaunchdManager()
    # load exits non-zero ("already loaded") but the job IS active → fine
    _failing_run(monkeypatch, {"load": "already loaded"})
    m.start()


def test_launchd_stop_failure_raises_only_when_still_active(monkeypatch, fake_home):
    m = daemonctl.LaunchdManager()
    _failing_run(monkeypatch, {"unload": "boom"})
    monkeypatch.setattr(m, "is_active", lambda: True)
    with pytest.raises(daemonctl.DaemonCtlError, match="launchctl unload failed"):
        m.stop()
    monkeypatch.setattr(m, "is_active", lambda: False)
    m.stop()  # not-loaded noise is tolerated


def test_launchd_restart_kickstarts_atomically(monkeypatch, fake_home):
    calls = _failing_run(monkeypatch, {})
    daemonctl.LaunchdManager().restart()
    assert len(calls) == 1
    assert calls[0][:3] == ["launchctl", "kickstart", "-k"]
    assert calls[0][3].endswith(daemonctl.LAUNCHD_LABEL)


def test_launchd_restart_falls_back_to_unload_load(monkeypatch, fake_home):
    calls = _failing_run(monkeypatch, {"kickstart": "Unknown subcommand"})
    daemonctl.LaunchdManager().restart()
    joined = [" ".join(c[:2]) for c in calls]
    assert joined[0] == "launchctl kickstart"
    assert "launchctl unload" in joined and "launchctl load" in joined


def test_restart_service_requires_an_installed_unit(monkeypatch, fake_home):
    monkeypatch.setattr(daemonctl.platform, "system", lambda: "Linux")
    assert daemonctl.restart_service() is False  # no unit file → nothing to ask


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


# ---- unit-file safety: native escaping, no raw interpolation -------------------


def test_systemd_unit_escapes_percent_and_quotes_values(fake_home, monkeypatch):
    # `%` is a systemd specifier and spaces split arguments: a home like
    # "/tmp/my %state dir" must round-trip through the unit file literally.
    home = str(fake_home / "my %state dir")
    monkeypatch.setenv("ALLE_HOME", home)
    monkeypatch.setattr(
        daemonctl, "_service_exec", lambda: ["/usr/bin/alle", "applier"]
    )
    text = daemonctl.SystemdManager()._unit_text()
    assert 'Environment="ALLE_HOME=' in text
    assert home.replace("%", "%%") in text
    # every % is escaped: no occurrence of the token with a single (bare) %
    assert text.count("%state") == text.count("%%state")


def test_systemd_unit_quotes_exec_arguments(fake_home, monkeypatch):
    monkeypatch.setattr(
        daemonctl, "_service_exec", lambda: ["/opt/od d/alle 100%", "applier"]
    )
    monkeypatch.delenv("ALLE_HOME", raising=False)
    text = daemonctl.SystemdManager()._unit_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))
    assert '"/opt/od d/alle 100%%"' in line
    assert '"applier"' in line


def test_systemd_refuses_values_a_unit_cannot_represent(fake_home, monkeypatch):
    monkeypatch.setenv("ALLE_HOME", str(fake_home) + "/bad\nname")
    with pytest.raises(daemonctl.DaemonCtlError, match="newline"):
        daemonctl.SystemdManager()._unit_text()


# ---- rollback safety: a failed install must not strand the previous setup ------


def _failing_systemctl_enable(monkeypatch):
    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    class _Fail(_Result):
        returncode = 1
        stderr = "enable refused"

    def fake(cmd):
        if cmd[:2] == ["systemctl", "--user"] and "enable" in cmd:
            return _Fail()
        return _Result()

    monkeypatch.setattr(daemonctl, "_run", fake)


def test_systemd_failed_fresh_install_leaves_no_unit_file(fake_home, monkeypatch):
    monkeypatch.setattr(daemonctl.shutil, "which", lambda name: f"/usr/bin/{name}")
    _failing_systemctl_enable(monkeypatch)
    m = daemonctl.SystemdManager()
    with pytest.raises(daemonctl.DaemonCtlError, match="enable refused"):
        m.install()
    assert not m.is_installed()  # no half-installed unit left behind


def test_systemd_failed_reinstall_restores_previous_unit(fake_home, monkeypatch):
    monkeypatch.setattr(daemonctl.shutil, "which", lambda name: f"/usr/bin/{name}")
    m = daemonctl.SystemdManager()
    m.unit_path().parent.mkdir(parents=True, exist_ok=True)
    m.unit_path().write_text("[Unit]\nDescription=previous generation\n")
    _failing_systemctl_enable(monkeypatch)
    with pytest.raises(daemonctl.DaemonCtlError):
        m.install()
    assert "previous generation" in m.unit_path().read_text()


def test_launchd_failed_fresh_install_leaves_no_plist(fake_home, monkeypatch):
    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    class _Fail(_Result):
        returncode = 1
        stderr = "load refused"

    def fake(cmd):
        return _Fail() if cmd[:2] == ["launchctl", "load"] else _Result()

    monkeypatch.setattr(daemonctl, "_run", fake)
    m = daemonctl.LaunchdManager()
    with pytest.raises(daemonctl.DaemonCtlError, match="load refused"):
        m.install()
    assert not m.is_installed()


def test_service_daemon_install_restores_manual_daemon_on_failure(monkeypatch):
    from alle import service

    monkeypatch.setattr(service.daemonctl, "require_backend", lambda: None)
    monkeypatch.setattr(service.daemon, "is_running", lambda: True)
    stops, restores = [], []
    monkeypatch.setattr(service.daemon, "stop", lambda: stops.append(1) or True)
    monkeypatch.setattr(service.daemon, "ensure_running", lambda: restores.append(1))

    def failing_install(linger=False):
        raise service.daemonctl.DaemonCtlError("launchctl load failed")

    monkeypatch.setattr(service.daemonctl, "install", failing_install)
    with pytest.raises(service.ServiceError, match="load failed"):
        service.daemon_install()
    # the manually running daemon was brought back, not left stopped
    assert stops == [1] and restores == [1]


# ---- uninstall keeps evidence when the stop half fails -------------------------


def test_systemd_uninstall_keeps_unit_when_disable_fails_live(fake_home, monkeypatch):
    m = daemonctl.SystemdManager()
    m.unit_path().parent.mkdir(parents=True, exist_ok=True)
    m.unit_path().write_text("[Unit]\n")

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    class _Fail(_Result):
        returncode = 1
        stderr = "disable refused"

    def fake(cmd):
        if "disable" in cmd:
            return _Fail()
        return _Result()  # is-active reports active (returncode 0)

    monkeypatch.setattr(daemonctl, "_run", fake)
    with pytest.raises(daemonctl.DaemonCtlError, match="disable"):
        m.uninstall()
    assert m.is_installed()  # evidence kept for diagnosis


def test_launchd_uninstall_keeps_plist_when_unload_fails_live(fake_home, monkeypatch):
    m = daemonctl.LaunchdManager()
    m.unit_path().parent.mkdir(parents=True, exist_ok=True)
    m.unit_path().write_bytes(b"plist")

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    class _Fail(_Result):
        returncode = 1
        stderr = "unload refused"

    def fake(cmd):
        if cmd[:2] == ["launchctl", "unload"]:
            return _Fail()
        return _Result()  # launchctl list says the job is still alive

    monkeypatch.setattr(daemonctl, "_run", fake)
    with pytest.raises(daemonctl.DaemonCtlError, match="unload"):
        m.uninstall()
    assert m.is_installed()


def test_systemd_linger_failure_names_the_installed_service(monkeypatch, fake_home):
    # enable succeeds, linger fails: the error must say the service itself was
    # installed and started, so the user doesn't uninstall a working setup.
    monkeypatch.setattr(daemonctl.shutil, "which", lambda name: f"/usr/bin/{name}")

    class _Result:
        returncode = 0
        stderr = ""
        stdout = ""

    class _Fail(_Result):
        returncode = 1
        stderr = "linger refused"

    def fake(cmd):
        return _Fail() if cmd[:1] == ["loginctl"] else _Result()

    monkeypatch.setattr(daemonctl, "_run", fake)
    with pytest.raises(daemonctl.DaemonCtlError, match="service was installed"):
        daemonctl.SystemdManager().install(linger=True)
