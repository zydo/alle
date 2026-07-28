"""Deterministic call-count contracts for the performance-sensitive paths.

The optimization work that follows rewrites process-identity verification,
installed-version discovery, and the daemon's metrics sample. What makes those
paths expensive is not an algorithm — it is *how many subprocesses and verified
pidfile reads* one logical operation performs. So this module pins that count
directly. Nothing here measures elapsed time: wall-clock numbers belong in the
opt-in microbenchmarks (``scripts/microbench.py``), never in an assertion.

Each contract asserts an exact count, so work that creeps back in fails
specifically rather than as a slowdown nobody notices. The counts these paths
started from, before the identity snapshot and the status version cache:

===============================  ============  =========
contract                         was           is
===============================  ============  =========
one local process verification   2 ``ps``      1 ``ps``
one ``Runner.generation()``      2 verifies    1 verify
one metrics sample               5 verifies    2 verifies
five Web-UI status polls         5 shims       1 shim
===============================  ============  =========

The BSD/no-procfs identity path is forced on every platform: Linux reads
``/proc`` and spawns nothing, so alle's primary macOS target would otherwise go
unmeasured on CI.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from alle import daemon, proc, service, singbox

# The `ps` counts below assume the no-procfs path (macOS/BSD), which
# `bsd_identity` forces everywhere. `verify` counts are platform-independent.
PS_PER_VERIFICATION = 1  # one `ps -o state=,lstart=` snapshot

METRICS_THREAD = "alle-metrics"  # the daemon names its metrics worker thread


# ---- instrumentation ---------------------------------------------------------


class _ModuleSpy:
    """Stand-in for a module attribute with some of its functions replaced.

    Scoped substitution: patching ``proc.subprocess`` leaves the real
    ``subprocess`` module untouched for every other importer, so a contract
    that counts one module's subprocess use cannot see (or disturb) another's.
    """

    def __init__(self, module, **replacements) -> None:
        self.__dict__["_module"] = module
        self.__dict__.update(replacements)

    def __getattr__(self, name):
        return getattr(self._module, name)


# What a real `ps` prints for each field alle asks for. Values are arbitrary
# but must be stable: identity comparison only needs the same process to answer
# the same way twice.
_PS_FIELD_OUTPUT = {
    "state": "S",
    "lstart": "Mon Jul 28 04:00:00 2026",
    "command": "/opt/alle/bin/sing-box run -c /home/u/.alle/singbox.json",
}


class FakePs:
    """A ``ps`` that answers any ``-o`` field list and counts its executions.

    Field-driven on purpose. It answers today's two single-field calls and a
    future combined ``-o state=,lstart=`` call the same way, so the contract
    measures how many processes are spawned rather than which flags reach them
    — the counts stay meaningful across the rewrite they exist to guard.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def run(self, cmd, **_kwargs):
        self.calls.append((threading.current_thread().name, list(cmd)))
        fmt = cmd[cmd.index("-o") + 1]
        fields = [field.rstrip("=") for field in fmt.split(",")]
        out = " ".join(_PS_FIELD_OUTPUT[field] for field in fields)
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

    def count(self, thread: str | None = None) -> int:
        return sum(1 for name, _ in self.calls if thread in (None, name))

    def reset(self) -> None:
        self.calls.clear()


class CountingCall:
    """Wraps a function and records who called it, and how often."""

    def __init__(self, func) -> None:
        self._func = func
        self.threads: list[str] = []

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.current_thread().name)
        return self._func(*args, **kwargs)

    def count(self, thread: str | None = None) -> int:
        return sum(1 for name in self.threads if thread in (None, name))

    def reset(self) -> None:
        self.threads.clear()


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture
def bsd_identity(monkeypatch, tmp_path):
    """Force the no-procfs identity path and count the ``ps`` runs it makes."""
    absent = tmp_path / "no-procfs"

    def redirect_procfs(*args):
        path = Path(*args)
        if path.is_absolute() and path.parts[1:2] == ("proc",):
            # A path that simply does not exist: the reads fail with the same
            # OSError a BSD kernel produces by having no procfs at all.
            return absent.joinpath(*path.parts[2:])
        return path

    ps = FakePs()
    monkeypatch.setattr(proc, "Path", redirect_procfs)
    monkeypatch.setattr(proc, "subprocess", _ModuleSpy(subprocess, run=ps.run))
    return ps


@pytest.fixture
def verifications(monkeypatch):
    """Count verified identity reads (``proc.verify``) wherever they happen."""
    counter = CountingCall(proc.verify)
    monkeypatch.setattr(proc, "verify", counter)
    return counter


@pytest.fixture
def live_singbox_pidfile(bsd_identity):
    """A sing-box pidfile whose recorded identity verifies against this process.

    ``Runner`` then takes the local-ownership path — the one every status call,
    metrics sample, and probe uses in explicit-proxy mode — without a real
    sing-box or a helper round-trip.
    """
    proc.write_pidfile(singbox._pid_path(), os.getpid())
    bsd_identity.reset()
    return singbox._pid_path()


@pytest.fixture
def homebrew_version_shim(monkeypatch, tmp_path):
    """A Homebrew-shaped install, counting every ``opt/bin/alle version`` spawn.

    This is the 100+ ms outlier in the audit: the stable opt prefix is the only
    reliable view of a moved keg, so status discovery pays a whole interpreter
    start-up per call.
    """
    prefix = tmp_path / "opt" / "alle"
    (prefix / "bin").mkdir(parents=True)
    monkeypatch.setenv("ALLE_SERVICE_OWNER", "homebrew")
    monkeypatch.setenv("ALLE_SERVICE_PREFIX", str(prefix))

    spawns: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        spawns.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="9.9.9\n", stderr="")

    monkeypatch.setattr(daemon, "subprocess", _ModuleSpy(subprocess, run=fake_run))
    return spawns


