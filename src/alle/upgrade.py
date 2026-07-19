"""Upgrade alle via the tool that installed it — never by replacing itself.

The never-self-update invariant: alle's files are owned by whatever installed
them (Homebrew, uv tool, pipx, pip), and only that owner may replace them.
``alle upgrade`` detects the owning channel, checks its authoritative release
source, and delegates only when a newer release exists. The service layer then
hands daemon restart responsibility to the correct supervisor.

Channels that cannot be upgraded from here refuse with the right instruction
instead of guessing: a container image is immutable (pull a new tag), a git
checkout belongs to git, and an undetectable channel gets no blind command run
against it.

The version *check* is a separate, explicitly user-invoked action
(``alle upgrade --check`` / the Web UI button): it asks the owning channel for
the latest release and reports — it never fires in the background and never
changes anything.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from alle import daemon

PACKAGE = "alle-proxy"
PYPI_JSON_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
HOMEBREW_FORMULA_URL = (
    "https://raw.githubusercontent.com/zydo/homebrew-tap/main/Formula/alle.rb"
)
UPGRADE_TIMEOUT = 300.0  # resolver + download on a slow network
CHECK_TIMEOUT = 10.0
PYPI_SIMPLE_URL = "https://pypi.org/simple"

# The checked source and delegated install source are one invariant. These
# variables can otherwise make pip, uv, or pipx's pip/uv backend resolve the
# checked package from a different index or local wheel directory. Manager
# home/bin variables are not source controls; the owning channel's values are
# re-derived and validated below so the exact installation remains targeted.
_PYTHON_SOURCE_ENV = {
    "PIP_CONFIG_FILE",
    "PIP_CONSTRAINT",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_INDEX_URL",
    "PIP_ISOLATED",
    "PIP_NO_INDEX",
    "PIP_PRE",
    "PIP_TRUSTED_HOST",
    "UV_CONFIG_FILE",
    "UV_BUILD_CONSTRAINT",
    "UV_CONSTRAINT",
    "UV_DEFAULT_INDEX",
    "UV_EXCLUDE",
    "UV_EXCLUDE_NEWER",
    "UV_EXCLUDE_NEWER_PACKAGE",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_FORK_STRATEGY",
    "UV_INDEX",
    "UV_INDEX_STRATEGY",
    "UV_INDEX_URL",
    "UV_INSECURE_HOST",
    "UV_NO_BINARY",
    "UV_NO_BINARY_PACKAGE",
    "UV_NO_BUILD",
    "UV_NO_BUILD_ISOLATION",
    "UV_NO_BUILD_PACKAGE",
    "UV_NO_CONFIG",
    "UV_NO_INDEX",
    "UV_OFFLINE",
    "UV_OVERRIDE",
    "UV_PRERELEASE",
    "UV_RESOLUTION",
    "UV_TORCH_BACKEND",
}


class UpgradeError(RuntimeError):
    """A user-facing refusal or failure; the message says what to do instead."""


class UpgradeBusyError(UpgradeError):
    """Another process currently owns the per-user upgrade transaction."""


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
    if not isinstance(info, dict):
        return False
    directory = info.get("dir_info")
    return isinstance(directory, dict) and bool(directory.get("editable"))


def _dist_exists() -> bool:
    try:
        from importlib.metadata import version

        version(PACKAGE)
        return True
    except Exception:  # noqa: BLE001 — any metadata failure reads as "not installed"
        return False


def _owns_package(value: object) -> bool:
    return isinstance(value, str) and canonicalize_name(value) == canonicalize_name(
        PACKAGE
    )


def _uv_receipt_owns(prefix: Path) -> bool:
    """Whether ``prefix`` is the uv tool environment for alle.

    ``UV_TOOL_DIR`` can put tool environments anywhere, so the receipt is the
    primary ownership signal. Python 3.11+ has a TOML parser; the narrow
    fallback keeps alle's Python 3.10 support without adding a runtime parser
    dependency and reads only uv's ``[tool] requirements`` assignment.
    """
    try:
        text = (prefix / "uv-receipt.toml").read_text()
    except OSError:
        return False
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
        if not re.search(r"(?m)^\s*\[tool\]\s*$", text):
            return False
        assignment = re.search(r"(?ms)^\s*requirements\s*=\s*\[(.*?)\]\s*$", text)
        if assignment is None:
            return False
        names = re.findall(r"\bname\s*=\s*['\"]([^'\"]+)['\"]", assignment.group(1))
        return any(_owns_package(name) for name in names)
    try:
        receipt = tomllib.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(receipt, dict):
        return False
    tool = receipt.get("tool")
    if not isinstance(tool, dict):
        return False
    requirements = tool.get("requirements")
    if not isinstance(requirements, list):
        return False
    return any(
        isinstance(item, dict) and _owns_package(item.get("name"))
        for item in requirements
    )


def _uv_receipt_alle_entrypoint(prefix: Path) -> Path | None:
    """The external ``alle`` path recorded by uv's owning receipt."""
    try:
        text = (prefix / "uv-receipt.toml").read_text()
    except OSError:
        return None
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
        records = re.findall(r"\{([^{}]*)\}", text)
        for record in records:
            # key = "value" / key = 'value' pairs, split-parsed instead of a
            # regex: an unanchored ([\w-]+)\s*= scan backtracks super-linearly
            # on long word runs, and this fallback may read attacker-adjacent
            # bytes (a receipt file). Machine-generated inline tables never
            # quote commas inside values, so a comma split is faithful here.
            fields: dict[str, str] = {}
            for pair in record.split(","):
                key, eq, value = pair.partition("=")
                value = value.strip()
                if eq and len(value) >= 2 and value[0] == value[-1] in "'\"":
                    fields[key.strip()] = value[1:-1]
            if (
                fields.get("name") == "alle"
                and _owns_package(fields.get("from"))
                and fields.get("install-path")
            ):
                path = Path(fields["install-path"])
                return path if path.is_absolute() else None
        return None
    try:
        receipt = tomllib.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(receipt, dict):
        return None
    tool = receipt.get("tool")
    if not isinstance(tool, dict):
        return None
    entrypoints = tool.get("entrypoints")
    if not isinstance(entrypoints, list):
        return None
    for item in entrypoints:
        if not isinstance(item, dict):
            continue
        install_path = item.get("install-path")
        if (
            item.get("name") == "alle"
            and _owns_package(item.get("from"))
            and isinstance(install_path, str)
        ):
            path = Path(install_path)
            return path if path.is_absolute() else None
    return None


