from __future__ import annotations

import importlib.util
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

# The packaging scripts import tomllib (3.11+); they only ever run on a macOS
# dev machine — the test-macos job still exercises them on 3.14 — so skip the
# module wholesale on older interpreters instead of carrying a fallback the
# scripts would never use.
if sys.version_info < (3, 11):
    pytest.skip("macOS packaging scripts need Python 3.11+", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "macos_build_app", ROOT / "packaging" / "macos" / "build_app.py"
)
build_app = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(build_app)

PKG_SPEC = importlib.util.spec_from_file_location(
    "macos_build_pkg", ROOT / "packaging" / "macos" / "build_pkg.py"
)
build_pkg = importlib.util.module_from_spec(PKG_SPEC)
assert PKG_SPEC.loader is not None
PKG_SPEC.loader.exec_module(build_pkg)


def test_normalize_arch():
    assert build_app.normalize_arch("arm64") == "arm64"
    assert build_app.normalize_arch("aarch64") == "arm64"
    assert build_app.normalize_arch("x86_64") == "amd64"


def test_info_plist_template_has_menu_bar_app_shape():
    raw = build_app.render_template(
        "Info.plist.template",
        bundle_id="com.example.Alle",
        version="1.2.3",
    )
    plist = plistlib.loads(raw.encode())

    assert plist["CFBundleIdentifier"] == "com.example.Alle"
    assert plist["CFBundleExecutable"] == "Alle"
    assert plist["CFBundleShortVersionString"] == "1.2.3"
    assert plist["CFBundleIconFile"] == "AppIcon"
    assert plist["LSMinimumSystemVersion"] == "13.0"
    assert plist["LSUIElement"] is True


def test_wrapper_points_at_bundled_core_and_exports_service_env():
    wrapper = (build_app.PACKAGING / "alle-wrapper.sh.template").read_text()

    assert 'CORE="$RESOURCES/alle-core/alle"' in wrapper
    assert 'export ALLE_EXECUTABLE="$SELF"' in wrapper
    assert 'export ALLE_SERVICE_OWNER="macos-app"' in wrapper
    assert 'export ALLE_SERVICE_PREFIX="$RESOURCES"' in wrapper
    assert 'export ALLE_SINGBOX="$SINGBOX"' in wrapper
    assert 'exec "$CORE" "$@"' in wrapper


def test_packaging_lives_outside_python_package():
    assert "src/alle" not in build_app.PACKAGING.relative_to(build_app.ROOT).parts


def test_tray_status_icons_present():
    # The Swift tray loads status-{stopped,running,tun}.pdf as template glyphs.
    # build_app.copy_tray_status_icons copies these into the .app, so the source
    # PDFs must exist alongside their SVG sources under the SPM target.
    resources = build_app.ROOT / "macos" / "Alle" / "Sources" / "Alle" / "Resources"
    for kind in ("stopped", "running", "tun"):
        assert (resources / f"status-{kind}.pdf").is_file(), (
            f"missing status-{kind}.pdf"
        )
        assert (resources / f"status-{kind}.svg").is_file(), (
            f"missing status-{kind}.svg"
        )


def test_postinstall_is_valid_bash_and_executable():
    postinstall = build_pkg.PKG_SCRIPTS / "postinstall"
    assert postinstall.is_file(), "missing postinstall script"
    assert os.access(postinstall, os.X_OK), "postinstall must be executable"
    result = subprocess.run(
        ["bash", "-n", str(postinstall)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"postinstall syntax error: {result.stderr}"


def test_pkg_name_is_deterministic():
    assert build_pkg.pkg_name("0.1.13", "arm64") == "Alle-0.1.13-macos-arm64.pkg"


def test_postinstall_is_hermetic_and_just_launches():
    # The hermetic pkg carries everything in the app; postinstall must NOT reach
    # out to curl|sh, install a root helper, or write a system login item.
    text = (build_pkg.PKG_SCRIPTS / "postinstall").read_text()
    assert "/Applications/Alle.app" in text
    assert "open" in text  # launch the tray for the installing user
    assert "install.sh" not in text
    assert "helper install" not in text
    assert "/Library/LaunchAgents" not in text
    assert "/Library/LaunchDaemons" not in text


def test_uninstall_script_is_valid_bash_and_covers_everything():
    uninstall = build_app.PACKAGING / "uninstall.sh.template"
    assert uninstall.is_file(), "missing uninstall.sh template"
    text = uninstall.read_text()
    # Tears down the hermetic install: daemon, user login items, opt-in helper,
    # app-scoped state, and the app itself.
    for needle in (
        "daemon uninstall",
        "Library/LaunchAgents",
        "helper uninstall",
        "Application Support/Alle",
        "/Applications/Alle.app",
    ):
        assert needle in text, f"uninstall.sh missing {needle!r}"
    result = subprocess.run(
        ["bash", "-n", str(uninstall)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"uninstall.sh syntax error: {result.stderr}"


def test_wrapper_is_valid_posix_sh():
    # Every CLI invocation in the app bundle goes through this wrapper, and it
    # is #!/bin/sh — check it against sh, not bash, so a bashism cannot ship.
    wrapper = build_app.PACKAGING / "alle-wrapper.sh.template"
    result = subprocess.run(["sh", "-n", str(wrapper)], capture_output=True, text=True)
    assert result.returncode == 0, f"alle-wrapper.sh syntax error: {result.stderr}"


def test_wrapper_scopes_state_to_app_data_dir():
    # The hermetic app keeps its state out of the read-only bundle and out of the
    # CLI install's ~/.alle, so the two can coexist.
    wrapper = (build_app.PACKAGING / "alle-wrapper.sh.template").read_text()
    assert "ALLE_HOME" in wrapper
    assert "Library/Application Support/Alle" in wrapper