# ---- one local process verification ------------------------------------------


def measure_verification(ps: FakePs) -> int:
    """``ps`` runs spent proving a recorded pidfile identity is still live."""
    record = proc.record(os.getpid())  # what write_pidfile stores at spawn
    assert record["start"], "the identity path did not capture a start marker"
    ps.reset()
    assert proc.verify(record, ()) is True
    return ps.count()


def test_local_process_verification_runs_one_ps(bsd_identity):
    """One snapshot carries both the state and the start time (was two runs)."""
    assert measure_verification(bsd_identity) == PS_PER_VERIFICATION


def test_verification_still_rejects_a_recycled_pid(bsd_identity):
    """The count contract must never be met by skipping the start-time check."""
    stale = {"pid": os.getpid(), "start": "not-this-process"}
    assert proc.verify(stale, ()) is False


# ---- one Runner.generation() -------------------------------------------------


def measure_generation(ps: FakePs, verify: CountingCall) -> dict[str, int]:
    """Work spent answering "which sing-box instance is running right now?"."""
    ps.reset()
    verify.reset()
    generation = singbox.Runner().generation()
    assert generation and generation.startswith(f"{os.getpid()}/")
    return {"verifications": verify.count(), "ps": ps.count()}


def test_generation_verifies_identity_once(
    bsd_identity, verifications, live_singbox_pidfile
):
    """One public operation, one verification — the marker is carried out of it
    rather than rebuilt by rereading and reverifying the same record."""
    assert measure_generation(bsd_identity, verifications) == {
        "verifications": 1,
        "ps": PS_PER_VERIFICATION,
    }


def test_generation_is_none_when_the_pidfile_is_not_ours(bsd_identity, tmp_path):
    """Fail-closed behaviour is part of the contract being pinned."""
    singbox._pid_path().write_text('{"pid": %d, "start": "recycled"}' % os.getpid())
    assert singbox.Runner().generation() is None


# ---- one metrics sample ------------------------------------------------------


class _NoopEngine:
    """Keeps the driven applier loop off reconcile/probe work."""

    def __init__(self, store) -> None:
        self.store = store

    def reconcile(self) -> dict:
        return {}


def measure_metrics_sample(
    monkeypatch, ps: FakePs, verify: CountingCall
) -> dict[str, int]:
    """Identity work inside one real metrics pass of the daemon loop.

    The loop is driven for a single iteration rather than reproducing the
    sample here: the sequence itself is what the optimization changed, so a
    copy in the test would keep passing while measuring code that no longer
    exists.
    """

    class _Clock:
        t = float(daemon.METRICS_INTERVAL + 1)

        def monotonic(self) -> float:
            return self.t

        def time(self) -> float:
            return 1000.0

        def sleep(self, _seconds) -> None:
            raise KeyboardInterrupt  # end the loop after one iteration

    monkeypatch.setattr("alle.engine.Engine", _NoopEngine)
    monkeypatch.setattr(daemon, "time", _Clock())
    ps.reset()
    verify.reset()
    handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.run_applier()
    finally:
        signal.signal(signal.SIGTERM, handlers[0])
        signal.signal(signal.SIGINT, handlers[1])
    # Only the metrics worker's own thread counts: the loop's supervision tick
    # and the daemon's own pidfile writes share this process.
    return {
        "verifications": verify.count(METRICS_THREAD),
        "ps": ps.count(METRICS_THREAD),
    }


def test_metrics_sample_verifies_identity_twice(
    monkeypatch, bsd_identity, verifications, live_singbox_pidfile
):
    """Exactly the two generation reads that bracket the sample — the
    preliminary is_running() pass was a third check of the same pid."""
    assert measure_metrics_sample(monkeypatch, bsd_identity, verifications) == {
        "verifications": 2,
        "ps": 2 * PS_PER_VERIFICATION,
    }


# ---- repeated Web-UI status snapshots ----------------------------------------

STATUS_POLLS = 5  # a Web-UI tab polls /api/v1/status every three seconds


def measure_status_polls(ps: FakePs, shim_spawns: list) -> dict[str, int]:
    """Per-poll subprocess cost of the surface a browser tab hits on a timer."""
    ps.reset()
    shim_spawns.clear()
    for _ in range(STATUS_POLLS):
        snapshot = service.status_snapshot()
        assert snapshot["running"] is True
        assert snapshot["daemon"]["installed_version"] == "9.9.9"
    return {"version_shims": len(shim_spawns), "ps": ps.count()}


def test_status_polls_share_one_version_discovery(
    bsd_identity, homebrew_version_shim, live_singbox_pidfile
):
    """A tab polling on a timer pays for the Homebrew shim once per cache
    window, and for one identity snapshot per poll."""
    assert measure_status_polls(bsd_identity, homebrew_version_shim) == {
        "version_shims": 1,
        "ps": STATUS_POLLS * PS_PER_VERIFICATION,
    }


def test_status_reports_a_stopped_daemon_without_a_live_pidfile(
    bsd_identity, homebrew_version_shim
):
    """No pidfile means no identity subprocess at all — and no phantom daemon."""
    snapshot = service.status_snapshot()
    assert snapshot["running"] is False
    assert bsd_identity.count() == 0