def _pipx_receipt_owns(prefix: Path) -> bool:
    """Whether ``prefix`` is pipx's environment for alle."""
    try:
        receipt = json.loads((prefix / "pipx_metadata.json").read_text())
    except (OSError, TypeError, ValueError):
        return False
    if not isinstance(receipt, dict):
        return False
    main = receipt.get("main_package")
    return isinstance(main, dict) and _owns_package(main.get("package"))


def _owning_shim_dir(channel: str) -> Path | None:
    """Validate and return the external bin dir for this uv/pipx environment."""
    prefix = Path(sys.prefix)
    if channel == "uv-tool" and _uv_receipt_owns(prefix):
        expected = _uv_receipt_alle_entrypoint(prefix)
        if expected is None:
            raise UpgradeError("uv's owning receipt records no usable alle entrypoint")
    elif channel == "pipx" and _pipx_receipt_owns(prefix):
        expected = prefix / "bin" / "alle"
    else:
        # Old/missing/corrupt receipts still have one verifiable relationship:
        # the external shim must resolve to the console script in the detected
        # conventional tool environment. Never carry a stale manager bin
        # override merely because the fallback has no exposure metadata.
        normalized = str(prefix).replace("\\", "/").lower()
        conventional = (channel == "uv-tool" and "/uv/tools/" in normalized) or (
            channel == "pipx" and "/pipx/venvs/" in normalized
        )
        if not conventional:
            return None
        expected = prefix / "bin" / "alle"

    found = shutil.which("alle")
    candidate = Path(os.path.abspath(found)) if found else None
    try:
        matches = bool(candidate and candidate.samefile(expected))
    except OSError:
        matches = False
    if candidate is None or not matches:
        rendered = str(candidate) if candidate is not None else "nothing"
        raise UpgradeError(
            f"the detected {channel} environment owns this alle installation, "
            f"but PATH resolves the alle shim to {rendered} instead of {expected}; "
            "put the owning shim directory first on PATH and retry."
        )
    return candidate.parent


