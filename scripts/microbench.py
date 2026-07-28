#!/usr/bin/env python3
"""Opt-in microbenchmarks for alle's measured hot paths.

Run locally to produce before/after evidence for optimization work:

    uv run python scripts/microbench.py
    uv run python scripts/microbench.py --only routes --repeat 3
    uv run python scripts/microbench.py --json > after.json

Deliberately **not** a CI gate. Wall-clock numbers depend on the machine, its
thermal state, and what else is running, so they are evidence a human reads —
never an assertion. The deterministic half of the same contracts (how many
subprocesses and verified pidfile reads each path performs) is pinned by
``tests/test_perf_contracts.py``, which is where regressions must fail.

Every benchmark is hermetic: a throwaway ``ALLE_HOME``, no provider
credentials, and no network. The one real subprocess is a local stand-in for
the Homebrew ``opt/bin/alle`` shim — an interpreter start plus an ``alle``
import, which is what that shim actually costs — so the Homebrew-shaped status
number reflects real process start-up rather than a mocked constant.

Comparing two runs honestly means comparing the same benchmark names, the same
``--repeat``, and the same machine. The environment block printed above the
results carries what is needed to check that; when something did change, say
so alongside the numbers instead of presenting them as a speedup.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))


# ---- the hermetic bench home -------------------------------------------------


class BenchHome:
    """A throwaway ``ALLE_HOME`` plus the fixtures the benchmarks compose from.

    Nothing here may reach the developer's real installation: the home is a
    temp dir, the privileged-helper socket points at a path nothing binds (so
    an ownership lookup can never consult a live helper), and every
    service-shape variable is set explicitly rather than inherited.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        os.environ["ALLE_HOME"] = str(root / "home")
        os.environ["ALLE_HELPER_SOCKET"] = str(root / "no-helper.sock")
        for var in ("ALLE_SERVICE_OWNER", "ALLE_SERVICE_PREFIX", "ALLE_SERVICE"):
            os.environ.pop(var, None)

    # -- synthetic state ------------------------------------------------------

    def write_state(self, channels: int = 1, provider: str = "nordvpn") -> None:
        """Replace state.json with ``channels`` synthetic channels."""
        from alle import state

        data = state._blank()
        data["router"]["port"] = 18080
        data["providers"][provider] = {
            "channels": {
                f"wg_us_{i + 1}": {
                    "country": "United States",
                    "city": f"City {i % 40}",
                    "port": 19000 + i,
                    "wg": _WG_PARAMS,
                    "probe": {
                        "ok": True,
                        "latency_ms": 30 + i % 70,
                        "ip": "203.0.113.7",
                    },
                }
                for i in range(channels)
            }
        }
        state._write_raw(data)

    def write_geo_rules(self, categories: int, per_category: int) -> None:
        """Cache ``categories`` geo rule-sets offline and reference each of them
        ``per_category`` times in the rule table.

        Duplicate references are the shape that matters: the compile verifies
        each distinct category once, so the cost of the table should follow the
        number of categories, not the number of rules.
        """
        from alle import geodata, state

        data = state._read_raw()
        rules = []
        files: dict[str, dict] = {}
        for c in range(categories):
            name = f"category-{c}"
            # Distinct content per category, so each has its own digest and
            # content-addressed file — as a real cache does.
            files[name] = geodata._write_cache("geosite", name, _srs_bytes(c))
            rules.extend(
                {
                    "id": f"r{len(rules) + i + 1}",
                    "type": "geosite",
                    "value": name,
                    "target": "direct",
                }
                for i in range(per_category)
            )
        data["router"]["rules"] = rules
        data["geodata"] = {
            "source": geodata.DEFAULT_SOURCE,
            "geosite": {
                "source": geodata.DEFAULT_SOURCE,
                "commit": "0" * 40,
                "files": files,
            },
        }
        state._write_raw(data)

    # -- process identity -----------------------------------------------------

    def claim_pidfiles(self) -> None:
        """Record this process as both the live sing-box and the live daemon.

        Identity verification is the cost under measurement, and it only
        happens for a pidfile whose recorded start time still matches — so the
        benchmark has to look like a running installation, not a stopped one.
        Our own PID is the one process guaranteed to be alive and ours.
        """
        from alle import daemon, proc, singbox

        proc.write_pidfile(singbox._pid_path(), os.getpid())
        proc.write_pidfile(daemon._pid_path(), os.getpid())

    def release_pidfiles(self) -> None:
        from alle import daemon, singbox

        singbox._pid_path().unlink(missing_ok=True)
        daemon._pid_path().unlink(missing_ok=True)

    # -- installed-version discovery -----------------------------------------

    @contextlib.contextmanager
    def stubbed_version(self, value: str = "0.0.0-bench") -> Iterator[None]:
        """Take version discovery out of the measurement entirely."""
        from alle import daemon

        original = daemon.installed_version
        daemon.installed_version = lambda: value  # type: ignore[assignment]
        try:
            yield
        finally:
            daemon.installed_version = original  # type: ignore[assignment]

    @contextlib.contextmanager
    def homebrew_shim(self) -> Iterator[None]:
        """A Homebrew-shaped install whose opt shim really starts a process."""
        prefix = self.root / "opt" / "alle"
        shim = prefix / "bin" / "alle"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" -c "import alle; print(alle.__version__)"\n'
        )
        shim.chmod(0o755)
        os.environ["ALLE_SERVICE_OWNER"] = "homebrew"
        os.environ["ALLE_SERVICE_PREFIX"] = str(prefix)
        try:
            yield
        finally:
            os.environ.pop("ALLE_SERVICE_OWNER", None)
            os.environ.pop("ALLE_SERVICE_PREFIX", None)

    def shim_description(self) -> str:
        return f"/bin/sh -> {Path(sys.executable).name} -c 'import alle; print(...)'"


