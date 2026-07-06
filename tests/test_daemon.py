"""The applier's change-detection: the config signature is what triggers a
reconcile, so it must move on any channel add/remove/edit and stay put for
probe-only writes — and a *failed* reconcile is retried on a timer even when
the signature never moves."""

from __future__ import annotations

import signal

import pytest

from alle import daemon
from alle.state import Store, _read_raw, config_signature

WG = {
    "private_key": "PRIV=",
    "address": ["10.5.0.2/32"],
    "peer": {
        "public_key": "PUB=",
        "endpoint_host": "se1.example.com",
        "endpoint_port": 51820,
        "preshared_key": None,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "keepalive": 25,
    },
}


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
    v = daemon._installed_version()
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


def test_supervised_daemon_exits_on_version_change(monkeypatch):
    monkeypatch.setenv("ALLE_SERVICE", "1")
    monkeypatch.setattr(daemon, "_installed_version", lambda: "99.0.0")
    # version mismatch under a supervisor: run_applier breaks cleanly (no sleep,
    # so no KeyboardInterrupt) for the supervisor to respawn on new code
    _drive_once(monkeypatch)
    assert "package upgraded" in _read_log()


def test_unsupervised_daemon_ignores_version_change(monkeypatch):
    monkeypatch.delenv("ALLE_SERVICE", raising=False)
    monkeypatch.setattr(daemon, "_installed_version", lambda: "99.0.0")
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
