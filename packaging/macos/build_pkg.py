#!/usr/bin/env python3
"""Build a hermetic macOS .pkg installer for alle.

Ships a self-contained Alle.app (native tray + bundled core + pinned sing-box)
to /Applications. The pkg is hermetic and user-level: no curl|sh, no root
helper, no system login LaunchAgent — everything the app needs lives in the
bundle, and its state lives under ~/Library/Application Support/Alle. The TUN
helper is an opt-in the tray installs (with an admin prompt) on first use; run
the bundled uninstall.sh to remove everything.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import sys
import tempfile
from pathlib import Path

# Make build_app importable whether this runs as a script (packaging/macos is
# sys.path[0]) or is imported by a test (different sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_app
from build_app import DIST, BuildError, _sha256, run

PKG_SCRIPTS = build_app.ROOT / "packaging" / "macos" / "pkg" / "scripts"
APP_NAME = build_app.APP_NAME
# The receipt identifier is the app's bundle id: one install, one receipt.
IDENTIFIER = build_app.BUNDLE_ID


def pkg_name(version: str, arch: str) -> str:
    return f"Alle-{version}-macos-{arch}.pkg"


def build_pkg(out: Path, version: str, arch: str) -> Path:
    pkg = out / pkg_name(version, arch)
    if pkg.exists():
        pkg.unlink()
    with tempfile.TemporaryDirectory(prefix="alle-pkg-build-") as base_str:
        base = Path(base_str)
        # Payload root holds Alle.app directly; install-location /Applications
        # lands it at /Applications/Alle.app. (install-location / is refused by
        # modern macOS — / is the read-only Signed System Volume.) The scripts
        # dir is a sibling of the payload root so pkgbuild does not ship it as
        # payload content.
        staging = base / "root"
        staging.mkdir()
        app = build_app.build_bundled_app(
            out=staging, version=version, arch=arch, configuration="release"
        )
        if app.name != APP_NAME:
            raise BuildError(f"unexpected app name: {app.name}")

        scripts = base / "scripts"
        scripts.mkdir()
        postinstall = PKG_SCRIPTS / "postinstall"
        if not postinstall.exists():
            raise BuildError(f"missing postinstall script: {postinstall}")
        shutil.copy2(postinstall, scripts / "postinstall")
        build_app.chmod_exec(scripts / "postinstall")
        # Strip extended attributes so pkgbuild doesn't emit ._ AppleDouble
        # sidecars into the payload/scripts.
        run(["xattr", "-cr", str(staging)])
        run(["xattr", "-cr", str(scripts)])

        run(
            [
                "pkgbuild",
                "--root",
                str(staging),
                "--scripts",
                str(scripts),
                "--identifier",
                IDENTIFIER,
                "--version",
                version,
                "--install-location",
                "/Applications",
                str(pkg),
            ]
        )
    (pkg.with_suffix(pkg.suffix + ".sha256")).write_text(
        f"{_sha256(pkg)}  {pkg.name}\n"
    )
    # Structural smoke: expanding a flat pkg fails if it is malformed. pkgutil
    # refuses to expand into an existing dir, so target a fresh subdir.
    with tempfile.TemporaryDirectory(prefix="alle-pkg-expand-") as expand_str:
        run(["pkgutil", "--expand", str(pkg), str(Path(expand_str) / "expanded")])
    return pkg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=build_app.read_version())
    parser.add_argument("--output", type=Path, default=DIST)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if platform.system() != "Darwin":
        raise SystemExit("macOS pkg packaging must run on macOS")
    arch = build_app.normalize_arch()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    pkg = build_pkg(out, args.version, arch)
    print(f"built {pkg}")


if __name__ == "__main__":
    main()
