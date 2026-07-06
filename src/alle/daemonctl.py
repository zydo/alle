"""Install/uninstall alle's background daemon as a user-level login service.

This is the plumbing behind ``alle daemon install`` — the CLI is the single home
of service-management logic (never a shell script or docs recipe, which are not
version-matched to the installed alle). Two backends:

* **macOS** — a per-user LaunchAgent plist in ``~/Library/LaunchAgents``,
  loaded with ``launchctl``.
* **Linux** — a ``systemd --user`` unit in ``~/.config/systemd/user``.

Both **exec the stable console-script shim** ``alle applier`` (e.g.
``~/.local/bin/alle``), not a path inside the versioned venv: uv/pipx keep that
shim stable across upgrades, so the unit survives ``uv tool upgrade`` untouched.
``alle applier`` also already matches the daemon's PID-identity markers, so the
supervised process is recognised by the same liveness checks as a hand-spawned
one. The unit sets ``ALLE_SERVICE=1`` so the daemon knows it is supervised (arms
self-restart-on-upgrade), and carries ``ALLE_HOME`` when the user overrode it.

Login persistence: LaunchAgent / ``systemd --user`` both mean *auto-start at
login, run for the login session*. A macOS LaunchAgent cannot survive logout
(that needs a root LaunchDaemon, which breaks the user-level invariant); on Linux
``--linger`` opts into logout survival via ``loginctl enable-linger``.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from alle import applog, paths

LAUNCHD_LABEL = "com.github.zydo.alle"
SYSTEMD_UNIT = "alle.service"


class DaemonCtlError(RuntimeError):
    """A user-correctable problem installing/removing the login service."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a service-manager command, capturing output (injectable for tests)."""
    return subprocess.run(cmd, capture_output=True, text=True)


def _service_exec() -> list[str]:
    """The stable command a unit should exec: the ``alle`` shim + ``applier``.

    Prefers the console-script shim on PATH (stable across upgrades); falls back
    to ``<python> -m alle applier`` only when no shim is found (e.g. an odd
    install). Both spellings contain the ``alle applier`` PID marker.
    """
    shim = shutil.which("alle")
    if shim:
        return [shim, "applier"]
    return [sys.executable, "-m", "alle", "applier"]


def _service_env() -> dict[str, str]:
    """Environment the unit must carry: the supervised marker, and ALLE_HOME
    when the user overrode it (so the service uses the same state dir)."""
    env = {"ALLE_SERVICE": "1"}
    if os.environ.get("ALLE_HOME"):
        env["ALLE_HOME"] = str(paths.state_dir())
    return env


# ---- backends ----------------------------------------------------------------


