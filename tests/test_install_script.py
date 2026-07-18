"""The release-pinned macOS/Linux uv bootstrap and its failure boundaries."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nset -eu\n" + text)
    path.chmod(0o755)


def _base_host(tmp_path: Path, *, system: str = "Linux", machine: str = "x86_64"):
    home = tmp_path / "home"
    root = tmp_path / "root"
    bin_dir = tmp_path / "bin"
    home.mkdir()
    (root / "etc").mkdir(parents=True)
    (root / "proc/1").mkdir(parents=True)
    (root / "etc/os-release").write_text('ID=testlinux\nPRETTY_NAME="Test Linux"\n')
    (root / "proc/version").write_text("Linux version test\n")
    (root / "proc/1/cgroup").write_text("0::/user.slice\n")
    _write_executable(
        bin_dir / "uname",
        f'case "${{1:-}}" in -s) echo {system};; -m) echo {machine};; '
        '-r) echo 6.8.0-test;; *) echo "unsupported uname call" >&2; exit 2;; esac\n',
    )
    _write_executable(
        bin_dir / "id", 'if [ "${1:-}" = -u ]; then echo 1000; else exit 2; fi\n'
    )
    _write_executable(bin_dir / "systemctl", "exit 0\n")
    env = os.environ.copy()
    # The repository test session sets an isolated ALLE_HOME globally. These
    # installer subprocesses model a fresh user's login environment unless a
    # test opts into a custom state directory explicitly.
    env.pop("ALLE_HOME", None)
    env.pop("XDG_STATE_HOME", None)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "_ALLE_INSTALL_TEST_ROOT": str(root),
            "MUTATION_LOG": str(tmp_path / "mutations"),
        }
    )
    return env, home, root, bin_dir


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(INSTALLER), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


def _mock_uv(bin_dir: Path, home: Path, *, package_fails: bool = False) -> None:
    fail = "exit 42" if package_fails else ""
    _write_executable(
        bin_dir / "uv",
        f"""
tool_bin=${{UV_TOOL_BIN_DIR:-$HOME/.local/bin}}
tools_dir=${{UV_TOOL_DIR:-$HOME/.local/share/uv/tools}}
if [ "$1" = "--version" ]; then echo "uv 0.11.29"; exit 0; fi
if [ "$1 $2 ${{3:-}}" = "tool install --help" ]; then [ "${{INSTALL_HELP_FAIL:-0}}" = 0 ] || exit 91; echo "--no-config --force --no-sources --default-index"; exit 0; fi
if [ "$1 $2 ${{3:-}}" = "tool update-shell --help" ]; then exit "${{UPDATE_SHELL_HELP_FAIL:-0}}"; fi
if [ "$1 $2" = "tool update-shell" ]; then exit "${{UPDATE_SHELL_FAIL:-0}}"; fi
if [ "$1 $2" = "tool list" ]; then
  [ "${{UV_LIST_EMPTY:-0}}" = 0 ] && [ -x "$tool_bin/alle" ] && echo "alle-proxy 0.1.8" || true
  exit 0
fi
if [ "$1 $2 ${{3:-}}" = "tool dir --bin" ]; then echo "$tool_bin"; exit 0; fi
if [ "$1 $2" = "tool dir" ]; then echo "$tools_dir"; exit 0; fi
if [ "$1 $2" = "tool install" ]; then
  [ -z "${{UV_OVERRIDE:-}}" ]
  [ -z "${{UV_CONSTRAINT:-}}" ]
  [ -z "${{UV_BUILD_CONSTRAINT:-}}" ]
  [ -z "${{UV_EXCLUDE:-}}" ]
  [ -z "${{UV_EXCLUDE_NEWER:-}}" ]
  [ -z "${{UV_EXCLUDE_NEWER_PACKAGE:-}}" ]
  [ -z "${{UV_PRERELEASE:-}}" ]
  [ -z "${{UV_INDEX_STRATEGY:-}}" ]
  [ -z "${{UV_RESOLUTION:-}}" ]
  [ -z "${{UV_FORK_STRATEGY:-}}" ]
  [ -z "${{UV_TORCH_BACKEND:-}}" ]
  [ -z "${{UV_OFFLINE:-}}" ]
  [ -z "${{UV_NO_BINARY:-}}" ]
  [ -z "${{UV_NO_BINARY_PACKAGE:-}}" ]
  [ -z "${{UV_NO_BUILD:-}}" ]
  [ -z "${{UV_NO_BUILD_PACKAGE:-}}" ]
  [ -z "${{UV_NO_BUILD_ISOLATION:-}}" ]
  echo package >> "$MUTATION_LOG"
  {fail}
  mkdir -p "$tool_bin" "$tools_dir/alle-proxy/bin"
  cp "{bin_dir}/alle-fixture" "$tool_bin/alle"
  cp "{bin_dir}/python-fixture" "$tools_dir/alle-proxy/bin/python"
  chmod +x "$tool_bin/alle" "$tools_dir/alle-proxy/bin/python"
  exit 0
