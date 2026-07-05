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