def detect_channel() -> str:
    """One of ``uv-tool`` / ``pipx`` / ``homebrew`` / ``pip`` / ``checkout`` /
    ``container`` / ``unknown`` — the tool that owns this installation's files."""
    from alle import runtime

    if runtime.in_container():
        return "container"
    if _editable_install():
        return "checkout"
    prefix_path = Path(sys.prefix)
    prefix = str(prefix_path).replace("\\", "/")
    low = prefix.lower()
    # A Homebrew keg's venv lives in the formula's libexec under Cellar (or the
    # `opt` symlink); the headless brew channel upgrades via `brew upgrade`.
    if "/cellar/alle/" in low or "/homebrew/opt/alle/" in low:
        return "homebrew"
    # Ownership receipts survive arbitrary UV_TOOL_DIR/PIPX_HOME roots. Keep
    # the conventional path shapes as fallback for old/missing/corrupt
    # receipts so existing installations do not lose their upgrade channel.
    if _uv_receipt_owns(prefix_path):
        return "uv-tool"
    if _pipx_receipt_owns(prefix_path):
        return "pipx"
    if "/uv/tools/" in low:
        return "uv-tool"
    if "/pipx/venvs/" in low:
        return "pipx"
    # A receipt at an otherwise custom root is evidence that a tool manager
    # owns this environment even when malformed or for a different package.
    # Falling through to plain pip would mutate manager-owned files with the
    # wrong owner; fail closed unless ownership was proven above.
    if (prefix_path / "uv-receipt.toml").exists() or (
        prefix_path / "pipx_metadata.json"
    ).exists():
        return "unknown"
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