fi
if [ "$1 $2" = "tool uninstall" ]; then
  echo tool-uninstall >> "$MUTATION_LOG"
  [ "${{UNINSTALL_TOOL_FAIL:-0}}" = 0 ] || exit 43
  rm -f "$tool_bin/alle"
  rm -rf "$tools_dir/alle-proxy"
  if [ "${{INTERRUPT_AFTER_TOOL_REMOVE:-0}}" = 1 ]; then kill -TERM "$PPID"; fi
  exit 0
fi
exit 93
""",
    )
    _write_executable(
        bin_dir / "alle-fixture",
        """
case "$1 ${2:-}" in
  "version ") echo 0.1.9 ;;
  "daemon install")
    mkdir -p "${ALLE_HOME:-$HOME/.alle}"
    echo service >> "$MUTATION_LOG"
    printf '%s\n' "$PATH" > "${SERVICE_PATH_LOG:-/dev/null}"
    printf '%s\n' "${ALLE_HOME:-$HOME/.alle}" > "${SERVICE_HOME_LOG:-/dev/null}"
    if [ "${3:-}" = --linger ] && { [ "${SERVICE_FAIL:-0}" = 0 ] || [ "${ENABLE_LINGER_BEFORE_FAIL:-0}" = 1 ]; }; then loginctl enable-linger; fi
    [ "${SERVICE_FAIL:-0}" = 0 ]
    ;;
  "stop ") echo stop >> "$MUTATION_LOG"; [ "${STOP_FAIL:-0}" = 0 ] ;;
  "daemon uninstall") echo service-uninstall >> "$MUTATION_LOG"; [ "${UNINSTALL_SERVICE_FAIL:-0}" = 0 ] ;;
  "daemon status") printf '%s\n' '{"service":{"installed":true,"active":true}}' ;;
  "helper status") printf '%s\n' "${ALLE_HOME:-$HOME/.alle}" > "${HELPER_HOME_LOG:-/dev/null}"; if [ "${HELPER_STATUS_MALFORMED:-false}" = true ]; then printf '%s\n' '{}'; elif [ "${HELPER_INSTALLED:-false}" = true ]; then printf '%s\n' '{"supported":true,"installed":true}'; else printf '%s\n' '{"supported":true,"installed":false}'; fi ;;
  "health ") exit 0 ;;
  *) exit 94 ;;
esac
""",
    )
    _write_executable(bin_dir / "python-fixture", 'exit "${HEADLESS_FAIL:-0}"\n')


def _mock_loginctl(bin_dir: Path) -> None:
    _write_executable(
        bin_dir / "loginctl",
        """
case "$1" in
  show-user)
    linger_state=$(cat "$LINGER_STATE_FILE")
    if [ "${LINGER_QUERY_UNKNOWN_WHEN_YES:-0}" = 1 ] && [ "$linger_state" = yes ]; then echo unknown; else echo "$linger_state"; fi
    ;;
  enable-linger) echo yes > "$LINGER_STATE_FILE"; echo linger-enable >> "$MUTATION_LOG" ;;
  disable-linger) echo no > "$LINGER_STATE_FILE"; echo linger-disable >> "$MUTATION_LOG" ;;
  *) exit 92 ;;
esac
""",
    )


def test_embedded_release_and_uv_pins_match_sources():
    text = INSTALLER.read_text()
    project = (ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version = "([^"]+)"', project, re.MULTILINE)
    assert match is not None
    version = match.group(1)
    assert f'ALLE_VERSION="{version}"' in text
    assert 'UV_VERSION="0.11.29"' in text
    assert (
        'UV_INSTALLER_SHA256="504a79fd2ed0dcd47e7f04f0792cfd0871f62e24a7fe40fa8ae0f563a369f2bd"'
        in text
    )
    assert "alle-proxy==$ALLE_VERSION" in text
    assert "--default-index https://pypi.org/simple" in text
    assert "-u UV_FIND_LINKS" in text
    assert "-u UV_DOWNLOAD_URL" in text
    assert "-u CARGO_DIST_FORCE_INSTALL_DIR" in text
    assert "-u UV_UNMANAGED_INSTALL" in text
    assert "-u XDG_BIN_HOME" in text
    assert "-u XDG_DATA_HOME" in text
    assert "-u UV_OVERRIDE" in text
    assert "-u UV_CONSTRAINT" in text
    assert "-u UV_BUILD_CONSTRAINT" in text
    assert "-u UV_EXCLUDE" in text
    assert "-u UV_EXCLUDE_NEWER" in text
    assert "-u UV_EXCLUDE_NEWER_PACKAGE" in text
    assert "-u UV_PRERELEASE" in text
    assert "-u UV_INDEX_STRATEGY" in text
    assert "-u UV_RESOLUTION" in text
    assert "-u UV_FORK_STRATEGY" in text
    assert "-u UV_TORCH_BACKEND" in text
    assert "-u UV_OFFLINE" in text


def test_installer_is_posix_and_has_no_privileged_or_system_package_mutation():
    text = INSTALLER.read_text()
    assert text.startswith("#!/bin/sh\n")
    assert not re.search(r"^\s*sudo\s", text, re.MULTILINE)
    assert not re.search(r"\b(apt|apt-get|dnf|yum|pacman|brew) install\b", text)
    # Integrity comes from the pinned SHA-256 checks, not transport flags: the
    # installer must not reintroduce a system-package or privileged mutation.
    assert 'sh -n "$installer"' in text
    assert "UV_NO_MODIFY_PATH=1" in text


def test_uv_installer_destination_overrides_cannot_hide_the_installed_uv(
    tmp_path: Path,
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    uv_source = tmp_path / "uv-source"
    (bin_dir / "uv").replace(uv_source)
    mock_installer = tmp_path / "uv-installer-fixture.sh"
    _write_executable(
        mock_installer,
        """
