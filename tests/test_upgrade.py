"""``alle upgrade`` channel checks, delegated mutation, and restart ownership.

Every package-manager and network boundary is faked. The tests cover stable and
prerelease transitions, post-install verification, refusal channels, and the
special versioned-keg behavior of Homebrew.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from importlib import metadata
from io import BytesIO

import pytest

from alle import service, upgrade

_REAL_UPGRADE_LOCK_DIRECTORY = upgrade._upgrade_lock_directory


@pytest.fixture(autouse=True)
def isolated_upgrade_lock(monkeypatch, tmp_path):
    """Never contend through the developer/CI account's real user lock."""
    directory = tmp_path / "account"
    directory.mkdir(mode=0o700)

    monkeypatch.setattr(upgrade, "_upgrade_lock_directory", lambda: directory)


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


@pytest.mark.parametrize("payload", ["[]", '{"dir_info": "not-an-object"}'])
def test_malformed_direct_url_metadata_is_not_treated_as_editable(monkeypatch, payload):
    distribution = type("Distribution", (), {"read_text": lambda self, name: payload})()
    monkeypatch.setattr(metadata, "distribution", lambda package: distribution)

    assert upgrade._editable_install() is False


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


def test_detects_uv_tool_from_receipt_under_a_custom_root(monkeypatch, tmp_path):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    prefix = tmp_path / "company-managed" / "alle-environment"
    prefix.mkdir(parents=True)
    (prefix / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "alle_proxy" }]\n'
        'entrypoints = [{ name = "alle", from = "alle-proxy" }]\n'
    )
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))

    assert upgrade.detect_channel() == "uv-tool"


def test_detects_pipx_from_metadata_under_a_custom_root(monkeypatch, tmp_path):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    prefix = tmp_path / "managed-apps" / "venvs" / "alle-proxy"
    prefix.mkdir(parents=True)
    (prefix / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": "Alle.Proxy"}})
    )
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))

    assert upgrade.detect_channel() == "pipx"


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("uv-receipt.toml", 'tool = "not-a-table"\n'),
        ("uv-receipt.toml", '[tool]\nrequirements = "not-a-list"\n'),
        ("pipx_metadata.json", "[]"),
        ("pipx_metadata.json", '{"main_package": "not-a-table"}'),
    ],
)
def test_malformed_custom_root_receipts_refuse_instead_of_falling_through_to_pip(
    monkeypatch, tmp_path, filename, contents
):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    monkeypatch.setattr(upgrade, "_dist_exists", lambda: True)
    prefix = tmp_path / "ordinary-venv"
    prefix.mkdir()
    (prefix / filename).write_text(contents)
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))

    assert upgrade.detect_channel() == "unknown"


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("/srv/UV/TOOLS/alle-proxy", "uv-tool"),
        ("/srv/PIPX/VENVS/alle-proxy", "pipx"),
    ],
)
def test_manager_path_heuristics_remain_as_receipt_fallback(
    monkeypatch, prefix, expected
):
    _no_container(monkeypatch)
    monkeypatch.setattr(upgrade, "_editable_install", lambda: False)
    monkeypatch.setattr(upgrade.sys, "prefix", prefix)

    assert upgrade.detect_channel() == expected


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


# ---- mutation serialization ------------------------------------------------


def test_upgrade_lock_returns_busy_without_running_channel_checks(monkeypatch):
    with upgrade._upgrade_lock():
        monkeypatch.setattr(
            upgrade,
            "detect_channel",
            lambda: pytest.fail("a busy caller must stop before channel/network work"),
        )
        with pytest.raises(upgrade.UpgradeBusyError, match="already running"):
            upgrade.run()


def test_upgrade_lock_excludes_a_second_process():
    directory = upgrade._upgrade_lock_directory()
    probe = (
        "import fcntl, os, sys\n"
        "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    raise SystemExit(75)\n"
    )

    with upgrade._upgrade_lock():
        result = subprocess.run(
            [sys.executable, "-c", probe, str(directory)],
            capture_output=True,
            text=True,
        )

    assert result.returncode == 75