@contextmanager
def _upgrade_lock():
    """Acquire the per-user mutation lock without waiting.

    A CLI process and the daemon-hosted Web API can otherwise both pass their
    version gates and run the same package manager concurrently. The lock spans
    the fresh version read, channel check, delegated mutation, and postcondition
    verification. A second caller gets an immediate actionable busy error.
    """
    import fcntl

    try:
        directory = _upgrade_lock_directory()
        descriptor = os.open(  # noqa: PTH123 - flock requires the directory fd
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as e:
        raise UpgradeError(f"could not open the per-user upgrade lock: {e}") from e
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise UpgradeBusyError(
                "another alle upgrade is already running; wait for it to finish "
                "and try again."
            ) from e
        except OSError as e:
            raise UpgradeError(
                f"could not acquire the per-user upgrade lock: {e}"
            ) from e
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(descriptor)


def _upgrade_lock_directory() -> Path:
    """The existing per-user inode used for lock identity; creates no files.

    ``ALLE_HOME`` selects runtime/config state, but it does not select a second
    installation. Two shells with different state roots can still target the
    same Homebrew keg or uv/pipx/pip environment, so their package mutation
    lock must converge on the OS account's home-directory inode, independently
    of a mutable ``HOME`` environment variable. ``flock`` is advisory and does
    not block normal directory access; using the existing inode leaves no
    product state behind for bootstrap uninstall to purge.
    """
    directory = _account_home()
    metadata = directory.stat()
    # Group-writable homes are legitimate on shared-group Linux systems. The
    # fixed inode's owner—not the permissions of files that may be created
    # inside it—is the identity and safety property this file-free lock needs.
    if metadata.st_uid != os.geteuid():
        raise OSError(f"home directory {directory} is not owned by this user")
    return directory


def _account_home() -> Path:
    """Return the effective user's home recorded by the operating system."""
    import pwd

    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def _python_manager_env(channel: str) -> dict[str, str]:
    """A manager environment that cannot redirect the checked PyPI source."""
    env = os.environ.copy()
    for name in _PYTHON_SOURCE_ENV:
        env.pop(name, None)
    # pipx may own either a pip- or uv-backed environment. These settings make
    # both backends ignore user/system config while explicit command flags set
    # PyPI. The direct pip command also carries --isolated itself.
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_ISOLATED"] = "1"
    env["UV_NO_CONFIG"] = "1"
    prefix = Path(sys.prefix)
    if channel == "uv-tool":
        # UV_TOOL_DIR is the parent of each tool environment. Derive it from
        # the running interpreter so a service that did not inherit the
        # install shell's custom variable still mutates this owning receipt.
        env["UV_TOOL_DIR"] = str(prefix.parent)
        if shim_dir := _owning_shim_dir(channel):
            env["UV_TOOL_BIN_DIR"] = str(shim_dir)
    elif channel == "pipx" and prefix.parent.name.lower() == "venvs":
        # pipx always stores environments under <PIPX_HOME>/venvs. Point its
        # CLI back at that custom root instead of accidentally targeting the
        # user's default home.
        env["PIPX_HOME"] = str(prefix.parent.parent)
        if shim_dir := _owning_shim_dir(channel):
            env["PIPX_BIN_DIR"] = str(shim_dir)
    return env


def _homebrew_prefix(brew: str) -> Path:
    """Return this brew executable's active absolute prefix for alle."""
    prefix_cmd = [brew, "--prefix", "alle"]
    try:
        result = subprocess.run(
            prefix_cmd,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise UpgradeError(
            f"`{' '.join(prefix_cmd)}` did not finish within {int(CHECK_TIMEOUT)}s"
        ) from e
    except OSError as e:
        raise UpgradeError(f"could not run `{' '.join(prefix_cmd)}`: {e}") from e
    if result.returncode != 0:
        raise UpgradeError(_command_error(prefix_cmd, result))
    prefix_text = result.stdout.strip()
    prefix = Path(prefix_text)
    if not prefix_text or not prefix.is_absolute():
        raise UpgradeError(
            f"`{' '.join(prefix_cmd)}` returned an invalid prefix {prefix_text!r}"
        )
    return prefix


def _validate_homebrew_owner(brew: str) -> None:
    """Require PATH's brew to own the Homebrew environment running alle."""
    formula_prefix = _homebrew_prefix(brew)
    expected = formula_prefix / "libexec"
    running = Path(sys.prefix)
    try:
        matches = running.samefile(expected)
    except OSError:
        matches = False
    if not matches:
        raise UpgradeError(
            f"{brew} resolves alle to {expected}, which does not own the running "
            f"Homebrew environment {running}; put its owning brew first on PATH."
        )


def _command_for(channel: str, *, latest: str, exact_target: bool) -> list[str]:
    requirement = f"{PACKAGE}=={latest}" if exact_target else PACKAGE
    if channel == "pip":
        return [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--upgrade",
            "--index-url",
            PYPI_SIMPLE_URL,
            requirement,
        ]
    # The brew keg is named for the formula (`alle`), not the PyPI package.
    tool = {"uv-tool": "uv", "pipx": "pipx", "homebrew": "brew"}[channel]
    exe = shutil.which(tool)
    if not exe:
        raise UpgradeError(
            f"this alle was installed with {tool}, but `{tool}` is not on PATH — "
            f"upgrade needs the owning tool."
        )
    if channel == "uv-tool":
        # `uv tool upgrade` preserves the requirement used at install time.
        # The release bootstrap deliberately installs `alle-proxy==<version>`,
        # so replace that receipt with an unconstrained requirement when a
        # newer release exists instead of remaining pinned forever.
        command = [
            exe,
            "tool",
            "install",
            "--force",
            "--default-index",
            PYPI_SIMPLE_URL,
            "--no-config",
            "--no-sources",
        ]
        if not exact_target:
            command.extend(["--prerelease", "disallow"])
        return [*command, requirement]
    if channel == "homebrew":
        _validate_homebrew_owner(exe)
        return [exe, "upgrade", "alle"]
    # `pipx upgrade` deliberately reuses the receipt's package_or_url. That may
    # be a Git/local/direct source even though alle just checked PyPI. Reinstall
    # through the same owning manager to replace that source: exact for an
    # opted-in prerelease, unpinned for stable so future upgrades remain open.
    return [
        exe,
        "install",
        "--force",
        "--index-url",
        PYPI_SIMPLE_URL,
        requirement,
    ]


def _command_error(cmd: list[str], result: subprocess.CompletedProcess) -> str:
    tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-8:])
    return f"`{' '.join(cmd)}` failed (exit {result.returncode}):\n{tail}"


