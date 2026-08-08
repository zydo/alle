# PyInstaller spec for the bundled macOS alle core.
# Invoked by packaging/macos/build_app.py from the repository root.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

ROOT = Path(SPECPATH).parents[2]
block_cipher = None

# The Web UI is not part of this bundle. The macOS app renders every screen
# natively and offers no path to a browser UI, so shipping `alle/assets` would
# add a second, unreachable interface to the payload — and one that could be
# reached by a determined user pointing a browser at the loopback API, which is
# exactly the "three overlapping surfaces" problem the native app exists to end.
# The assets stay in the wheel for the CLI, Docker, and Linux surfaces.
def _without_web_assets(entries):
    kept = []
    for source, destination in entries:
        parts = Path(destination).parts
        if len(parts) >= 2 and parts[0] == "alle" and parts[1] == "assets":
            continue
        kept.append((source, destination))
    return kept


datas = _without_web_assets(collect_data_files("alle")) + copy_metadata("alle-proxy")

a = Analysis(
    [str(ROOT / "src" / "alle" / "__main__.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["alle.tray", "alle.companion", "rumps", "PyObjC"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="alle",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="alle",
)