def test_upgrade_lock_inode_is_fixed_across_different_alle_homes(monkeypatch, tmp_path):
    account = tmp_path / "account"
    account.mkdir(mode=0o700, exist_ok=True)
    account.chmod(0o770)  # legitimate shared-group home remains a usable inode
    monkeypatch.setattr(upgrade, "_account_home", lambda: account)
    # Restore the production target function hidden by this module's isolation
    # fixture, then prove ALLE_HOME does not select a second package lock.
    monkeypatch.setattr(
        upgrade, "_upgrade_lock_directory", _REAL_UPGRADE_LOCK_DIRECTORY
    )
    monkeypatch.setenv("ALLE_HOME", str(tmp_path / "state-a"))
    monkeypatch.setenv("HOME", str(tmp_path / "shell-home-a"))
    first = upgrade._upgrade_lock_directory()
    with upgrade._upgrade_lock():
        monkeypatch.setenv("ALLE_HOME", str(tmp_path / "state-b"))
        monkeypatch.setenv("HOME", str(tmp_path / "shell-home-b"))
        assert upgrade._upgrade_lock_directory() == first
        with pytest.raises(upgrade.UpgradeBusyError, match="already running"):
            with upgrade._upgrade_lock():
                pass
    assert list(account.iterdir()) == []  # locking leaves no uninstall residue


def test_run_rereads_version_under_lock_before_an_exact_rc_gate(monkeypatch):
    held = {"value": False}

    @contextmanager
    def serialized():
        held["value"] = True
        try:
            yield
        finally:
            held["value"] = False

    monkeypatch.setattr(upgrade, "_upgrade_lock", serialized)
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "uv-tool")
    # The stale pre-lock view would accept 0.1.9rc1 over 0.1.8. The fresh
    # under-lock view is already 0.1.9 final, so the exact RC must not run and
    # downgrade it.
    monkeypatch.setattr(
        upgrade.daemon,
        "installed_version",
        lambda: "0.1.9" if held["value"] else "0.1.8",
    )

    def latest(channel, timeout, *, prerelease=False):
        assert held["value"] is True
        assert prerelease is True
        return "0.1.9rc1"

    monkeypatch.setattr(upgrade, "_latest_for_channel", latest)
    monkeypatch.setattr(
        upgrade.shutil,
        "which",
        lambda name: pytest.fail("the stale exact-RC mutation must be gated off"),
    )

    result = upgrade.run(prerelease=True)

    assert result["changed"] is False
    assert result["command"] is None
    assert result["before"] == result["after"] == "0.1.9"


# ---- delegation -------------------------------------------------------------


def test_run_delegates_to_uv_and_reports_versions(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "uv-tool")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    versions = iter(["0.1.8", "0.1.9"])
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: next(versions))
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    calls = []
    manager_envs = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        manager_envs.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    res = upgrade.run()
    # the owning tool runs by resolved absolute path, never bare-name PATH lookup
    assert calls == [
        [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "--default-index",
            upgrade.PYPI_SIMPLE_URL,
            "--no-config",
            "--no-sources",
            "--prerelease",
            "disallow",
            "alle-proxy",
        ]
    ]
    assert res["channel"] == "uv-tool"
    assert res["before"] == "0.1.8" and res["after"] == "0.1.9"
    assert res["changed"] is True
    assert manager_envs[0]["UV_NO_CONFIG"] == "1"
    assert "UV_INDEX" not in manager_envs[0]


def test_run_surfaces_a_failed_command(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pipx")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no network")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    with pytest.raises(upgrade.UpgradeError, match="no network"):
        upgrade.run()


def test_run_requires_the_owning_tool_on_path(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "uv-tool")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: None)
    with pytest.raises(upgrade.UpgradeError, match="PATH"):
        upgrade.run()