# Placeholder keys, kept as short as the test suite's (tests/conftest.py): the
# benchmarks never hand these to WireGuard, and anything longer reads as a
# high-entropy literal to the repo's secret scan.
def _srs_bytes(seed: int) -> bytes:
    """A minimal well-formed binary rule-set: header plus a zlib body.

    Content differs per seed so every category gets its own digest; the compile
    only reads and hashes these, so an empty rule list is enough.
    """
    import zlib

    return b"SRS\x01" + zlib.compress(b"\x00" + bytes([seed % 251]))


_WG_PARAMS = {
    "private_key": "PRIV=",
    "address": ["10.5.0.2/32"],  # noqa: S1313
    "peer": {
        "public_key": "PUB=",
        "endpoint_host": "198.51.100.10",
        "endpoint_port": 51820,
        "preshared_key": None,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "keepalive": 25,
    },
}


# ---- registry ----------------------------------------------------------------


@dataclass(frozen=True)
class Benchmark:
    name: str
    setup: Callable[[BenchHome], contextlib.AbstractContextManager]
    repeat: int
    note: str


BENCHMARKS: list[Benchmark] = []


def bench(name: str, *, repeat: int, note: str):
    """Register a benchmark whose body yields the callable to time.

    Setup runs outside the timed section, so each recorded number is the cost
    of the operation itself rather than of building its inputs.
    """

    def register(fn):
        BENCHMARKS.append(Benchmark(name, contextlib.contextmanager(fn), repeat, note))
        return fn

    return register


# ---- status composition ------------------------------------------------------


@bench(
    "status.compose",
    repeat=200,
    note="one channel, no live pidfile, version discovery stubbed",
)
def _status_compose(home: BenchHome):
    from alle import service

    home.write_state(channels=1)
    home.release_pidfiles()
    with home.stubbed_version():
        yield service.status_snapshot


@bench(
    "status.compose@1000ch",
    repeat=50,
    note="1,000 channels, same pure-composition shape as status.compose",
)
def _status_compose_1000(home: BenchHome):
    from alle import service

    home.write_state(channels=1000)
    home.release_pidfiles()
    with home.stubbed_version():
        yield service.status_snapshot


@bench(
    "status.native",
    repeat=50,
    note="live sing-box + daemon pidfiles, native (importlib.metadata) version",
)
def _status_native(home: BenchHome):
    from alle import service

    home.write_state(channels=1)
    home.claim_pidfiles()
    try:
        yield service.status_snapshot
    finally:
        home.release_pidfiles()


@bench(
    "status.homebrew",
    repeat=20,
    note="as status.native, Homebrew-shaped: steady-state polling, version cached",
)
def _status_homebrew(home: BenchHome):
    """What a Web-UI tab actually pays per poll on a Homebrew install."""
    from alle import service

    home.write_state(channels=1)
    home.claim_pidfiles()
    try:
        with home.homebrew_shim():
            yield service.status_snapshot
    finally:
        home.release_pidfiles()


