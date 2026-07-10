"""The applier's change-detection: the config signature is what triggers a
reconcile, so it must move on any channel add/remove/edit and stay put for
probe-only writes — and a *failed* reconcile is retried on a timer even when
the signature never moves."""

from __future__ import annotations

import json
import signal

import pytest

from alle import daemon
from alle.state import Store, _read_raw, config_signature
from conftest import wg_config

WG = wg_config("se1.example.com")


def _sig():
    return config_signature(_read_raw())


def test_signature_tracks_channel_lifecycle():
    empty = _sig()
    store = Store.load()
    store.add_provider("nordvpn")
    assert _sig() == empty  # an empty provider has no config-relevant content

    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    after_add = _sig()
    assert after_add != empty

    store.remove_channel("nordvpn", ch.id)
    assert _sig() == empty  # removing it returns to the empty signature


def test_signature_ignores_probe_writes():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    before = _sig()
    store.set_probe("nordvpn", ch.id, {"ok": True, "ip": "1.2.3.4", "at": 1})
    assert _sig() == before


def test_state_stamp_moves_only_on_writes():
    a = daemon._state_stamp()
    store = Store.load()
    store.add_provider("nordvpn")
    b = daemon._state_stamp()
    assert b != a  # a write is visible without parsing the file
    assert daemon._state_stamp() == b  # no write, no change


def test_failed_reconcile_is_retried_without_a_state_change(monkeypatch):
    """An environmental failure (offline download, stolen port) must be retried
    on RECONCILE_RETRY's cadence, not sit broken until the user edits state."""
    calls: list[int] = []

    class _BoomEngine:
        def __init__(self, store):
            self.store = store

        def reconcile(self):
            calls.append(1)
            raise RuntimeError("boom")

    class _FakeTime:
        """Each sleep jumps past the retry window; the third ends the loop."""

        def __init__(self):
            self.t = 0.0
            self.sleeps = 0

        def monotonic(self):
            return self.t

        def time(self):
            return 1000.0

        def sleep(self, _seconds):
            self.sleeps += 1
            self.t += daemon.RECONCILE_RETRY + 1
            if self.sleeps >= 3:
                raise KeyboardInterrupt

    monkeypatch.setattr("alle.engine.Engine", _BoomEngine)
    monkeypatch.setattr(daemon, "time", _FakeTime())
    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.run_applier()
    finally:  # run_applier installs its own handlers; restore pytest's
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)

    assert len(calls) >= 2  # retried though the config signature never moved


# ---- supervision: unexpected exit, crash loops, rejected configs ---------------


def _run_loop(monkeypatch, engine_cls, runner_cls, *, step=2.1, sleeps=3):
    """Drive run_applier with a fake clock stepping ``step``s per sleep and a
    KeyboardInterrupt after ``sleeps`` iterations."""
    from alle import singbox

    class _Clock:
        def __init__(self):
            self.t = 0.0
            self.sleeps = 0
            self.info: dict = {}

        def monotonic(self):
            return self.t

        def time(self):
            return 1000.0

        def sleep(self, _s):
            self.sleeps += 1
            self.t += step
            if self.sleeps >= sleeps:
                # snapshot before the loop's exit cleanup unlinks the file
                self.info = json.loads(daemon._info_path().read_text())
                raise KeyboardInterrupt

    clock = _Clock()
    monkeypatch.setattr("alle.engine.Engine", engine_cls)
    monkeypatch.setattr(singbox, "Runner", runner_cls)
    monkeypatch.setattr(daemon, "time", clock)
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.run_applier()
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])
    return clock


def test_supervision_restarts_an_unexpected_exit(monkeypatch):
    """sing-box dying between probes is detected and restarted by the loop
    itself — with the kill switch enabled, exactly the state where a dead
    process means total outage."""
    Store.load().set_killswitch(True)
    world = {"running": False, "reconciles": 0}

    class _Eng:
        def __init__(self, store):
            self.store = store

        def reconcile(self):
            world["reconciles"] += 1
            world["running"] = True  # a reconcile brings sing-box up
            return {}

    class _Runner:
        def is_running(self):
            if world["reconciles"] == 1 and world["running"]:
                world["running"] = False  # crash after the first reconcile
                return True
            return world["running"]

        def connections(self):
            return []

        def generation(self):
            return None

    _run_loop(monkeypatch, _Eng, _Runner, sleeps=4)
    assert world["reconciles"] >= 2  # initial apply + supervised restart
    assert world["running"] is True  # recovered
    log = _read_log()
    assert "exited unexpectedly" in log and "restarted after unexpected exit" in log