def test_run_delegates_to_brew_upgrade(monkeypatch):
    # The keg is named for the formula (`alle`), not the PyPI package.
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "homebrew")
    monkeypatch.setattr(
        upgrade.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}"
    )
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    monkeypatch.setattr(upgrade, "_homebrew_installed_version", lambda brew: "0.1.9")
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    monkeypatch.setattr(upgrade, "_validate_homebrew_owner", lambda brew: None)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    res = upgrade.run()
    assert calls == [["/opt/homebrew/bin/brew", "upgrade", "alle"]]
    assert res["channel"] == "homebrew"
    assert res["changed"] is True


def test_homebrew_manager_must_own_the_running_formula_environment(
    monkeypatch, tmp_path
):
    running = tmp_path / "cellar-a" / "alle" / "0.1.8" / "libexec"
    running.mkdir(parents=True)
    other = tmp_path / "cellar-b" / "alle" / "0.1.8"
    (other / "libexec").mkdir(parents=True)
    monkeypatch.setattr(upgrade.sys, "prefix", str(running))
    monkeypatch.setattr(upgrade, "_homebrew_prefix", lambda brew: other)

    with pytest.raises(upgrade.UpgradeError, match="does not own the running"):
        upgrade._validate_homebrew_owner("/other/homebrew/bin/brew")


def test_homebrew_manager_accepts_its_opt_symlink_to_the_running_keg(
    monkeypatch, tmp_path
):
    keg = tmp_path / "Cellar" / "alle" / "0.1.8"
    (keg / "libexec").mkdir(parents=True)
    opt = tmp_path / "opt" / "alle"
    opt.parent.mkdir(parents=True)
    opt.symlink_to(keg, target_is_directory=True)
    monkeypatch.setattr(upgrade.sys, "prefix", str(keg / "libexec"))
    monkeypatch.setattr(upgrade, "_homebrew_prefix", lambda brew: opt)

    upgrade._validate_homebrew_owner("/owning/homebrew/bin/brew")


def test_homebrew_post_version_comes_from_the_new_keg_shim(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:] == ["--prefix", "alle"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="/opt/homebrew/opt/alle\n", stderr=""
            )
        if cmd == ["/opt/homebrew/opt/alle/bin/alle", "version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0.1.9\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)

    assert upgrade._homebrew_installed_version("/opt/homebrew/bin/brew") == "0.1.9"
    assert calls == [
        ["/opt/homebrew/bin/brew", "--prefix", "alle"],
        ["/opt/homebrew/opt/alle/bin/alle", "version"],
    ]


def test_run_requires_brew_on_path(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "homebrew")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: None)
    with pytest.raises(upgrade.UpgradeError, match="PATH"):
        upgrade.run()


def test_pip_channel_upgrades_via_the_running_interpreter(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pip")
    versions = iter(["0.1.8", "0.1.9"])
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: next(versions))
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run)
    upgrade.run()
    assert calls == [
        [
            sys.executable,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--upgrade",
            "--index-url",
            upgrade.PYPI_SIMPLE_URL,
            "alle-proxy",
        ]
    ]


@pytest.mark.parametrize("channel", ["homebrew", "uv-tool", "pipx", "pip"])
def test_run_does_not_invoke_owner_when_already_current(monkeypatch, channel):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: channel)
    monkeypatch.setattr(
        upgrade.shutil,
        "which",
        lambda name: pytest.fail("an up-to-date install must not resolve its manager"),
    )
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.9")
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    monkeypatch.setattr(
        upgrade.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("package manager must not run"),
    )
    result = upgrade.run()
    assert result["changed"] is False
    assert result["command"] is None
    assert result["before"] == result["after"] == "0.1.9"