class _Manager:
    name = "unknown"

    def unit_path(self) -> Path:  # pragma: no cover - overridden
        raise NotImplementedError

    def is_installed(self) -> bool:
        return self.unit_path().exists()

    def is_active(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError

    def install(self, linger: bool = False) -> None:  # pragma: no cover
        raise NotImplementedError

    def uninstall(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def stop(self) -> None:  # pragma: no cover
        raise NotImplementedError


class LaunchdManager(_Manager):
    name = "launchd"

    def unit_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"

    def _plist_bytes(self) -> bytes:
        log = str(paths.state_dir() / "applier.log")
        env = _service_env()
        plist = {
            "Label": LAUNCHD_LABEL,
            "ProgramArguments": _service_exec(),
            "EnvironmentVariables": env,
            "RunAtLoad": True,
            "KeepAlive": True,  # supervisor: respawn on crash / self-restart-on-upgrade
            "ProcessType": "Background",
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
        return plistlib.dumps(plist)

    def install(self, linger: bool = False) -> None:
        if linger:
            raise DaemonCtlError(
                "macOS has no user-level logout survival (--linger is Linux-only); "
                "a LaunchAgent auto-starts at login and runs for the session."
            )
        p = self.unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _run(["launchctl", "unload", str(p)])  # idempotent: clear any prior load
        p.write_bytes(self._plist_bytes())
        r = _run(["launchctl", "load", "-w", str(p)])
        if r.returncode != 0:
            raise DaemonCtlError(f"launchctl load failed: {r.stderr.strip()}")

    def uninstall(self) -> None:
        p = self.unit_path()
        if p.exists():
            _run(["launchctl", "unload", "-w", str(p)])
            p.unlink(missing_ok=True)

    def is_active(self) -> bool:
        return _run(["launchctl", "list", LAUNCHD_LABEL]).returncode == 0

    def start(self) -> None:
        _run(["launchctl", "load", str(self.unit_path())])

    def stop(self) -> None:
        # Unload removes the job so KeepAlive can't resurrect it this session;
        # the plist stays in LaunchAgents, so it reloads at next login.
        _run(["launchctl", "unload", str(self.unit_path())])


class SystemdManager(_Manager):
    name = "systemd"

    def unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT

    def _unit_text(self) -> str:
        exec_line = " ".join(_service_exec())
        env_lines = "\n".join(f"Environment={k}={v}" for k, v in _service_env().items())
        return (
            "[Unit]\n"
            "Description=alle VPN daemon\n"
            "After=network-online.target\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={exec_line}\n"
            f"{env_lines}\n"
            "Restart=on-failure\n"
            "RestartSec=3\n"
            "StandardOutput=journal\n"
            "StandardError=journal\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return _run(["systemctl", "--user", *args])

    def install(self, linger: bool = False) -> None:
        if not shutil.which("systemctl"):
            raise DaemonCtlError("systemctl not found — is this a systemd system?")
        p = self.unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self._unit_text())
        self._systemctl("daemon-reload")
        r = self._systemctl("enable", "--now", SYSTEMD_UNIT)
        if r.returncode != 0:
            raise DaemonCtlError(f"systemctl enable failed: {r.stderr.strip()}")
        if linger and shutil.which("loginctl"):
            _run(["loginctl", "enable-linger"])

    def uninstall(self) -> None:
        self._systemctl("disable", "--now", SYSTEMD_UNIT)
        self.unit_path().unlink(missing_ok=True)
        self._systemctl("daemon-reload")

    def is_active(self) -> bool:
        return self._systemctl("is-active", "--quiet", SYSTEMD_UNIT).returncode == 0

    def start(self) -> None:
        self._systemctl("start", SYSTEMD_UNIT)

    def stop(self) -> None:
        self._systemctl("stop", SYSTEMD_UNIT)


def manager() -> _Manager | None:
    """The service manager for this OS, or None on an unsupported platform."""
    system = platform.system()
    if system == "Darwin":
        return LaunchdManager()
    if system == "Linux":
        return SystemdManager()
    return None


def _require_manager() -> _Manager:
    m = manager()
    if m is None:
        raise DaemonCtlError(
            f"no user-level service backend for {platform.system()} "
            "(supported: macOS launchd, Linux systemd --user)."
        )
    return m


def is_installed() -> bool:
    """True if a login-service unit exists for alle (ownership-handoff signal)."""
    m = manager()
    return m is not None and m.is_installed()


# ---- operations (used by the service layer) ----------------------------------


def install(linger: bool = False) -> dict:
    """Install + start the login service. Eagerly fetches sing-box so the
    service starts ready without a runtime download."""
    from alle import singbox  # lazy: keep the CLI hot path free of the sing-box stack

    m = _require_manager()
    already = m.is_installed()
    try:
        singbox.ensure_binary()  # eager: service starts without a download stall
    except singbox.SingBoxError as e:
        raise DaemonCtlError(f"could not pre-fetch sing-box: {e}") from e
    m.install(linger=linger)
    applog.log(f"daemon service {'reinstalled' if already else 'installed'} ({m.name})")
    return {
        "manager": m.name,
        "unit_path": str(m.unit_path()),
        "reinstalled": already,
        "linger": linger,
    }


def uninstall() -> dict:
    """Remove the login service (leaves ~/.alle state intact)."""
    m = _require_manager()
    if not m.is_installed():
        return {"manager": m.name, "removed": False}
    m.uninstall()
    applog.log(f"daemon service removed ({m.name})")
    return {"manager": m.name, "removed": True, "unit_path": str(m.unit_path())}


def status() -> dict:
    """Where the login service stands, for ``alle daemon status``."""
    m = manager()
    if m is None:
        return {"supported": False, "platform": platform.system()}
    installed = m.is_installed()
    return {
        "supported": True,
        "manager": m.name,
        "installed": installed,
        "active": m.is_active() if installed else False,
        "unit_path": str(m.unit_path()),
    }


def start_service() -> bool:
    """Ask the supervisor to (re)start the service. True if one is installed."""
    m = manager()
    if m is None or not m.is_installed():
        return False
    m.start()
    return True


def stop_service() -> bool:
    """Ask the supervisor to stop the service (so KeepAlive/Restart doesn't
    resurrect it). True if one is installed."""
    m = manager()
    if m is None or not m.is_installed():
        return False
    m.stop()
    return True