[ -z "${UV_INSTALL_DIR:-}" ]
[ -z "${CARGO_DIST_FORCE_INSTALL_DIR:-}" ]
[ -z "${UV_UNMANAGED_INSTALL:-}" ]
[ -z "${XDG_BIN_HOME:-}" ]
[ -z "${XDG_DATA_HOME:-}" ]
mkdir -p "$HOME/.local/bin"
cp "$UV_SOURCE" "$HOME/.local/bin/uv"
chmod +x "$HOME/.local/bin/uv"
""",
    )
    _write_executable(
        bin_dir / "curl",
        """
out=
while [ "$#" -gt 0 ]; do
  if [ "$1" = --output ]; then shift; out=$1; fi
  shift
done
cp "$MOCK_UV_INSTALLER" "$out"
""",
    )
    _write_executable(
        bin_dir / "sha256sum",
        'printf "%s  %s\\n" "504a79fd2ed0dcd47e7f04f0792cfd0871f62e24a7fe40fa8ae0f563a369f2bd" "$1"\n',
    )
    override_root = home / "unexpected-destination"
    env.update(
        {
            "UV_SOURCE": str(uv_source),
            "MOCK_UV_INSTALLER": str(mock_installer),
            "UV_INSTALL_DIR": str(override_root / "uv-install"),
            "CARGO_DIST_FORCE_INSTALL_DIR": str(override_root / "cargo-dist"),
            "UV_UNMANAGED_INSTALL": str(override_root / "unmanaged"),
            "XDG_BIN_HOME": str(override_root / "xdg-bin"),
            "XDG_DATA_HOME": str(override_root / "xdg-data"),
        }
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert (home / ".local/bin/uv").exists()
    assert not override_root.exists()
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert f"uv_path={home}/.local/bin/uv" in receipt.read_text()


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("root", "normal user"),
        ("os", "unsupported operating system"),
        ("arch", "unsupported architecture"),
        ("wsl", "WSL is not supported"),
        ("container", "containers are not supported"),
        ("os-release", "cannot read /etc/os-release"),
        ("systemd", "no usable systemd --user session"),
    ],
)
def test_preflight_refusals_happen_before_mutation(
    tmp_path: Path, kind: str, message: str
):
    env, home, root, bin_dir = _base_host(tmp_path)
    if kind == "root":
        _write_executable(bin_dir / "id", "echo 0\n")
    elif kind == "os":
        _write_executable(
            bin_dir / "uname",
            'case "$1" in -s) echo FreeBSD;; -m) echo x86_64;; esac\n',
        )
    elif kind == "arch":
        _write_executable(
            bin_dir / "uname",
            'case "$1" in -s) echo Linux;; -m) echo riscv64;; -r) echo test;; esac\n',
        )
    elif kind == "wsl":
        (root / "proc/version").write_text("Microsoft WSL2")
    elif kind == "container":
        (root / ".dockerenv").touch()
    elif kind == "os-release":
        (root / "etc/os-release").unlink()
    elif kind == "systemd":
        _write_executable(bin_dir / "systemctl", "exit 1\n")
    result = _run(env)
    assert result.returncode != 0
    assert message in result.stderr
    assert not Path(env["MUTATION_LOG"]).exists()
    assert not (home / ".alle").exists()
    assert not (home / ".local/state/alle").exists()


def test_macos_rejects_linger_before_mutation(tmp_path: Path):
    env, *_ = _base_host(tmp_path, system="Darwin", machine="arm64")
    result = _run(env, "--linger")
    assert result.returncode != 0
    assert "--linger is Linux-only" in result.stderr


@pytest.mark.parametrize(
    ("owner", "message"),
    [
        ("homebrew", "brew upgrade alle"),
        ("pipx", "pipx upgrade alle-proxy"),
        ("checkout", "checkout or virtual environment"),
        ("pip", "Python/pip owner"),
        ("uv-conflict", "PATH resolves alle"),
    ],
)
def test_existing_owner_handoffs_precede_mutation(
    tmp_path: Path, owner: str, message: str
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    alle_dir = bin_dir
    if owner == "checkout":
        alle_dir = tmp_path / "checkout/.venv/bin"
        env["PATH"] = f"{alle_dir}:{env['PATH']}"
    _write_executable(alle_dir / "alle", "exit 0\n")
    if owner == "homebrew":
        _write_executable(
            bin_dir / "brew",
            f'if [ "$1" = --prefix ]; then echo "{tmp_path}"; else exit 0; fi\n',
        )
    elif owner == "pipx":
        _write_executable(
            bin_dir / "pipx",
            'if [ "$1 $2" = "list --short" ]; then echo "alle-proxy 0.1.8"; fi\n',
        )
    elif owner == "uv-conflict":
        _mock_uv(bin_dir, home)
        # Make uv report ownership even though its expected bin dir differs.
        (home / ".local/bin").mkdir(parents=True)
        _write_executable(home / ".local/bin/alle", "exit 0\n")
    result = _run(env)
    assert result.returncode != 0
    assert message in result.stderr
    assert not Path(env["MUTATION_LOG"]).exists()
    assert not (home / ".alle").exists()
    assert not (home / ".local/state/alle").exists()


def test_package_failure_never_registers_service(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home, package_fails=True)
    result = _run(env)
    assert result.returncode != 0
    assert "no service was registered" in result.stderr
    assert Path(env["MUTATION_LOG"]).read_text().splitlines() == ["package"]
    assert not (home / ".alle").exists()
    assert not (home / ".local/state/alle").exists()


def test_package_resolution_environment_cannot_override_the_exact_release(
    tmp_path: Path,
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    env.update(
        {
            "UV_OVERRIDE": str(tmp_path / "override.txt"),
            "UV_CONSTRAINT": str(tmp_path / "constraint.txt"),
            "UV_BUILD_CONSTRAINT": str(tmp_path / "build-constraint.txt"),
            "UV_EXCLUDE": "alle-proxy",
            "UV_EXCLUDE_NEWER": "2000-01-01",
            "UV_EXCLUDE_NEWER_PACKAGE": "alle-proxy=2000-01-01",
            "UV_PRERELEASE": "allow",
            "UV_INDEX_STRATEGY": "unsafe-best-match",
            "UV_RESOLUTION": "lowest",
            "UV_FORK_STRATEGY": "fewest",
            "UV_TORCH_BACKEND": "cpu",
            "UV_OFFLINE": "1",
            "UV_NO_BINARY": "alle-proxy",
            "UV_NO_BINARY_PACKAGE": "alle-proxy",
            "UV_NO_BUILD": "1",
            "UV_NO_BUILD_PACKAGE": "alle-proxy",
            "UV_NO_BUILD_ISOLATION": "1",
        }
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert (home / ".local/bin/alle").exists()


def test_headless_verification_failure_keeps_a_reversible_receipt(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    env["HEADLESS_FAIL"] = "1"

    result = _run(env)

    assert result.returncode != 0
    assert "headless verification failed" in result.stderr
    assert (home / ".local/state/alle/bootstrap-receipt").exists()
    assert (home / ".alle/.alle-bootstrap-receipt").exists()

    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    removed = _run(env, "--uninstall")
    assert removed.returncode == 0, removed.stderr
    assert not (home / ".local/bin/alle").exists()
    assert not (home / ".alle").exists()


def test_service_failure_leaves_tool_and_prints_repair_handoff(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    env["SERVICE_FAIL"] = "1"
    result = _run(env)
    assert result.returncode != 0
    assert (home / ".local/bin/alle").exists()
    assert "service registration failed" in result.stderr
    assert "tool uninstall alle-proxy" in result.stderr
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert receipt.exists(), "a failed service install must remain reversible"
    assert "receipt_version=1" in receipt.read_text()
    assert (home / ".alle/.alle-bootstrap-receipt").exists()
    assert Path(env["MUTATION_LOG"]).read_text().splitlines() == ["package", "service"]


def test_success_and_same_release_rerun_are_package_idempotent(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    first = _run(env)
    assert first.returncode == 0, first.stderr
    assert "verified its healthy login service" in first.stdout

    # The installed absolute uv/alle shims are now on PATH. The second run
    # verifies/re-registers the idempotent service but does not mutate the tool.
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    second = _run(env)
    assert second.returncode == 0, second.stderr
    assert "leaving the tool unchanged" in second.stdout
    assert Path(env["MUTATION_LOG"]).read_text().splitlines() == [
        "package",
        "service",
        "service",
    ]
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert receipt.exists()
    assert f"state_dir={home}/.alle" in receipt.read_text()


def test_uninstall_removes_service_tool_state_and_receipt_but_keeps_uv(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"

    result = _run(env, "--uninstall")
    assert result.returncode == 0, result.stderr
    assert not (home / ".local/bin/alle").exists()
    assert not (home / ".alle").exists()
    assert not (home / ".local/state/alle/bootstrap-receipt").exists()
    assert (bin_dir / "uv").exists(), "uv itself must be retained"
    assert Path(env["MUTATION_LOG"]).read_text().splitlines() == [
        "package",
        "service",
        "stop",
        "service-uninstall",
        "stop",
        "tool-uninstall",
    ]
    assert "uv was retained" in result.stdout


def test_uninstall_purges_custom_recorded_state(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    custom = home / "private/alle-state"
    env["ALLE_HOME"] = str(custom)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    assert _run(env, "--uninstall").returncode == 0
    assert not custom.exists()


def test_custom_state_rerun_uses_receipt_when_alle_home_is_unset(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    custom = home / "private/alle-state"
    env["ALLE_HOME"] = str(custom)
    assert _run(env).returncode == 0

    env.pop("ALLE_HOME")
    rerun = _run(env)

    assert rerun.returncode == 0, rerun.stderr
    assert "leaving the tool unchanged" in rerun.stdout
    assert custom.exists()
    assert not (home / ".alle").exists()
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert f"state_dir={custom}" in receipt.read_text()
    assert Path(env["MUTATION_LOG"]).read_text().splitlines().count("package") == 1


def test_uninstall_service_failure_preserves_tool_and_state(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["UNINSTALL_SERVICE_FAIL"] = "1"
    result = _run(env, "--uninstall")
    assert result.returncode != 0
    assert "tool and state were left intact" in result.stderr
    assert (home / ".local/bin/alle").exists()
    assert (home / ".alle").exists()
    assert Path(env["MUTATION_LOG"]).read_text().splitlines()[-2:] == [
        "stop",
        "service-uninstall",
    ]


def test_uninstall_stop_failure_preserves_service_tool_and_state(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["STOP_FAIL"] = "1"

    result = _run(env, "--uninstall")

    assert result.returncode != 0
    assert (
        "login service, uv tool, state, and receipt were left intact" in result.stderr
    )
    assert Path(env["MUTATION_LOG"]).read_text().splitlines()[-1] == "stop"
    assert (home / ".local/bin/alle").exists()
    assert (home / ".alle").exists()
    assert (home / ".local/state/alle/bootstrap-receipt").exists()


def test_uninstall_does_not_require_uv_install_or_shell_update_features(
    tmp_path: Path,
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["INSTALL_HELP_FAIL"] = "1"
    env["UPDATE_SHELL_HELP_FAIL"] = "1"

    result = _run(env, "--uninstall")

    assert result.returncode == 0, result.stderr
    assert not (home / ".local/bin/alle").exists()
    assert not (home / ".alle").exists()


def test_uninstall_refuses_non_uv_owned_alle(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _write_executable(bin_dir / "alle", "exit 0\n")
    (home / ".alle").mkdir()
    result = _run(env, "--uninstall")
    assert result.returncode != 0
    assert "not owned by this uv bootstrap" in result.stderr
    assert (home / ".alle").exists()


def test_uninstall_rejects_unsafe_receipt_before_mutation(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    receipt = home / ".local/state/alle/bootstrap-receipt"
    receipt.write_text("receipt_version=1\nstate_dir=/\n")

    before = Path(env["MUTATION_LOG"]).read_text()
    result = _run(env, "--uninstall")
    assert result.returncode != 0
    assert "refusing malformed bootstrap receipt" in result.stderr
    assert Path(env["MUTATION_LOG"]).read_text() == before
    assert (home / ".local/bin/alle").exists()


@pytest.mark.parametrize("unsafe", ["//", "/tmp/..", "/./"])
def test_uninstall_rejects_paths_that_canonicalize_to_root_before_mutation(
    tmp_path: Path, unsafe: str
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    receipt = home / ".local/state/alle/bootstrap-receipt"
    receipt.write_text(
        re.sub(
            r"^state_dir=.*$", f"state_dir={unsafe}", receipt.read_text(), flags=re.M
        )
    )
    before = Path(env["MUTATION_LOG"]).read_text()

    result = _run(env, "--uninstall")

    assert result.returncode != 0
    assert "refusing" in result.stderr
    assert Path(env["MUTATION_LOG"]).read_text() == before
    assert (home / ".local/bin/alle").exists()


def test_uninstall_rejects_unknown_receipt_version_before_mutation(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    receipt = home / ".local/state/alle/bootstrap-receipt"
    receipt.write_text(
        receipt.read_text().replace("receipt_version=1", "receipt_version=2")
    )
    before = Path(env["MUTATION_LOG"]).read_text()

    result = _run(env, "--uninstall")

    assert result.returncode != 0
    assert "unsupported bootstrap receipt version" in result.stderr
    assert Path(env["MUTATION_LOG"]).read_text() == before


def test_uninstall_tool_failure_preserves_and_resumes_from_phase(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["UNINSTALL_TOOL_FAIL"] = "1"

    result = _run(env, "--uninstall")
    assert result.returncode != 0
    assert "resumable receipt were left intact" in result.stderr
    assert (home / ".alle").exists()
    receipt_dir = home / ".local/state/alle"
    assert (receipt_dir / "bootstrap-receipt").exists()
    assert "phase=tool_removing" in (receipt_dir / "uninstall-phase").read_text()

    env.pop("UNINSTALL_TOOL_FAIL")
    resumed = _run(env, "--uninstall")
    assert resumed.returncode == 0, resumed.stderr
    assert not (home / ".alle").exists()
    assert not receipt_dir.exists()


def test_uninstall_resumes_after_interruption_once_uv_removed_the_tool(
    tmp_path: Path,
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["INTERRUPT_AFTER_TOOL_REMOVE"] = "1"

    interrupted = _run(env, "--uninstall")

    assert interrupted.returncode != 0
    receipt_dir = home / ".local/state/alle"
    assert not (home / ".local/bin/alle").exists()
    assert (home / ".alle").exists()
    assert (receipt_dir / "bootstrap-receipt").exists()
    assert "phase=tool_removing" in (receipt_dir / "uninstall-phase").read_text()
    before = Path(env["MUTATION_LOG"]).read_text()

    install_attempt = _run(env)
    assert install_attempt.returncode != 0
    assert "uninstall is in progress" in install_attempt.stderr
    assert Path(env["MUTATION_LOG"]).read_text() == before

    env.pop("INTERRUPT_AFTER_TOOL_REMOVE")
    resumed = _run(env, "--uninstall")
    assert resumed.returncode == 0, resumed.stderr
    assert "resuming an interrupted bootstrap uninstall" in resumed.stdout
    assert not (home / ".alle").exists()
    assert not receipt_dir.exists()


def test_uninstall_resumes_after_state_purge_failure_removed_the_marker(
    tmp_path: Path,
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    state_dir = home / ".alle"
    _write_executable(
        bin_dir / "rm",
        """