def test_prerelease_upgrade_uses_exact_uv_requirement(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "uv-tool")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    versions = iter(["0.1.9rc1", "0.1.9rc2"])
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: next(versions))

    def latest(channel, timeout, *, prerelease=False):
        assert prerelease is True
        return "0.1.9rc2"

    monkeypatch.setattr(upgrade, "_latest_for_channel", latest)
    calls = []
    monkeypatch.setattr(
        upgrade.subprocess,
        "run",
        lambda cmd, **kw: (
            calls.append(cmd)
            or subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        ),
    )
    result = upgrade.run(prerelease=True)
    assert calls == [
        [
            "/usr/bin/uv",
            "tool",
            "install",
            "--force",
            "--default-index",
            upgrade.PYPI_SIMPLE_URL,
            "--no-config",
            "--no-sources",
            "alle-proxy==0.1.9rc2",
        ]
    ]
    assert result["changed"] is True


def test_prerelease_pipx_uses_forced_versioned_install(monkeypatch):
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert upgrade._command_for("pipx", latest="0.1.9rc1", exact_target=True) == [
        "/usr/bin/pipx",
        "install",
        "--force",
        "--index-url",
        upgrade.PYPI_SIMPLE_URL,
        "alle-proxy==0.1.9rc1",
    ]


def test_stable_pipx_force_install_replaces_any_recorded_source(monkeypatch):
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert upgrade._command_for("pipx", latest="0.1.9", exact_target=False) == [
        "/usr/bin/pipx",
        "install",
        "--force",
        "--index-url",
        upgrade.PYPI_SIMPLE_URL,
        "alle-proxy",
    ]


def test_python_manager_environment_preserves_owner_but_removes_source_overrides(
    monkeypatch, tmp_path
):
    prefix = tmp_path / "custom-uv-tools" / "alle-proxy"
    prefix.mkdir(parents=True)
    bin_dir = tmp_path / "custom-bin"
    bin_dir.mkdir()
    shim = bin_dir / "alle"
    shim.write_text("#!/bin/sh\n")
    (prefix / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = [{{ name = "alle-proxy" }}]\n'
        f'entrypoints = [{{ name = "alle", install-path = "{shim}", '
        'from = "alle-proxy" }]\n'
    )
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))
    monkeypatch.setattr(
        upgrade.shutil, "which", lambda name: str(shim) if name == "alle" else None
    )
    monkeypatch.setenv("UV_TOOL_DIR", "/stale/uv-tools")
    monkeypatch.setenv("PIPX_HOME", "/custom/pipx")
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror.invalid/simple")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://extra.invalid/simple")
    monkeypatch.setenv("UV_INDEX", "https://uv-mirror.invalid/simple")
    monkeypatch.setenv("UV_FIND_LINKS", "/tmp/untrusted-wheels")
    monkeypatch.setenv("UV_CONFIG_FILE", "/tmp/alternate-uv.toml")
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "2024-01-01")
    monkeypatch.setenv("UV_EXCLUDE_NEWER_PACKAGE", "alle-proxy=2024-01-01")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_RESOLUTION", "lowest-direct")

    env = upgrade._python_manager_env("uv-tool")

    assert env["UV_TOOL_DIR"] == str(prefix.parent)
    assert env["UV_TOOL_BIN_DIR"] == str(bin_dir)
    assert env["PIPX_HOME"] == "/custom/pipx"
    assert "PIP_INDEX_URL" not in env
    assert "PIP_EXTRA_INDEX_URL" not in env
    assert "UV_INDEX" not in env
    assert "UV_FIND_LINKS" not in env
    assert "UV_EXCLUDE_NEWER" not in env
    assert "UV_EXCLUDE_NEWER_PACKAGE" not in env
    assert "UV_OFFLINE" not in env
    assert "UV_RESOLUTION" not in env
    assert env["PIP_CONFIG_FILE"] == upgrade.os.devnull
    assert env["PIP_ISOLATED"] == "1"
    assert env["UV_NO_CONFIG"] == "1"


