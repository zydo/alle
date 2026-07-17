"""Release-artifact contents: the sdist/wheel ship exactly what they should.

Regression for the P1 'constrain package contents and complete notices' gate:

* local-only notes (roadmaps under ``.localonly/``) never leak into a release
  artifact (they used to — Hatchling reads ``.gitignore`` but not
  ``.git/info/exclude``);
* the dashboard screenshot stays in the sdist (so it renders on the PyPI
  project page via a relative README path) but out of the installed wheel (the
  Web UI never serves it);
* ``THIRD_PARTY_NOTICES.md`` ships in the wheel's ``dist-info/licenses/``;
* every Web UI asset the server can serve is present in the wheel;
* every relative README image ships in the sdist, so PyPI can render it.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src" / "alle" / "assets"
UV = shutil.which("uv") or ""

pytestmark = pytest.mark.skipif(not UV, reason="uv not installed")


@pytest.fixture(scope="module")
def built():
    """Build sdist + wheel once into a throwaway dir; return their paths."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="alle-dist-"))
    subprocess.run(
        [UV, "build", "--out-dir", str(tmp)], cwd=ROOT, check=True, capture_output=True
    )
    sdist = next(tmp.glob("*.tar.gz"))
    wheel = next(tmp.glob("*.whl"))
    yield sdist, wheel
    shutil.rmtree(tmp, ignore_errors=True)


def _sdist_names(sdist: Path) -> set[str]:
    with tarfile.open(sdist) as tf:
        return {m.name for m in tf.getmembers()}


def _wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as zf:
        return set(zf.namelist())


def test_local_only_notes_do_not_leak_into_artifacts(built):
    # Local-only notes (roadmaps live under .localonly/ as of 2026-07-14) must
    # stay out of both artifacts — check the directory and the old filename
    # pattern, since Hatchling reads .gitignore but not .git/info/exclude.
    sdist, wheel = built
    for name in _sdist_names(sdist) | _wheel_names(wheel):
        upper = name.upper()
        assert "ROADMAP" not in upper, f"roadmap leaked into artifact: {name}"
        assert ".LOCALONLY" not in upper, (
            f"local-only notes leaked into artifact: {name}"
        )


def test_screenshot_in_sdist_not_wheel(built):
    sdist, wheel = built
    sdist_names = _sdist_names(sdist)
    wheel_names = _wheel_names(wheel)
    assert any(n.endswith("webui.png") for n in sdist_names), (
        "webui.png must ship in the sdist so it renders on the PyPI project page"
    )
    assert not any(n.endswith("webui.png") for n in wheel_names), (
        "webui.png must not ship in the wheel — the Web UI never serves it"
    )


def test_third_party_notices_ships_in_wheel_and_sdist(built):
    sdist, wheel = built
    sdist_names = _sdist_names(sdist)
    wheel_names = _wheel_names(wheel)
    assert any(n.endswith("THIRD_PARTY_NOTICES.md") for n in sdist_names)
    assert any("licenses/THIRD_PARTY_NOTICES" in n for n in wheel_names), (
        "THIRD_PARTY_NOTICES.md must be in the wheel's dist-info/licenses/"
    )


def test_all_served_assets_present_in_wheel(built):
    _, wheel = built
    wheel_names = _wheel_names(wheel)
    served = [
        p.name
        for p in ASSETS.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.name != "webui.png"
    ]
    missing = [
        name
        for name in served
        if not any(n.endswith(f"assets/{name}") for n in wheel_names)
    ]
    assert not missing, f"served Web UI assets missing from wheel: {missing}"


def test_only_safe_daemon_entrypoint_is_shipped(built):
    """The supported foreground path owns PID markers, API, signals, and
    children; the old direct ``alled`` function entry point bypassed them."""
    _, wheel = built
    with zipfile.ZipFile(wheel) as archive:
        entry_points = next(
            name for name in archive.namelist() if name.endswith("entry_points.txt")
        )
        text = archive.read(entry_points).decode()
    assert "alle = alle.cli:main" in text
    assert "alled" not in text


def test_readme_images_render_on_pypi():
    """PyPI renders README images from absolute URLs (raw.githubusercontent); a
    relative src would not render, now that screenshots aren't shipped in the
    sdist. Guard against accidentally reintroducing a relative image path."""
    readme = (ROOT / "README.md").read_text()
    rel_srcs = [
        s
        for s in re.findall(r'src="([^"]+)"', readme)
        if not s.startswith(("http://", "https://"))
    ]
    assert not rel_srcs, f"relative README image srcs won't render on PyPI: {rel_srcs}"