def test_supervision_backs_off_on_a_crash_loop(monkeypatch):
    """A config that dies right after every start must not trigger a 1 Hz
    restart storm: attempts space out exponentially, capped at 60s."""
    world = {"reconciles": 0}

    class _Eng:
        def __init__(self, store):
            self.store = store

        def reconcile(self):
            world["reconciles"] += 1
            return {}  # "succeeds", but the process never stays up

    class _Runner:
        def is_running(self):
            return False

        def connections(self):
            return []

        def generation(self):
            return None

    clock = _run_loop(monkeypatch, _Eng, _Runner, step=2.0, sleeps=40)  # ~80 fake s
    # 1 initial + crashes at ~0,2,4,8,16,32,64 fake-seconds ≈ 8 total; a
    # storm would be ~40. Bounds are loose on purpose (tick alignment).
    assert 4 <= world["reconciles"] <= 12
    assert "crash 5" in _read_log()  # kept counting, kept backing off
    assert clock.info["runtime"]["singbox"] == "crash_looping"


def test_rejected_config_is_not_retried_on_the_timer(monkeypatch):
    """A deterministic rejection waits for a state change instead of burning
    the RECONCILE_RETRY timer on a config that cannot start."""
    from alle import singbox

    store = Store.load()
    store.add_provider("nordvpn")
    calls: list[int] = []

    class _Eng:
        def __init__(self, store):
            self.store = store

        def reconcile(self):
            calls.append(1)
            raise singbox.ConfigRejectedError("unknown field frobnicate")

    class _Runner:
        def is_running(self):
            return False

        def connections(self):
            return []

        def generation(self):
            return None

    info: dict = {}

    class _Clock:
        def __init__(self):
            self.t = 0.0
            self.sleeps = 0

        def monotonic(self):
            return self.t

        def time(self):
            return 1000.0

        def sleep(self, _s):
            self.sleeps += 1
            self.t += daemon.RECONCILE_RETRY + 1  # far past any retry timer
            if self.sleeps == 3:
                # a real state change: the only thing that may retry a rejection
                Store.load().add_channel("nordvpn", "US", "", dict(WG))
            if self.sleeps >= 5:
                # snapshot before the loop's exit cleanup unlinks the file
                info.update(json.loads(daemon._info_path().read_text()))
                raise KeyboardInterrupt

    monkeypatch.setattr("alle.engine.Engine", _Eng)
    monkeypatch.setattr(singbox, "Runner", _Runner)
    monkeypatch.setattr(daemon, "time", _Clock())
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.run_applier()
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])

    assert len(calls) == 2  # once at start, once after the state change — no storm
    assert "rejected the generated config" in _read_log()
    assert info["runtime"]["singbox"] == "config_rejected"
    assert "frobnicate" in info["runtime"]["detail"]


def test_a_stuck_probe_pass_does_not_block_reconciles(monkeypatch):
    """The probe + reconnect pass runs on its own thread, so a probe that hangs
    (a fleet of dead channels under slow echo sources) can't delay a config
    reconcile. In the old inline code the loop body blocked on probe_all and
    the reconcile cadence stalled; now the main loop keeps applying state."""
    import threading

    store = Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG))

    release = threading.Event()
    reconciled = {"n": 0}
    from alle import singbox

    class _Eng:
        def __init__(self, inner_store):
            self.store = inner_store
            self.runner = _Runner()

        def reconcile(self):
            reconciled["n"] += 1
            return {}

        def probe_all(self):
            release.wait(5)  # block until the test releases us

    class _Runner:
        def is_running(self):
            return True

        def connections(self):
            return []

        def generation(self):
            return None

    class _Clock:
        """Steps past PROBE_INTERVAL each sleep and toggles killswitch so a
        reconcile is due every tick — proving the reconcile path runs freely
        while the probe worker is stuck."""

        def __init__(self):
            self.t = 0.0
            self.sleeps = 0

        def monotonic(self):
            return self.t

        def time(self):
            return 1000.0

        def sleep(self, _s):
            self.sleeps += 1
            self.t += daemon.PROBE_INTERVAL + 1  # a probe pass is due every tick
            # a real state change each tick so a reconcile fires each tick
            Store.load().set_killswitch(self.sleeps % 2 == 0)
            if self.sleeps >= 4:
                raise KeyboardInterrupt

    monkeypatch.setattr("alle.engine.Engine", _Eng)
    monkeypatch.setattr(singbox, "Runner", _Runner)
    monkeypatch.setattr(daemon, "time", _Clock())
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        with pytest.raises(KeyboardInterrupt):
            daemon.run_applier()
    finally:
        release.set()  # unblock the probe worker so it can exit
        # join the worker by name so any trailing state writes finish before
        # the temp-dir fixture cleans up (no race on the state dir)
        for th in threading.enumerate():
            if th.name == "alle-probe":
                th.join(5)
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])

    # Reconciles fired repeatedly while the probe was stuck: the main loop was
    # not blocked. (With the old inline probe_all, the loop stalled on the
    # first blocked probe and reconciled once at most.)
    assert reconciled["n"] >= 3