def test_pipx_manager_environment_targets_the_receipt_custom_home(
    monkeypatch, tmp_path
):
    prefix = tmp_path / "custom-pipx" / "venvs" / "alle-proxy"
    internal = prefix / "bin" / "alle"
    internal.parent.mkdir(parents=True)
    internal.write_text("#!/bin/sh\n")
    (prefix / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": "alle-proxy"}})
    )
    bin_dir = tmp_path / "custom-bin"
    bin_dir.mkdir()
    shim = bin_dir / "alle"
    shim.symlink_to(internal)
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))
    monkeypatch.setenv("PIPX_HOME", "/stale/default")
    monkeypatch.setattr(
        upgrade.shutil, "which", lambda name: str(shim) if name == "alle" else None
    )

    env = upgrade._python_manager_env("pipx")

    assert env["PIPX_HOME"] == str(tmp_path / "custom-pipx")
    assert env["PIPX_BIN_DIR"] == str(bin_dir)


def test_uv_path_fallback_derives_bin_dir_from_the_validated_shim(
    monkeypatch, tmp_path
):
    prefix = tmp_path / "share" / "uv" / "tools" / "alle-proxy"
    internal = prefix / "bin" / "alle"
    internal.parent.mkdir(parents=True)
    internal.write_text("#!/bin/sh\n")
    bin_dir = tmp_path / "custom-bin"
    bin_dir.mkdir()
    shim = bin_dir / "alle"
    shim.symlink_to(internal)
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", "/stale/bin")
    monkeypatch.setattr(
        upgrade.shutil, "which", lambda name: str(shim) if name == "alle" else None
    )

    env = upgrade._python_manager_env("uv-tool")

    assert env["UV_TOOL_DIR"] == str(prefix.parent)
    assert env["UV_TOOL_BIN_DIR"] == str(bin_dir)


def test_manager_environment_rejects_a_different_alle_shim(monkeypatch, tmp_path):
    prefix = tmp_path / "custom-pipx" / "venvs" / "alle-proxy"
    internal = prefix / "bin" / "alle"
    internal.parent.mkdir(parents=True)
    internal.write_text("#!/bin/sh\n")
    (prefix / "pipx_metadata.json").write_text(
        json.dumps({"main_package": {"package": "alle-proxy"}})
    )
    wrong = tmp_path / "other-bin" / "alle"
    wrong.parent.mkdir()
    wrong.write_text("#!/bin/sh\n")
    monkeypatch.setattr(upgrade.sys, "prefix", str(prefix))
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: str(wrong))

    with pytest.raises(upgrade.UpgradeError, match="instead of"):
        upgrade._python_manager_env("pipx")


def test_stable_pipx_force_install_replaces_an_exact_rc_receipt(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pipx")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    versions = iter(["0.1.9rc1", "0.1.9"])
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: next(versions))
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    calls = []
    monkeypatch.setattr(
        upgrade.subprocess,
        "run",
        lambda cmd, **kw: (
            calls.append(cmd)
            or subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        ),
    )

    result = upgrade.run()

    assert calls == [
        [
            "/usr/bin/pipx",
            "install",
            "--force",
            "--index-url",
            upgrade.PYPI_SIMPLE_URL,
            "alle-proxy",
        ]
    ]
    assert result["after"] == "0.1.9"
    assert result["changed"] is True


def test_successful_manager_noop_is_an_upgrade_error(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "uv-tool")
    monkeypatch.setattr(upgrade.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    monkeypatch.setattr(
        upgrade.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout="already satisfied", stderr=""
        ),
    )

    with pytest.raises(upgrade.UpgradeError, match="did not advance"):
        upgrade.run()


