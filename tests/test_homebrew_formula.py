"""The Homebrew formula's headless product boundary and its release updater.

Two things are guarded here:

* the formula and every other native channel consume the same genuinely
  headless base wheel — no channel-specific surgery can drift; and
* `scripts/update-homebrew-formula.py` rewrites only the formula's own
  `url`/`sha256`, never the pinned resource blocks, and is idempotent.

The formula itself is exercised by `brew test` on clean runners in the tap; this
suite is the in-repo artifact assertion that keeps the two from drifting.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FORMULA = ROOT / "packaging" / "homebrew" / "alle.rb"
UPDATER = ROOT / "scripts" / "update-homebrew-formula.py"
UV = shutil.which("uv") or ""

GUI_MODULES = {"tray.py", "companion.py"}
GUI_SCRIPT = "alle-tray"


@pytest.fixture(scope="module")
def formula_text() -> str:
    return FORMULA.read_text()


def _load_updater():
    spec = importlib.util.spec_from_file_location("update_homebrew_formula", UPDATER)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- formula structure ------------------------------------------------------


def test_formula_has_native_service_and_caveats(formula_text):
    # Portable brew supervision (launchd on macOS, systemd --user on Linux) of
    # the stable `alle applier` shim, plus a caveat steering to brew services.
    assert re.search(r"service do\b", formula_text)
    assert 'run [opt_bin/"alle", "applier"]' in formula_text
    assert re.search(r'ALLE_SERVICE:\s+"1"', formula_text)
    assert re.search(r'ALLE_SERVICE_OWNER:\s+"homebrew"', formula_text)
    assert re.search(r"ALLE_SERVICE_PREFIX:\s+opt_prefix\.to_s", formula_text)
    assert re.search(r"PATH:\s+std_service_path_env", formula_text)
    assert "keep_alive true" in formula_text
    assert "def caveats" in formula_text
    assert "brew services start alle" in formula_text
    # The caveat must actively steer away from the competing user unit.
    assert "alle daemon install" in formula_text


def test_formula_relies_on_the_shared_headless_wheel(formula_text):
    # No channel-specific deletion: the wheel itself is the product boundary.
    install = formula_text.split("def install", 1)[1].split("service do", 1)[0]
    assert "virtualenv_install_with_resources" in install
    assert "rm " not in install
    # The formula test still proves the installed keg is headless.
    test_block = formula_text.split("test do", 1)[1]
    assert 'refute_path_exists "#{site}/tray.py"' in test_block
    assert 'refute_path_exists "#{site}/companion.py"' in test_block
    assert 'refute_path_exists bin/"alle-tray"' in test_block


def test_formula_never_installs_the_tray_extra(formula_text):
    # No `[tray]` extra and no rumps resource in the directives — the header
    # comment names them only to explain the boundary, so ignore comment lines.
    directives = "\n".join(
        line for line in formula_text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "[tray]" not in directives
    assert "rumps" not in directives.lower()
    assert 'resource "rumps"' not in formula_text


def test_pinned_resources_match_the_lockfile(formula_text):
    """Every `resource` sha256 in the formula matches the sdist hash uv.lock
    records, so the brew build resolves the same dependency bytes as the wheel.
    """
    lock = (ROOT / "uv.lock").read_text()
    resources = re.findall(
        r'resource "([^"]+)" do\n\s*url "[^"]+"\n\s*sha256 "([0-9a-f]{64})"',
        formula_text,
    )
    assert {name for name, _ in resources} == {"packaging", "pyyaml", "pycountry"}
    for name, sha in resources:
        block = re.search(
            rf'name = "{name}"\n.*?sdist = \{{[^}}]*?hash = "sha256:([0-9a-f]{{64}})"',
            lock,
            re.DOTALL,
        )
        assert block, f"{name} not found in uv.lock"
        assert block.group(1) == sha, f"{name} sha256 drifted from uv.lock"


# ---- artifact assertion: shared base wheel is headless ----------------------


@pytest.mark.skipif(not UV, reason="uv not installed")
def test_base_wheel_is_headless(formula_text):
    """The uv, pipx, and Homebrew channels share one headless artifact."""
    tmp = Path(tempfile.mkdtemp(prefix="alle-brew-"))
    try:
        subprocess.run(
            [UV, "build", "--wheel", "--out-dir", str(tmp)],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        wheel = next(tmp.glob("*.whl"))
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
            entry_points = zf.read(
                next(n for n in names if n.endswith("entry_points.txt"))
            ).decode()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    top_modules = {Path(n).name for n in names if re.fullmatch(r"alle/[^/]+\.py", n)}
    assert GUI_MODULES.isdisjoint(top_modules)
    assert GUI_SCRIPT not in entry_points
    assert "rumps" not in entry_points.lower()


# ---- the release updater ----------------------------------------------------


def test_updater_rewrites_only_the_source_url_and_sha(formula_text):
    mod = _load_updater()
    url = "https://files.pythonhosted.org/packages/ab/cd/alle_proxy-0.1.9.tar.gz"
    sha = "a" * 64
    out = mod.rewrite_source(formula_text, url, sha)

    # The package's own url/sha are updated...
    assert f'url "{url}"' in out
    assert f'sha256 "{sha}"' in out
    # ...and the resource pins are untouched.
    assert "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f" in out
    assert "5b6027d453fcd6060112b951dd010f01f168b51b4bf8a1f1fc8c95c8d94a0801" in out
    assert "ff452ff5a3e828ce110190feff1178bb1f2ea2281fa2075aadb987c2fb221661" in out
    # Exactly one url/sha256 changed: the resources still hold their own values.
    assert out.count(f'sha256 "{sha}"') == 1


def test_updater_is_idempotent(formula_text):
    mod = _load_updater()
    url = "https://example.invalid/alle_proxy-0.1.9.tar.gz"
    sha = "b" * 64
    once = mod.rewrite_source(formula_text, url, sha)
    twice = mod.rewrite_source(once, url, sha)
    assert once == twice


def test_updater_rejects_a_mangled_formula():
    mod = _load_updater()
    with pytest.raises(mod.UpdateError, match="url/sha256"):
        mod.rewrite_source("class Alle < Formula\nend\n", "u", "s")
