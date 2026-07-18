"""Upgrade alle via the tool that installed it — never by replacing itself.

The never-self-update invariant: alle's files are owned by whatever installed
them (uv tool, pipx, pip), and only that owner may replace them. ``alle
upgrade`` detects the owning channel and delegates; the service layer then
bounces the daemon so the new code takes over (the supervised
self-exit-on-version-change covers the service-managed case regardless).

Channels that cannot be upgraded from here refuse with the right instruction
instead of guessing: a container image is immutable (pull a new tag), a git
checkout belongs to git, and an undetectable channel gets no blind command run
against it.

The version *check* is a separate, explicitly user-invoked action
(``alle upgrade --check`` / the Web UI button): it asks PyPI for the latest
release and reports — it never fires in the background and never changes
anything.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

from alle import daemon

PACKAGE = "alle-proxy"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
UPGRADE_TIMEOUT = 300.0  # resolver + download on a slow network
CHECK_TIMEOUT = 10.0


class UpgradeError(RuntimeError):
    """A user-facing refusal or failure; the message says what to do instead."""


# ---- channel detection ------------------------------------------------------


def _editable_install() -> bool:
    """Installed with ``pip install -e`` / ``uv pip install -e`` (a checkout)?"""
    try:
        from importlib.metadata import PackageNotFoundError, distribution

        try:
            dist = distribution(PACKAGE)
        except PackageNotFoundError:
            return False
        text = dist.read_text("direct_url.json")
    except OSError:
        return False
    if not text:
        return False
    try:
        info = json.loads(text)
    except ValueError:
        return False
    return bool((info.get("dir_info") or {}).get("editable"))


def _dist_exists() -> bool:
    try:
        from importlib.metadata import version

        version(PACKAGE)
        return True
    except Exception:  # noqa: BLE001 — any metadata failure reads as "not installed"
        return False


def detect_channel() -> str:
    """One of ``uv-tool`` / ``pipx`` / ``pip`` / ``checkout`` / ``container`` /
    ``unknown`` — the tool that owns this installation's files."""
    from alle import runtime

    if runtime.in_container():
        return "container"
    if _editable_install():
        return "checkout"
    prefix = sys.prefix.replace("\\", "/")
    if "/uv/tools/" in prefix:
        return "uv-tool"
    if "/pipx/venvs/" in prefix:
        return "pipx"
    if _dist_exists():
        return "pip"
    return "unknown"


_REFUSALS = {
    "container": (
        "this alle runs inside a container image — the image is immutable. "
        "Upgrade by pulling a new image tag and recreating the container."
    ),
    "checkout": (
        "this alle is a git checkout — upgrade it with git (git pull) and "
        "your usual environment sync, not a package manager."
    ),
    "unknown": (
        "could not determine the install channel; upgrade alle with the tool "
        "that installed it."
    ),
}


def _command_for(channel: str) -> list[str]:
    if channel == "pip":
        return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE]
    tool = {"uv-tool": "uv", "pipx": "pipx"}[channel]
    exe = shutil.which(tool)
    if not exe:
        raise UpgradeError(
            f"this alle was installed with {tool}, but `{tool}` is not on PATH — "
            f"upgrade needs the owning tool."
        )
    if channel == "uv-tool":
        return [exe, "tool", "upgrade", PACKAGE]
    return [exe, "upgrade", PACKAGE]


# ---- the delegated upgrade --------------------------------------------------


def run() -> dict:
    """Delegate the upgrade to the owning channel and report versions.

    Raises :class:`UpgradeError` on refusal channels and on a failed
    delegated command (with the command's own error tail — that output is the
    actionable part)."""
    channel = detect_channel()
    if channel in _REFUSALS:
        raise UpgradeError(_REFUSALS[channel])
    cmd = _command_for(channel)
    before = daemon.installed_version()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=UPGRADE_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise UpgradeError(
            f"`{' '.join(cmd)}` did not finish within {int(UPGRADE_TIMEOUT)}s"
        ) from e
    except OSError as e:
        raise UpgradeError(f"could not run `{' '.join(cmd)}`: {e}") from e
    if r.returncode != 0:
        tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-8:])
        raise UpgradeError(f"`{' '.join(cmd)}` failed (exit {r.returncode}):\n{tail}")
    after = daemon.installed_version()  # re-read from disk, not the import cache
    return {
        "channel": channel,
        "command": cmd,
        "before": before,
        "after": after,
        "changed": after != before,
    }


# ---- the on-demand version check --------------------------------------------


def _version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in v.split(".")[:4]:
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)


def _version_newer(latest: str, current: str) -> bool:
    """Is ``latest`` strictly newer? Never true for an equal or older release,
    so a dev build ahead of PyPI is not offered a downgrade."""
    return _version_tuple(latest) > _version_tuple(current)


def _fetch_pypi_version(timeout: float) -> str:
    import urllib.request

    req = urllib.request.Request(PYPI_JSON_URL, headers={"Accept": "application/json"})  # noqa: S310 — fixed https/loopback URL, no user-supplied scheme
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URL
            data = json.load(resp)
    except (OSError, ValueError) as e:
        raise UpgradeError(
            f"could not reach PyPI to check the latest version: {e}"
        ) from e
    latest = str((data.get("info") or {}).get("version") or "")
    if not latest:
        raise UpgradeError("PyPI's response carried no version")
    return latest


def check_latest() -> dict:
    """Ask PyPI (now, because the user asked) for the latest release."""
    current = daemon.installed_version()
    latest = _fetch_pypi_version(CHECK_TIMEOUT)
    return {
        "current": current,
        "latest": latest,
        "update_available": _version_newer(latest, current),
    }