def test_successful_manager_must_reach_the_checked_target(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pip")
    versions = iter(["0.1.7", "0.1.8"])
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: next(versions))
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    monkeypatch.setattr(
        upgrade.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    with pytest.raises(upgrade.UpgradeError, match="requires at least 0.1.9"):
        upgrade.run()


@pytest.mark.parametrize("unexpected", ["0.2.0rc1", "0.2.0.dev1"])
def test_stable_upgrade_rejects_a_higher_prerelease_or_dev_result(
    monkeypatch, unexpected
):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pip")
    versions = iter(["0.1.8", unexpected])
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: next(versions))
    monkeypatch.setattr(
        upgrade, "_latest_for_channel", lambda channel, timeout, **kw: "0.1.9"
    )
    monkeypatch.setattr(
        upgrade.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    with pytest.raises(upgrade.UpgradeError, match="stable-release check"):
        upgrade.run()


def test_homebrew_refuses_prerelease_channel(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "homebrew")
    with pytest.raises(upgrade.UpgradeError, match="stable alle releases only"):
        upgrade.run(prerelease=True)
    with pytest.raises(upgrade.UpgradeError, match="stable alle releases only"):
        upgrade.check_latest(prerelease=True)


@pytest.mark.parametrize("channel", ["container", "checkout", "unknown"])
def test_check_latest_refuses_channels_without_an_authoritative_source(
    monkeypatch, channel
):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: channel)
    monkeypatch.setattr(
        upgrade,
        "_latest_for_channel",
        lambda *args, **kwargs: pytest.fail("a refusal channel must not use PyPI"),
    )
    with pytest.raises(upgrade.UpgradeError, match="image|git|install channel"):
        upgrade.check_latest()


# ---- the on-demand version check -------------------------------------------


def test_check_latest_reports_update_available(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "uv-tool")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    monkeypatch.setattr(upgrade, "_fetch_pypi_version", lambda timeout, **kw: "0.1.9")
    res = upgrade.check_latest()
    assert res == {
        "channel": "uv-tool",
        "current": "0.1.8",
        "latest": "0.1.9",
        "update_available": True,
    }


def test_check_latest_reports_up_to_date(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pipx")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.9")
    monkeypatch.setattr(upgrade, "_fetch_pypi_version", lambda timeout, **kw: "0.1.9")
    assert upgrade.check_latest()["update_available"] is False


def test_check_latest_never_downgrades(monkeypatch):
    # a dev version ahead of PyPI is not an "update available"
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "pip")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upgrade, "_fetch_pypi_version", lambda timeout, **kw: "0.1.9")
    assert upgrade.check_latest()["update_available"] is False


def test_homebrew_check_uses_tap_instead_of_pypi(monkeypatch):
    monkeypatch.setattr(upgrade, "detect_channel", lambda: "homebrew")
    monkeypatch.setattr(upgrade.daemon, "installed_version", lambda: "0.1.8")
    monkeypatch.setattr(upgrade, "_fetch_homebrew_version", lambda timeout: "0.1.9")
    monkeypatch.setattr(
        upgrade,
        "_fetch_pypi_version",
        lambda timeout, **kw: pytest.fail("brew check must not query PyPI"),
    )
    result = upgrade.check_latest()
    assert result["channel"] == "homebrew"
    assert result["latest"] == "0.1.9"
    assert result["update_available"] is True


def test_homebrew_formula_version_is_parsed_from_sdist_url(monkeypatch):
    formula = b'''url "https://files.pythonhosted.org/x/alle_proxy-0.1.10.tar.gz"'''
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: BytesIO(formula)
    )
    assert upgrade._fetch_homebrew_version(3.0) == "0.1.10"


def test_homebrew_formula_without_version_is_rejected(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: BytesIO(b"class Alle < Formula\nend\n"),
    )
    with pytest.raises(upgrade.UpgradeError, match="carried no alle version"):
        upgrade._fetch_homebrew_version(3.0)