@bench(
    "status.homebrew_cold",
    repeat=20,
    note="Homebrew-shaped with the version cache dropped before every call",
)
def _status_homebrew_cold(home: BenchHome):
    """What one version-cache miss costs — paid at most once per cache window.

    The comparable successor to the old uncached `status.homebrew`: before the
    cache existed, *every* poll looked like this.
    """
    from alle import daemon, service

    home.write_state(channels=1)
    home.claim_pidfiles()

    def cold_status():
        daemon.forget_installed_version()
        return service.status_snapshot()

    try:
        with home.homebrew_shim():
            yield cold_status
    finally:
        home.release_pidfiles()


# ---- process identity + metrics ---------------------------------------------


@bench(
    "metrics.identity",
    repeat=50,
    note="is_running() + two generation() reads; no Clash API request, no SQLite",
)
def _metrics_identity(home: BenchHome):
    """The identity sequence the daemon's metrics pass performs each sample.

    Mirrors ``daemon.run_applier``'s ``_metrics_pass`` up to (not including)
    the Clash API request and the accumulator, which are what this sequence
    is overhead *for*. The real path's call counts are pinned separately by
    ``tests/test_perf_contracts.py``; this only prices them.
    """
    from alle import singbox

    home.write_state(channels=1)
    home.claim_pidfiles()
    runner = singbox.Runner()

    def sample_identity():
        if not runner.is_running():
            raise RuntimeError("benchmark pidfile stopped verifying")
        before = runner.generation()
        after = runner.generation()
        return before, after

    try:
        yield sample_identity
    finally:
        home.release_pidfiles()


@bench(
    "proc.verify",
    repeat=200,
    note="one recorded pidfile identity proven live (the unit both paths repeat)",
)
def _proc_verify(home: BenchHome):
    from alle import proc

    record = proc.record(os.getpid())
    yield lambda: proc.verify(record, ("sing-box",))


# ---- geo rule-set verification ----------------------------------------------


@bench(
    "geo.compile@20cat_x10",
    repeat=50,
    note="config build over 200 geo rules naming 20 distinct cached categories",
)
def _geo_compile(home: BenchHome):
    """Prices the digest checks a compile performs for duplicate geo matchers."""
    from alle.engine import Engine
    from alle.state import Store

    home.write_state(channels=1)
    home.write_geo_rules(categories=20, per_category=10)

    def build():
        return Engine(Store.load())._build_config()

    yield build


# ---- route shadow analysis ---------------------------------------------------


def synthetic_rules(count: int) -> list[dict]:
    """A rule table of ``count`` mutually non-covering rules.

    Non-covering on purpose: a covering rule lets the lint stop scanning at the
    first hit, so a table full of them would flatter a quadratic scan. This is
    the shape a large real table trends toward (many specific destinations,
    few redundancies) *and* the worst case for the current algorithm, which
    makes it the honest input for a before/after comparison.

    Deterministic, so two runs on different days compare like for like.
    """
    rules: list[dict] = []
    for i in range(count):
        match i % 5:
            case 0:
                matcher = ("domain_suffix", f"host{i}.example{i}.com")
            case 1:
                matcher = ("ip_cidr", f"10.{i // 256}.{i % 256}.0/24")
            case 2:
                matcher = ("geosite", f"category-{i}")
            case 3:
                matcher = ("ip_cidr", f"2001:db8:{i:x}::/48")
            case _:
                matcher = ("geoip", f"cc{i}")
        rules.append({"id": f"r{i + 1}", "type": matcher[0], "value": matcher[1]})
    return rules


def _shadow_bench(count: int, repeat: int):
    @bench(
        f"routes.shadowed_by@{count}",
        repeat=repeat,
        note=f"{count} non-covering rules (worst case for a pairwise scan)",
    )
    def _run(home: BenchHome, count: int = count):
        from alle import routes

        rules = synthetic_rules(count)
        yield lambda: routes.shadowed_by(rules)


for _count, _repeat in ((100, 200), (500, 50), (1000, 20), (2000, 10)):
    _shadow_bench(_count, _repeat)


# ---- metrics storage ---------------------------------------------------------