last=
for argument in "$@"; do last=$argument; done
if [ "${FAIL_STATE_PURGE:-0}" = 1 ] && [ "$last" = "$FAIL_STATE_DIR" ]; then
  /bin/rm -f -- "$last/.alle-bootstrap-receipt"
  exit 88
fi
exec /bin/rm "$@"
""",
    )
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["FAIL_STATE_PURGE"] = "1"
    env["FAIL_STATE_DIR"] = str(state_dir)

    failed = _run(env, "--uninstall")

    assert failed.returncode != 0
    receipt_dir = home / ".local/state/alle"
    assert state_dir.exists()
    assert not (state_dir / ".alle-bootstrap-receipt").exists()
    assert (receipt_dir / "bootstrap-receipt").exists()
    assert (receipt_dir / "uninstall-phase").exists()
    assert not (home / ".local/bin/alle").exists()

    env["FAIL_STATE_PURGE"] = "0"
    resumed = _run(env, "--uninstall")
    assert resumed.returncode == 0, resumed.stderr
    assert not state_dir.exists()
    assert not receipt_dir.exists()


def test_resumed_uninstall_refuses_a_foreign_replacement_shim(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["INTERRUPT_AFTER_TOOL_REMOVE"] = "1"
    assert _run(env, "--uninstall").returncode != 0
    foreign = home / ".local/bin/alle"
    _write_executable(foreign, "exit 0\n")
    env.pop("INTERRUPT_AFTER_TOOL_REMOVE")
    env["UV_LIST_EMPTY"] = "1"

    refused = _run(env, "--uninstall")

    assert refused.returncode != 0
    assert "unowned shim remains" in refused.stderr
    assert foreign.exists()
    assert (home / ".alle").exists()
    assert (home / ".local/state/alle/bootstrap-receipt").exists()

    foreign.unlink()
    resumed = _run(env, "--uninstall")
    assert resumed.returncode == 0, resumed.stderr


def test_uninstall_rejects_tampered_phase_before_mutation(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    receipt_dir = home / ".local/state/alle"
    receipt = receipt_dir / "bootstrap-receipt"
    phase = receipt_dir / "uninstall-phase"
    phase.write_text(
        f"phase_version=1\nreceipt_path={receipt}\nstate_dir=/\nphase=tool_removing\n"
    )
    before = Path(env["MUTATION_LOG"]).read_text()

    result = _run(env, "--uninstall")

    assert result.returncode != 0
    assert "phase state does not match" in result.stderr
    assert Path(env["MUTATION_LOG"]).read_text() == before
    assert (home / ".local/bin/alle").exists()
    assert (home / ".alle").exists()


def test_uninstall_is_idempotent_after_full_removal(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    assert _run(env, "--uninstall").returncode == 0

    result = _run(env, "--uninstall")
    assert result.returncode == 0
    assert "nothing to remove" in result.stdout


def test_install_and_uninstall_support_paths_with_spaces(tmp_path: Path):
    spaced = tmp_path / "home path fixture"
    spaced.mkdir()
    env, home, _root, bin_dir = _base_host(spaced)
    _mock_uv(bin_dir, home)

    first = _run(env)
    assert first.returncode == 0, first.stderr
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    removed = _run(env, "--uninstall")

    assert removed.returncode == 0, removed.stderr
    assert not (home / ".alle").exists()


def test_off_path_standard_uv_is_reused_on_install_and_rerun(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    hidden_uv = home / ".local/bin/uv"
    hidden_uv.parent.mkdir(parents=True)
    (bin_dir / "uv").replace(hidden_uv)

    first = _run(env)
    second = _run(env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "reusing compatible uv" in first.stdout
    assert "leaving the tool unchanged" in second.stdout
    assert Path(env["MUTATION_LOG"]).read_text().splitlines().count("package") == 1


def test_custom_uv_dirs_are_recorded_and_uninstalled_after_environment_changes(
    tmp_path: Path,
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    custom_bin = home / "custom uv/bin"
    custom_tools = home / "custom uv/tools"
    env["UV_TOOL_BIN_DIR"] = str(custom_bin)
    env["UV_TOOL_DIR"] = str(custom_tools)
    env["XDG_STATE_HOME"] = str(home / "xdg-before")
    assert _run(env).returncode == 0
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert f"uv_bin_dir={custom_bin}" in receipt.read_text()
    assert not (home / "xdg-before/alle/bootstrap-receipt").exists()

    env.pop("UV_TOOL_BIN_DIR")
    env.pop("UV_TOOL_DIR")
    env["XDG_STATE_HOME"] = str(home / "xdg-after")
    result = _run(env, "--uninstall")

    assert result.returncode == 0, result.stderr
    assert not (custom_bin / "alle").exists()
    assert not (custom_tools / "alle-proxy").exists()


def test_profile_fallback_is_idempotent_and_service_sees_uv_bin(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    env["SHELL"] = "/bin/ash"
    env["SERVICE_PATH_LOG"] = str(tmp_path / "service-path")

    assert _run(env).returncode == 0
    assert _run(env).returncode == 0

    profile = (home / ".profile").read_text()
    assert profile.count("# alle bootstrap") == 1
    service_path = Path(env["SERVICE_PATH_LOG"]).read_text().strip()
    assert service_path.split(":", 1)[0] == str(home / ".local/bin")


@pytest.mark.parametrize("unsafe", ["/", "//", "/tmp/..", "/tmp"])
def test_unsafe_alle_home_is_rejected_before_package_or_service(
    tmp_path: Path, unsafe: str
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    env["ALLE_HOME"] = unsafe

    result = _run(env)

    assert result.returncode != 0
    assert "ALLE_HOME" in result.stderr
    assert not Path(env["MUTATION_LOG"]).exists()


def test_unwritable_receipt_location_fails_before_package_or_service(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    receipt_dir = home / ".local/state/alle"
    receipt_dir.parent.mkdir(parents=True)
    receipt_dir.write_text("not a directory")

    result = _run(env)

    assert result.returncode != 0
    assert "receipt directory" in result.stderr
    assert not Path(env["MUTATION_LOG"]).exists()


def test_home_with_newline_is_rejected_before_mutation(tmp_path: Path):
    newline_root = tmp_path / "bad\nhome-root"
    newline_root.mkdir()
    env, _home, _root, bin_dir = _base_host(newline_root)
    _mock_uv(bin_dir, Path(env["HOME"]))

    result = _run(env)

    assert result.returncode != 0
    assert "HOME must be an absolute path without newlines" in result.stderr
    assert not Path(env["MUTATION_LOG"]).exists()


@pytest.mark.parametrize(("prestate", "expect_disable"), [("yes", False), ("no", True)])
def test_linger_is_disabled_only_when_bootstrap_enabled_it(
    tmp_path: Path, prestate: str, expect_disable: bool
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    _mock_loginctl(bin_dir)
    state = tmp_path / "linger-state"
    state.write_text(prestate)
    env["LINGER_STATE_FILE"] = str(state)

    assert _run(env, "--linger").returncode == 0
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert f"linger_changed={int(expect_disable)}" in receipt.read_text()
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    result = _run(env, "--uninstall")

    assert result.returncode == 0, result.stderr
    mutations = Path(env["MUTATION_LOG"]).read_text().splitlines()
    assert ("linger-disable" in mutations) is expect_disable
    if expect_disable:
        assert mutations.index("linger-disable") < mutations.index("tool-uninstall")
        assert state.read_text().strip() == "no"
    else:
        assert state.read_text().strip() == "yes"


def test_failed_linger_install_does_not_claim_preexisting_linger(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    _mock_loginctl(bin_dir)
    state = tmp_path / "linger-state"
    state.write_text("no")
    env["LINGER_STATE_FILE"] = str(state)
    env["SERVICE_FAIL"] = "1"

    result = _run(env, "--linger")

    assert result.returncode != 0
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert "linger_changed=0" in receipt.read_text()
    assert state.read_text().strip() == "no"


def test_failed_linger_install_keeps_ownership_when_post_query_is_unknown(
    tmp_path: Path,
):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    _mock_loginctl(bin_dir)
    state = tmp_path / "linger-state"
    state.write_text("no")
    env.update(
        {
            "LINGER_STATE_FILE": str(state),
            "SERVICE_FAIL": "1",
            "ENABLE_LINGER_BEFORE_FAIL": "1",
            "LINGER_QUERY_UNKNOWN_WHEN_YES": "1",
        }
    )

    result = _run(env, "--linger")

    assert result.returncode != 0
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert "linger_changed=1" in receipt.read_text()
    assert state.read_text().strip() == "yes"


def test_macos_helper_blocks_uninstall_before_any_mutation(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path, system="Darwin", machine="arm64")
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["HELPER_INSTALLED"] = "true"
    before = Path(env["MUTATION_LOG"]).read_text()

    result = _run(env, "--uninstall")

    assert result.returncode != 0
    assert "sudo" in result.stderr and "helper uninstall" in result.stderr
    assert Path(env["MUTATION_LOG"]).read_text() == before


def test_macos_uninstall_fails_closed_on_unknown_helper_status(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path, system="Darwin", machine="arm64")
    _mock_uv(bin_dir, home)
    assert _run(env).returncode == 0
    env["PATH"] = f"{home}/.local/bin:{bin_dir}:/usr/bin:/bin"
    env["HELPER_STATUS_MALFORMED"] = "true"
    before = Path(env["MUTATION_LOG"]).read_text()

    result = _run(env, "--uninstall")

    assert result.returncode != 0
    assert "could not determine whether the privileged helper" in result.stderr
    assert Path(env["MUTATION_LOG"]).read_text() == before


def test_custom_state_is_canonical_for_service_and_macos_helper_guard(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path, system="Darwin", machine="arm64")
    _mock_uv(bin_dir, home)
    real_state = home / "private/alle-state"
    real_state.mkdir(parents=True)
    linked_state = home / "alle-state-link"
    linked_state.symlink_to(real_state, target_is_directory=True)
    env["ALLE_HOME"] = str(linked_state)
    env["SERVICE_HOME_LOG"] = str(tmp_path / "service-home")
    env["HELPER_HOME_LOG"] = str(tmp_path / "helper-home")

    assert _run(env).returncode == 0
    assert Path(env["SERVICE_HOME_LOG"]).read_text().strip() == str(real_state)
    receipt = home / ".local/state/alle/bootstrap-receipt"
    assert f"state_dir={real_state}" in receipt.read_text()

    env.pop("ALLE_HOME")
    env["HELPER_INSTALLED"] = "true"
    result = _run(env, "--uninstall")

    assert result.returncode != 0
    assert "helper uninstall" in result.stderr
    assert Path(env["HELPER_HOME_LOG"]).read_text().strip() == str(real_state)
    assert (home / ".local/bin/alle").exists()
    assert real_state.exists()


def test_hidden_pipx_and_foreign_uv_bin_owners_are_rejected(tmp_path: Path):
    env, home, _root, bin_dir = _base_host(tmp_path)
    _mock_uv(bin_dir, home)
    _write_executable(
        bin_dir / "pipx",
        'if [ "$1 $2" = "list --short" ]; then echo "alle-proxy 0.1.8"; fi\n',
    )
    result = _run(env)
    assert result.returncode != 0
    assert "owned by pipx" in result.stderr
    assert not Path(env["MUTATION_LOG"]).exists()

    (bin_dir / "pipx").unlink()
    foreign = home / ".local/bin/alle"
    _write_executable(foreign, "exit 0\n")
    env["UV_LIST_EMPTY"] = "1"
    result = _run(env)
    assert result.returncode != 0
    assert "foreign alle shim" in result.stderr
    assert not Path(env["MUTATION_LOG"]).exists()


def test_download_checksum_failure_executes_nothing(tmp_path: Path):
    env, _home, _root, bin_dir = _base_host(tmp_path)
    # No uv on PATH. curl supplies syntactically valid but unauthentic bytes.
    _write_executable(
        bin_dir / "curl",
        'while [ "$#" -gt 0 ]; do if [ "$1" = --output ]; then shift; out=$1; fi; shift; done\n'
        'printf "#!/bin/sh\\necho executed >> \\$MUTATION_LOG\\n" > "$out"\n',
    )
    result = _run(env)
    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stderr
    assert not Path(env["MUTATION_LOG"]).exists()


def test_script_sha_can_be_published_without_rewriting():
    digest = hashlib.sha256(INSTALLER.read_bytes()).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