def test_pypi_prerelease_selection_uses_newest_non_yanked_release(monkeypatch):
    data = {
        "info": {"version": "0.1.9"},
        "releases": {
            "0.1.9": [{"yanked": False}],
            "0.2.0rc1": [{"yanked": False}],
            "0.2.0rc2": [{"yanked": True}],
            "not-a-version": [{"yanked": False}],
        },
    }
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: BytesIO(json.dumps(data).encode()),
    )
    assert upgrade._fetch_pypi_version(3.0) == "0.1.9"
    assert upgrade._fetch_pypi_version(3.0, prerelease=True) == "0.2.0rc1"


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"releases": []},
        {"releases": {"0.1.9": "not-a-list"}},
        {"releases": {"0.1.9": ["not-an-object"]}},
        {"releases": {}, "info": []},
    ],
)
def test_malformed_pypi_shapes_are_user_facing_upgrade_errors(monkeypatch, data):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout: BytesIO(json.dumps(data).encode()),
    )

    with pytest.raises(upgrade.UpgradeError, match="PyPI's response"):
        upgrade._fetch_pypi_version(3.0)


def test_version_ordering():
    assert upgrade._version_newer("0.1.10", "0.1.9") is True
    assert upgrade._version_newer("0.1.9", "0.1.10") is False
    assert upgrade._version_newer("1.0.0", "0.9.9") is True
    assert upgrade._version_newer("0.1.9", "0.1.9") is False
    assert upgrade._version_newer("0.1.9rc1", "0.1.8") is True
    assert upgrade._version_newer("0.1.9rc2", "0.1.9rc1") is True
    assert upgrade._version_newer("0.1.9", "0.1.9rc2") is True
    assert upgrade._version_newer("0.1.9rc1", "0.1.9") is False


def test_invalid_version_is_rejected():
    with pytest.raises(upgrade.UpgradeError, match="invalid release version"):
        upgrade._version_newer("not a version", "0.1.9")


# ---- service wrapper: restart lands on the new code -------------------------


def test_service_upgrade_restarts_when_version_changed(monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "run",
        lambda **kwargs: {
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
        lambda **kwargs: {
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


def test_service_leaves_brew_supervised_restart_to_homebrew(monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "run",
        lambda **kwargs: {
            "channel": "homebrew",
            "before": "0.1.8",
            "after": "0.1.9",
            "changed": True,
        },
    )
    monkeypatch.setattr(service.daemon, "in_daemon_process", lambda: False)
    monkeypatch.setattr(service.daemon, "is_running", lambda: True)
    monkeypatch.setattr(
        service.daemon,
        "daemon_info",
        lambda: {"pid": 42, "service_owner": "homebrew"},
    )
    monkeypatch.setattr(
        service,
        "restart",
        lambda: pytest.fail("the old Homebrew keg must not perform a restart"),
    )

    result = service.upgrade_run()

    assert result["restart_pending"] is True
    assert result["restart_owner"] == "homebrew"
    assert "restart" not in result


def test_service_requires_explicit_restart_for_unsupervised_brew_daemon(monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "run",
        lambda **kwargs: {
            "channel": "homebrew",
            "before": "0.1.8",
            "after": "0.1.9",
            "changed": True,
        },
    )
    monkeypatch.setattr(service.daemon, "in_daemon_process", lambda: True)
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: {"pid": 42})
    monkeypatch.setattr(
        service,
        "restart",
        lambda: pytest.fail("the old Homebrew keg must not spawn itself"),
    )

    result = service.upgrade_run()

    assert result["restart_required"] is True
    assert result["restart_command"] == "brew services restart alle"
    assert "restart" not in result


def test_service_upgrade_maps_refusals_to_service_errors(monkeypatch):
    def refuse(**kwargs):
        raise upgrade.UpgradeError("this alle runs in a container image")

    monkeypatch.setattr(upgrade, "run", refuse)
    with pytest.raises(service.ServiceError, match="container image"):
        service.upgrade_run()


def test_service_upgrade_preserves_typed_busy_error(monkeypatch):
    def busy(**kwargs):
        raise upgrade.UpgradeBusyError("another alle upgrade is already running")

    monkeypatch.setattr(upgrade, "run", busy)
    with pytest.raises(service.ServiceBusyError, match="already running"):
        service.upgrade_run()