def _homebrew_installed_version(brew: str) -> str:
    """Read the newly active Homebrew keg, never the caller's old Cellar path.

    A formula console script's interpreter and ``sys.path`` point into its
    versioned keg. After ``brew upgrade`` the still-running old process can only
    see old metadata, so discover the new opt prefix through brew and execute
    that keg's stable shim instead.
    """
    prefix = _homebrew_prefix(brew)

    version_cmd = [str(prefix / "bin" / "alle"), "version"]
    try:
        version_result = subprocess.run(
            version_cmd,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise UpgradeError(
            f"`{' '.join(version_cmd)}` did not finish within {int(CHECK_TIMEOUT)}s"
        ) from e
    except OSError as e:
        raise UpgradeError(f"could not run `{' '.join(version_cmd)}`: {e}") from e
    if version_result.returncode != 0:
        raise UpgradeError(_command_error(version_cmd, version_result))
    installed = version_result.stdout.strip()
    _parse_version(installed)
    return installed


def _verify_postcondition(
    *, before: str, after: str, latest: str, exact_target: bool, cmd: list[str]
) -> None:
    """Require a successful manager command to have installed its target."""
    before_v = _parse_version(before)
    after_v = _parse_version(after)
    latest_v = _parse_version(latest)
    rendered = " ".join(cmd)
    if after_v <= before_v:
        raise UpgradeError(
            f"`{rendered}` completed successfully, but alle did not advance "
            f"from {before} (found {after}; expected {latest})."
        )
    if exact_target and after_v != latest_v:
        raise UpgradeError(
            f"`{rendered}` completed successfully, but installed alle {after} "
            f"instead of the selected prerelease {latest}."
        )
    if not exact_target and (after_v.is_prerelease or after_v.is_devrelease):
        raise UpgradeError(
            f"`{rendered}` completed successfully, but installed prerelease/dev "
            f"alle {after} after a stable-release check (expected at least {latest})."
        )
    if not exact_target and after_v < latest_v:
        raise UpgradeError(
            f"`{rendered}` completed successfully, but installed alle {after}; "
            f"the checked channel requires at least {latest}."
        )


# ---- the delegated upgrade --------------------------------------------------


def run(*, prerelease: bool = False) -> dict:
    """Delegate the upgrade to the owning channel and report versions.

    Raises :class:`UpgradeError` on refusal channels and on a failed
    delegated command (with the command's own error tail — that output is the
    actionable part)."""
    with _upgrade_lock():
        channel = detect_channel()
        if channel in _REFUSALS:
            raise UpgradeError(_REFUSALS[channel])
        if prerelease and channel == "homebrew":
            raise UpgradeError(
                "Homebrew tracks stable alle releases only; use a uv, pipx, or pip "
                "installation to opt into prereleases."
            )
        # This read is deliberately under the interprocess lock. A caller that
        # arrived while another upgrade was finishing must gate on the new
        # installed version, never its stale pre-lock observation.
        before = daemon.installed_version()
        latest = _latest_for_channel(channel, CHECK_TIMEOUT, prerelease=prerelease)
        parsed_latest = _parse_version(latest)
        if not _version_newer(latest, before):
            return {
                "channel": channel,
                "command": None,
                "before": before,
                "after": before,
                "latest": latest,
                "changed": False,
            }
        exact_target = prerelease and parsed_latest.is_prerelease
        # Resolve the manager only after the version gate. An up-to-date install
        # needs no executable on PATH because no manager command will run.
        cmd = _command_for(channel, latest=latest, exact_target=exact_target)
        manager_env = _python_manager_env(channel) if channel != "homebrew" else None
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=UPGRADE_TIMEOUT,
                env=manager_env,
            )
        except subprocess.TimeoutExpired as e:
            raise UpgradeError(
                f"`{' '.join(cmd)}` did not finish within {int(UPGRADE_TIMEOUT)}s"
            ) from e
        except OSError as e:
            raise UpgradeError(f"could not run `{' '.join(cmd)}`: {e}") from e
        if r.returncode != 0:
            raise UpgradeError(_command_error(cmd, r))
        after = (
            _homebrew_installed_version(cmd[0])
            if channel == "homebrew"
            else daemon.installed_version()
        )
        _verify_postcondition(
            before=before,
            after=after,
            latest=latest,
            exact_target=exact_target,
            cmd=cmd,
        )
        return {
            "channel": channel,
            "command": cmd,
            "before": before,
            "after": after,
            "latest": latest,
            "changed": True,
        }