@bench(
    "metrics.speed_batch@50ch",
    repeat=20,
    note="counter reads for a 50-channel speed batch over 1,000 stored rows",
)
def _metrics_speed_batch(home: BenchHome):
    """The metrics half of a speed test: what reading each row's counters costs.

    Sized like a busy install — 1,000 stored rows, of which 50 are being
    tested — because the cost that mattered was reading *every* stored row once
    per completed channel.
    """
    from alle import metrics

    metrics.add_deltas(
        {
            (f"provider{i % 10}", f"wg_us_{i + 1}"): (1024 + i, 2048 + i)
            for i in range(1000)
        }
    )
    tested = [(f"provider{i % 10}", f"wg_us_{i + 1}") for i in range(50)]

    def read_counters():
        # One reading to build the rows, then one per completed row — the
        # streaming shape, which is the one that still reads per row.
        totals = metrics.totals()
        rows = [totals.get(ref, {}) for ref in tested]
        rows.extend(metrics.total(*ref) for ref in tested)
        return rows

    yield read_counters


@bench(
    "metrics.add_deltas@1000ch",
    repeat=20,
    note="one SQLite transaction banking a 1,000-channel sample",
)
def _metrics_add_deltas(home: BenchHome):
    from alle import metrics

    deltas = {
        (f"provider{i % 10}", f"wg_us_{i + 1}"): (1024 + i, 2048 + i)
        for i in range(1000)
    }
    yield lambda: metrics.add_deltas(deltas)


# ---- driver ------------------------------------------------------------------


def environment(home: BenchHome) -> dict:
    """Everything a later run needs to judge whether it is comparable."""
    from alle import __version__

    return {
        "when": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "alle": __version__,
        "git": _git_description(),
        "python": f"{platform.python_version()} ({platform.python_implementation()})",
        "platform": platform.platform(),
        "machine": f"{platform.machine()} ({os.cpu_count()} cpus)",
        "home": str(home.root),
        "homebrew_shim": home.shim_description(),
    }


def _git_description() -> str:
    try:
        head = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirty = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if head.returncode != 0:
        return "unknown"
    state = "dirty" if dirty.stdout.strip() else "clean"
    return f"{head.stdout.strip()} ({state})"


def run_one(benchmark: Benchmark, home: BenchHome, scale: float) -> dict:
    from alle import daemon

    repeat = max(1, round(benchmark.repeat * scale))
    # Installed-version discovery is cached per process: without this, one
    # benchmark's warm cache would silently pay for the next one's reads.
    daemon.forget_installed_version()
    with benchmark.setup(home) as call:
        call()  # warm-up: first-call imports and caches are not the subject
        samples = []
        for _ in range(repeat):
            started = time.perf_counter()
            call()
            samples.append((time.perf_counter() - started) * 1000)
    return {
        "name": benchmark.name,
        "note": benchmark.note,
        "repeat": repeat,
        "best_ms": min(samples),
        "median_ms": statistics.median(samples),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        # Its own string, not __doc__: the module docstring is developer
        # documentation (and is None under `python -OO`).
        description="Opt-in microbenchmarks for alle's measured hot paths.",
        epilog="Local evidence only — never a CI gate.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SUBSTRING",
        help="run only benchmarks whose name contains this (repeatable)",
    )
    parser.add_argument(
        "--repeat",
        type=float,
        default=1.0,
        metavar="SCALE",
        help="scale every benchmark's iteration count (default 1.0)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable results only"
    )
    parser.add_argument(
        "--list", action="store_true", help="list the benchmark names and exit"
    )
    args = parser.parse_args(argv)

    selected = [
        b
        for b in BENCHMARKS
        if not args.only or any(needle in b.name for needle in args.only)
    ]
    if args.list:
        for b in selected:
            print(f"{b.name:28} {b.note}")
        return 0
    if not selected:
        print("no benchmark matched --only", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="alle-microbench-"))
    try:
        home = BenchHome(root)
        env = environment(home)
        if not args.json:
            _print_header(env)
        results = []
        for benchmark in selected:
            result = run_one(benchmark, home, args.repeat)
            results.append(result)
            if not args.json:
                print(
                    f"  {result['name']:<28} {result['repeat']:>6}"
                    f" {result['best_ms']:>10.3f} {result['median_ms']:>10.3f}"
                )
        if args.json:
            json.dump({"environment": env, "results": results}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print()
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0


def _print_header(env: dict) -> None:
    print("\nalle microbenchmarks — local evidence, not a CI gate\n")
    for key, value in env.items():
        print(f"  {key:<14} {value}")
    print()
    print(f"  {'benchmark':<28} {'runs':>6} {'best/ms':>10} {'median/ms':>10}")


if __name__ == "__main__":
    raise SystemExit(main())