# ---- version info file + skew --------------------------------------------------


def test_daemon_info_trusts_only_the_live_pid(monkeypatch):
    monkeypatch.setattr(daemon, "running_pid", lambda: None)
    assert daemon.daemon_info() is None  # not running → no info

    monkeypatch.setattr(daemon, "running_pid", lambda: 4242)
    daemon._info_path().write_text('{"pid": 4242, "version": "0.9.9", "at": 1}')
    info = daemon.daemon_info()
    assert info is not None and info["version"] == "0.9.9"

    # a stale info file from a different pid is not trusted
    daemon._info_path().write_text('{"pid": 1, "version": "0.0.1", "at": 1}')
    info = daemon.daemon_info()
    assert info is not None and info["version"] is None


def test_installed_version_is_readable():
    v = daemon.installed_version()
    assert isinstance(v, str) and v  # resolves to the installed package version


# ---- self-restart on upgrade (supervised only) ---------------------------------


class _NoopEngine:
    def __init__(self, store):
        self.store = store

    def reconcile(self):
        return {}


def _drive_once(monkeypatch, now=daemon.VERSION_CHECK + 1):
    """Run run_applier with a clock parked past the version-check window so the
    first loop iteration performs the version check, then stop it."""

    class _Clock:
        t = float(now)

        def monotonic(self):
            return self.t

        def time(self):
            return 1000.0

        def sleep(self, _s):
            raise KeyboardInterrupt  # end after one iteration

    monkeypatch.setattr("alle.engine.Engine", _NoopEngine)
    monkeypatch.setattr(daemon, "time", _Clock())
    old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    try:
        daemon.run_applier()
    finally:
        signal.signal(signal.SIGTERM, old[0])
        signal.signal(signal.SIGINT, old[1])


def test_supervised_daemon_exits_nonzero_on_version_change(monkeypatch):
    monkeypatch.setenv("ALLE_SERVICE", "1")
    monkeypatch.setattr(daemon, "installed_version", lambda: "99.0.0")
    # version mismatch under a supervisor: exit *non-zero* so even a legacy
    # Restart=on-failure unit respawns onto the new code (a clean exit left
    # the daemon down after every upgrade until the next login)
    with pytest.raises(SystemExit) as ei:
        _drive_once(monkeypatch)
    assert ei.value.code == daemon.UPGRADE_EXIT_CODE
    assert "package upgraded" in _read_log()


def test_unsupervised_daemon_ignores_version_change(monkeypatch):
    monkeypatch.delenv("ALLE_SERVICE", raising=False)
    monkeypatch.setattr(daemon, "installed_version", lambda: "99.0.0")
    # no supervisor → must NOT self-exit (would stay down); the loop runs and we
    # end it via the sleep-raised KeyboardInterrupt instead
    with pytest.raises(KeyboardInterrupt):
        _drive_once(monkeypatch)


def _read_log() -> str:
    from alle import applog

    return applog.tail(200)


# ---- ownership handoff to a service manager ------------------------------------


def test_ensure_running_defers_to_service_manager(monkeypatch):
    from alle import daemonctl

    monkeypatch.setattr(daemon, "is_running", lambda: False)
    monkeypatch.setattr(daemonctl, "is_installed", lambda: True)
    started = []
    monkeypatch.setattr(daemonctl, "start_service", lambda: started.append(1) or True)
    spawned = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: spawned.append(1))
    daemon.ensure_running()
    assert started == [1] and spawned == []  # supervisor asked, no self-spawn


def test_stop_routes_through_service_manager(monkeypatch):
    from alle import daemonctl

    monkeypatch.setattr(daemonctl, "is_installed", lambda: True)
    monkeypatch.setattr(daemon, "is_running", lambda: True)
    stopped = []
    monkeypatch.setattr(daemonctl, "stop_service", lambda: stopped.append(1) or True)
    killed = []
    monkeypatch.setattr(daemon.os, "kill", lambda *a: killed.append(a))
    assert daemon.stop() is True  # was running
    assert stopped == [1] and killed == []  # supervisor stopped it, no raw signals
