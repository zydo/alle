#!/usr/bin/env python3
"""Build a sideloadable, self-contained macOS Alle.app bundle.

The bundle carries the native tray, the frozen Python core, and the pinned
sing-box. `build_pkg.py` wraps the same bundle into the shipping `.pkg`.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging" / "macos"
DIST = ROOT / "dist" / "macos"
BUNDLE_ID = "io.github.zydo.alle"
APP_NAME = "Alle.app"


class BuildError(RuntimeError):
    pass


def run(cmd: list[str], *, cwd: Path = ROOT) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def read_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    return tomllib.loads(pyproject.read_text())["project"]["version"]


def normalize_arch(machine: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    aliases = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "amd64",
        "amd64": "amd64",
    }
    try:
        return aliases[machine]
    except KeyError as e:
        raise BuildError(f"unsupported macOS architecture: {machine}") from e


def singbox_key(arch: str) -> str:
    return f"darwin-{arch}"


def render_template(name: str, **values: str) -> str:
    return (PACKAGING / name).read_text().format(**values)


def chmod_exec(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def app_paths(out: Path) -> dict[str, Path]:
    app = out / APP_NAME
    contents = app / "Contents"
    resources = contents / "Resources"
    return {
        "app": app,
        "contents": contents,
        "macos": contents / "MacOS",
        "resources": resources,
        "bin": resources / "bin",
        "core": resources / "alle-core",
        "singbox": resources / "sing-box",
    }


def build_swift(configuration: str) -> Path:
    run(["swift", "build", "--package-path", "macos/Alle", "-c", configuration])
    return ROOT / "macos" / "Alle" / ".build" / configuration / "Alle"


def build_pyinstaller(work: Path) -> Path:
    spec = PACKAGING / "pyinstaller" / "alle.spec"
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(work / "pyinstaller-dist"),
            "--workpath",
            str(work / "pyinstaller-work"),
            str(spec),
        ]
    )
    core = work / "pyinstaller-dist" / "alle"
    if not (core / "alle").exists():
        raise BuildError(f"PyInstaller did not produce {core / 'alle'}")
    return core


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def embed_singbox(dest: Path, key: str) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from alle.constants import SINGBOX_SHA256, SINGBOX_VERSION

    expected = SINGBOX_SHA256[key]
    asset = f"sing-box-{SINGBOX_VERSION}-{key}.tar.gz"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{SINGBOX_VERSION}/{asset}"
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / asset
        print(f"downloading {url}", flush=True)
        urllib.request.urlretrieve(url, archive)  # noqa: S310 - pinned upstream HTTPS asset
        with tarfile.open(archive) as tf:
            binary_member = next(
                (m for m in tf.getmembers() if m.name.endswith("/sing-box")), None
            )
            license_member = next(
                (m for m in tf.getmembers() if m.name.endswith("/LICENSE")), None
            )
            if binary_member is None or not binary_member.isfile():
                raise BuildError(f"{asset} did not contain sing-box")
            src = tf.extractfile(binary_member)
            if src is None:
                raise BuildError(f"could not read sing-box from {asset}")
            binary = dest / "sing-box"
            with binary.open("wb") as out:
                shutil.copyfileobj(src, out)
            chmod_exec(binary)
            got = _sha256(binary)
            if got != expected:
                raise BuildError(
                    f"sing-box checksum mismatch for {key}: expected {expected}, got {got}"
                )
            if license_member is not None:
                lic = tf.extractfile(license_member)
                if lic is not None:
                    (dest / "LICENSE").write_bytes(lic.read())


def copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def _render_svg(svg: Path, png: Path, size: int) -> None:
    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        run([rsvg, "-w", str(size), "-h", str(size), "-o", str(png), str(svg)])
        return
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        run(
            [
                magick,
                "-background",
                "none",
                str(svg),
                "-resize",
                f"{size}x{size}",
                str(png),
            ]
        )
        return
    raise BuildError(
        "could not render the app icon: install librsvg (`brew install librsvg`) "
        "or ImageMagick (`brew install imagemagick`)"
    )


def copy_tray_status_icons(resources: Path) -> None:
    """Copy the menu-bar status glyphs (PDF) into the app bundle's Resources.

    The Swift tray loads these via Bundle.main (Contents/Resources) at runtime
    and renders them as template images. PDFs are committed alongside their SVG
    sources under the SPM target's Resources dir.
    """
    src = ROOT / "macos" / "Alle" / "Sources" / "Alle" / "Resources"
    icons = sorted(src.glob("status-*.pdf"))
    if not icons:
        raise BuildError(f"no status-*.pdf tray icons found under {src}")
    for icon in icons:
        shutil.copy2(icon, resources / icon.name)


def build_app_icon(resources: Path) -> None:
    """Convert the in-repo SVG mark into a macOS .icns app icon."""
    svg = ROOT / "src" / "alle" / "assets" / "icon.svg"
    if not svg.exists():
        raise BuildError(f"missing app icon source: {svg}")
    with tempfile.TemporaryDirectory(prefix="alle-iconset-") as td:
        iconset = Path(td) / "AppIcon.iconset"
        iconset.mkdir()
        sizes = [
            ("icon_16x16.png", 16),
            ("icon_16x16@2x.png", 32),
            ("icon_32x32.png", 32),
            ("icon_32x32@2x.png", 64),
            ("icon_128x128.png", 128),
            ("icon_128x128@2x.png", 256),
            ("icon_256x256.png", 256),
            ("icon_256x256@2x.png", 512),
            ("icon_512x512.png", 512),
            ("icon_512x512@2x.png", 1024),
        ]
        for name, size in sizes:
            _render_svg(svg, iconset / name, size)
        run(
            [
                "iconutil",
                "-c",
                "icns",
                "-o",
                str(resources / "AppIcon.icns"),
                str(iconset),
            ]
        )


def _write_tray_skeleton(paths: dict, version: str, swift_binary: Path) -> None:
    """The shell of Alle.app: Info.plist, app icon, menu-bar status glyphs, the
    native tray executable, README, uninstall.sh, and third-party notices. The
    bundled build then adds core/sing-box/wrapper on top."""
    (paths["contents"] / "Info.plist").write_text(
        render_template("Info.plist.template", bundle_id=BUNDLE_ID, version=version)
    )
    build_app_icon(paths["resources"])
    copy_tray_status_icons(paths["resources"])
    shutil.copy2(swift_binary, paths["macos"] / "Alle")
    chmod_exec(paths["macos"] / "Alle")
    (paths["resources"] / "README.txt").write_text(
        (PACKAGING / "README.txt.template").read_text()
    )
    uninstall = paths["resources"] / "uninstall.sh"
    uninstall.write_text((PACKAGING / "uninstall.sh.template").read_text())
    chmod_exec(uninstall)
    notices = ROOT / "THIRD_PARTY_NOTICES.md"
    if notices.exists():
        shutil.copy2(notices, paths["resources"] / "THIRD_PARTY_NOTICES.md")


def construct_app(
    *,
    out: Path,
    version: str,
    swift_binary: Path,
    core_dir: Path,
    arch: str,
    embed_engine: bool,
) -> Path:
    paths = app_paths(out)
    if paths["app"].exists():
        shutil.rmtree(paths["app"])
    for key in ("macos", "bin", "resources"):
        paths[key].mkdir(parents=True, exist_ok=True)

    _write_tray_skeleton(paths, version, swift_binary)
    copy_tree(core_dir, paths["core"])
    wrapper = paths["bin"] / "alle"
    wrapper.write_text((PACKAGING / "alle-wrapper.sh.template").read_text())
    chmod_exec(wrapper)
    if embed_engine:
        embed_singbox(paths["singbox"], singbox_key(arch))
    return paths["app"]


def sign_path(path: Path, identity: str) -> None:
    run(["codesign", "--force", "--sign", identity, "--timestamp=none", str(path)])


def sign_app(app: Path, identity: str) -> None:
    # Sign inside-out, never letting --deep reach the pinned sing-box. Its exact
    # bytes are SHA-256-verified at runtime (alle.singbox); re-signing rewrites
    # them, so the daemon would reject the bundled copy as "not the pinned
    # sing-box" and stay degraded. So:
    #  - sign the PyInstaller launcher. --deep only recurses into nested
    #    *bundles*, and alle-core/ is a plain directory, so this signs the
    #    launcher itself and any framework bundle beside it — it stays within
    #    Resources/alle-core/ and never touches sibling Resources/sing-box/.
    #    The dylibs PyInstaller collects under _internal/ keep the ad-hoc
    #    signatures PyInstaller gave them;
    #  - sign the native tray;
    #  - seal the bundle root WITHOUT --deep (--deep would re-walk and re-sign
    #    sing-box too). sing-box is left with its original linker-signed bytes.
    launcher = app / "Contents" / "Resources" / "alle-core" / "alle"
    if launcher.exists():
        run(
            [
                "codesign",
                "--force",
                "--sign",
                identity,
                "--timestamp=none",
                "--deep",
                str(launcher),
            ]
        )
    sign_path(app / "Contents" / "MacOS" / "Alle", identity)
    run(["codesign", "--force", "--sign", identity, "--timestamp=none", str(app)])
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])


def build_bundled_app(
    *,
    out: Path,
    version: str,
    arch: str,
    configuration: str = "release",
    embed_engine: bool = True,
    sign_identity: str = "-",
) -> Path:
    """Build the self-contained Alle.app (tray + bundled core + sing-box).

    Shared by the standalone `build_app.py` entrypoint and the pkg builder
    (`build_pkg.py`): build the Swift tray, freeze the Python core, embed the
    pinned sing-box, assemble + ad-hoc sign the bundle.
    """
    with tempfile.TemporaryDirectory(prefix="alle-macos-build-") as td:
        work = Path(td)
        swift_binary = build_swift(configuration)
        core = build_pyinstaller(work)
        app = construct_app(
            out=out,
            version=version,
            swift_binary=swift_binary,
            core_dir=core,
            arch=arch,
            embed_engine=embed_engine,
        )
    sign_app(app, sign_identity)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=read_version())
    parser.add_argument(
        "--configuration", default="release", choices=["debug", "release"]
    )
    parser.add_argument("--sign-identity", default="-")
    parser.add_argument("--output", type=Path, default=DIST)
    parser.add_argument("--no-embed-sing-box", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if platform.system() != "Darwin":
        raise SystemExit("macOS app packaging must run on macOS")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    app = build_bundled_app(
        out=out,
        version=args.version,
        arch=normalize_arch(),
        configuration=args.configuration,
        embed_engine=not args.no_embed_sing_box,
        sign_identity=args.sign_identity,
    )
    print(f"built {app}")


if __name__ == "__main__":
    main()