# ---- the on-demand version check --------------------------------------------


def _parse_version(value: str) -> Version:
    try:
        return Version(value)
    except InvalidVersion as e:
        raise UpgradeError(f"invalid release version {value!r}") from e


def _version_newer(latest: str, current: str) -> bool:
    """Is ``latest`` strictly newer? Never true for an equal or older release,
    so a dev build ahead of PyPI is not offered a downgrade."""
    return _parse_version(latest) > _parse_version(current)


def _fetch_pypi_version(timeout: float, *, prerelease: bool = False) -> str:
    import urllib.request

    req = urllib.request.Request(PYPI_JSON_URL, headers={"Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            data = json.load(resp)
    except (OSError, ValueError) as e:
        raise UpgradeError(
            f"could not reach PyPI to check the latest version: {e}"
        ) from e
    if not isinstance(data, dict):
        raise UpgradeError("PyPI's response was not a JSON object")
    releases = data.get("releases", {})
    if not isinstance(releases, dict):
        raise UpgradeError("PyPI's response carried malformed release data")
    candidates: list[Version] = []
    for value, files in releases.items():
        if not isinstance(files, list) or any(
            not isinstance(item, dict) for item in files
        ):
            raise UpgradeError("PyPI's response carried malformed release data")
        try:
            parsed = Version(value)
        except (InvalidVersion, TypeError):
            continue
        usable = files and any(not item.get("yanked", False) for item in files)
        stable = not parsed.is_prerelease and not parsed.is_devrelease
        if usable and (prerelease or stable):
            candidates.append(parsed)
    if candidates:
        return str(max(candidates))

    # PyPI normally supplies the full release map. Keep a strict stable-only
    # fallback for compatible mirrors that expose only `info.version`.
    info = data.get("info", {})
    if not isinstance(info, dict):
        raise UpgradeError("PyPI's response carried malformed package info")
    latest = str(info.get("version") or "")
    if latest and not prerelease:
        parsed = _parse_version(latest)
        if not parsed.is_prerelease and not parsed.is_devrelease:
            return str(parsed)
    kind = "prerelease or stable release" if prerelease else "stable release"
    raise UpgradeError(f"PyPI's response carried no usable {kind}")


def _fetch_homebrew_version(timeout: float) -> str:
    """Read the stable version from the canonical tap formula.

    This is a network read of the same formula source `brew upgrade alle`
    follows, without mutating the user's local tap metadata merely to check.
    The formula derives its version from the pinned PyPI sdist URL.
    """
    import urllib.request

    req = urllib.request.Request(  # noqa: S310
        HOMEBREW_FORMULA_URL, headers={"Accept": "text/plain"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            formula = resp.read().decode("utf-8")
    except (OSError, UnicodeError) as e:
        raise UpgradeError(
            f"could not reach the Homebrew tap to check the latest version: {e}"
        ) from e
    match = re.search(r"alle_proxy-(\d[0-9A-Za-z.+-]*)\.tar\.gz", formula)
    if not match:
        raise UpgradeError("the Homebrew tap formula carried no alle version")
    return match.group(1)


def _latest_for_channel(
    channel: str, timeout: float, *, prerelease: bool = False
) -> str:
    if channel == "homebrew":
        return _fetch_homebrew_version(timeout)
    return _fetch_pypi_version(timeout, prerelease=prerelease)


def check_latest(*, prerelease: bool = False) -> dict:
    """Ask the owning channel (now, because the user asked) for latest."""
    channel = detect_channel()
    if channel in _REFUSALS:
        raise UpgradeError(_REFUSALS[channel])
    if prerelease and channel == "homebrew":
        raise UpgradeError(
            "Homebrew tracks stable alle releases only; use a uv, pipx, or pip "
            "installation to opt into prereleases."
        )
    current = daemon.installed_version()
    latest = _latest_for_channel(channel, CHECK_TIMEOUT, prerelease=prerelease)
    return {
        "channel": channel,
        "current": current,
        "latest": latest,
        "update_available": _version_newer(latest, current),
    }
