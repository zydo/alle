"""Install/uninstall the privileged TUN helper as a macOS system LaunchDaemon.

The companion to :mod:`alle.daemonctl`: ``daemonctl`` installs the **user-level**
applier (``~/Library/LaunchAgents``); this installs the **root** helper that owns
sing-box while tun mode is on (``/Library/LaunchDaemons``). The helper itself is
:mod:`alle.helper`; this module is just the service-management plumbing behind
``sudo alle helper install`` / ``uninstall`` / ``status``.

macOS-only: Linux uses ``setcap`` (no root process, no helper). The plist execs
the stable ``alle`` console-script shim with the hidden ``helper-run`` verb
(stable across ``uv tool upgrade``, exactly like ``alle applier``), and carries
``ALLE_HELPER_ALLOWED_UID`` (the real user behind ``sudo``) plus ``ALLE_HOME``
as environment — so at boot the root helper knows which user it serves and
where that user's state lives. ``install``/``uninstall`` require root
(``/Library/LaunchDaemons`` is root-owned); ``status`` does not.
"""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
from pathlib import Path

from alle import applog, paths
from alle.helper import HELPER_LABEL, HELPER_SOCKET_DEFAULT

LAUNCHD_PLIST = f"/Library/LaunchDaemons/{HELPER_LABEL}.plist"


class HelperCtlError(RuntimeError):
    """A user-correctable problem installing/removing the privileged helper."""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _supported() -> bool:
    return platform.system() == "Darwin"


def _require_darwin() -> None:
    if not _supported():
        raise HelperCtlError(
            f"the privileged helper is macOS-only (Linux uses setcap); "
            f"this is {platform.system()}."
        )


def _require_root(action: str) -> None:
    if os.geteuid() != 0:
        raise HelperCtlError(
            f"alle helper {action} needs root (it writes {LAUNCHD_PLIST}): "
            "run it under sudo."
        )


def _real_uid() -> int:
    """The user behind ``sudo alle helper install`` — the one the helper serves.

    ``sudo`` sets ``SUDO_UID``; without it (already-root shell, or a future root
    LaunchDaemon doing the install) the helper serves root only, which is
    useless for a user-level daemon — refuse rather than install silently
    broken.
    """
    sudo_uid = os.environ.get("SUDO_UID")
    if not sudo_uid:
        raise HelperCtlError(
            "could not determine the real user (SUDO_UID is unset). "
            "Run via:  sudo alle helper install"
        )
    try:
        return int(sudo_uid)
    except ValueError:
        raise HelperCtlError(
            f"bad SUDO_UID {sudo_uid!r}; run via: sudo alle helper install"
        )


def _service_exec() -> list[str]:
    """The stable command the plist execs: the ``alle`` shim + ``helper-run``.

    Prefers the console-script shim (stable across upgrades); falls back to
    ``<python> -m alle helper-run`` only when no shim is on PATH. Mirrors
    daemonctl so an upgrade never orphans the installed unit.
    """
    shim = shutil.which("alle")
    if shim:
        return [shim, "helper-run"]
    return [shutil.which("python3") or "python3", "-m", "alle", "helper-run"]


def _plist_bytes(uid: int, alle_home: str) -> bytes:
    log = str(Path(alle_home) / "helper.log")
    plist = {
        "Label": HELPER_LABEL,
        "ProgramArguments": _service_exec(),
        "EnvironmentVariables": {
            "ALLE_HELPER_ALLOWED_UID": str(uid),
            "ALLE_HELPER_SOCKET": HELPER_SOCKET_DEFAULT,
            "ALLE_HOME": alle_home,
            # The helper runs as root out of the installing user's uv-tool
            # venv. Without this, root would write root-owned .pyc into that
            # venv and later `uv tool upgrade` would fail to remove them. A
            # root daemon does not benefit from bytecode caching anyway.
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "UserName": "root",
        "RunAtLoad": True,
        "KeepAlive": True,  # supervisor: respawn on crash
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    return plistlib.dumps(plist)


def install() -> dict:
    """Write + load the system LaunchDaemon. Must run as root (via sudo)."""
    _require_darwin()
    _require_root("install")
    uid = _real_uid()
    alle_home = str(paths.state_dir())
    already = is_installed()
    p = Path(LAUNCHD_PLIST)
    # Unload any prior generation first so the new plist's env takes effect
    # cleanly (launchctl keeps a stale job definition otherwise).
    if already:
        _run(["launchctl", "unload", "-w", LAUNCHD_PLIST])
    p.write_bytes(_plist_bytes(uid, alle_home))
    r = _run(["launchctl", "load", "-w", LAUNCHD_PLIST])
    if r.returncode != 0:
        # Don't leave a plist that failed to load — it would shadow nothing
        # but read as "installed" to status().
        try:
            p.unlink()
        except OSError:
            pass
        raise HelperCtlError(f"launchctl load failed: {r.stderr.strip()}")
    applog.log(
        f"privileged helper {'reinstalled' if already else 'installed'} "
        f"(serves uid {uid})"
    )
    return {
        "installed": True,
        "reinstalled": already,
        "plist": LAUNCHD_PLIST,
        "serves_uid": uid,
        "socket": HELPER_SOCKET_DEFAULT,
    }


def uninstall() -> dict:
    """Unload + remove the system LaunchDaemon. Must run as root."""
    _require_darwin()
    _require_root("uninstall")
    if not is_installed():
        return {"removed": False}
    # Stop the helper first so it releases the socket and any tun it holds.
    _run(["launchctl", "unload", "-w", LAUNCHD_PLIST])
    try:
        Path(LAUNCHD_PLIST).unlink()
    except OSError as e:
        raise HelperCtlError(f"could not remove {LAUNCHD_PLIST}: {e}") from e
    applog.log("privileged helper removed")
    return {"removed": True, "plist": LAUNCHD_PLIST}


def is_installed() -> bool:
    return _supported() and Path(LAUNCHD_PLIST).exists()


def is_loaded() -> bool:
    """Best-effort: is the LaunchDaemon loaded? Needs root to be reliable for a
    system daemon, so callers that run unprivileged should use the service
    layer's `reachable` (ping) instead. Kept for root-driven flows."""
    if not _supported():
        return False
    return _run(["launchctl", "list", HELPER_LABEL]).returncode == 0


def status() -> dict:
    if not _supported():
        return {"supported": False, "platform": platform.system()}
    installed = is_installed()
    return {
        "supported": True,
        "installed": installed,
        # "loaded"/liveness is NOT reported here: a non-root user cannot
        # reliably query a system daemon's load state (`launchctl list` of a
        # system LaunchDaemon needs root). The service layer adds `reachable`
        # — a root-free ping — as the authoritative liveness signal.
        "plist": LAUNCHD_PLIST if installed else None,
        "socket": HELPER_SOCKET_DEFAULT if installed else None,
    }
