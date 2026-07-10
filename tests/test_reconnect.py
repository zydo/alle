"""Auto-reconnect state machine: swallow transient blips, then attempt recovery
with backoff, recover cleanly, and give up on exhaustion or auth errors — all
driven off persisted state with injected time + resolver (no network)."""

from __future__ import annotations

from typing import cast

import pytest

from alle import applog, reconnect, singbox
from alle.providers import ProviderAuthError, ProviderError
from alle.state import Store
from conftest import wg_config

WG = wg_config("us1.example.com")
NEW_WG = wg_config("us2.example.com")

FAIL = {"ok": False, "at": 1, "latency_ms": None, "ip": None, "error": "timeout"}
OK = {"ok": True, "at": 1, "latency_ms": 12.0, "ip": "1.2.3.4", "error": None}


class _Runner:
    def __init__(self):
        self.restarts = 0

    def restart(self):
        self.restarts += 1


@pytest.fixture
def channel():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "United States", "", dict(WG))
    return ch.id


def _pass(now, resolve=None):
    resolve = resolve or (lambda p, c, city: dict(NEW_WG))
    reconnect.run_pass(
        Store.load(), cast(singbox.Runner, _Runner()), now=now, resolve=resolve
    )


def _rc(cid):
    ch = Store.load().get_channel("nordvpn", cid)
    assert ch is not None
    return ch.reconnect


def test_transient_failures_are_swallowed_below_threshold(channel):
    store = Store.load()
    for i in range(reconnect.FAIL_THRESHOLD - 1):
        store.set_probe("nordvpn", channel, dict(FAIL))
        _pass(now=1000 + i)
    rc = _rc(channel)
    assert rc["fails"] == reconnect.FAIL_THRESHOLD - 1
    assert rc.get("attempts", 0) == 0  # no reconnect attempted yet


def test_recovery_clears_all_bookkeeping(channel):
    store = Store.load()
    store.set_probe("nordvpn", channel, dict(FAIL))
    _pass(now=1000)
    assert _rc(channel)["fails"] == 1
    store.set_probe("nordvpn", channel, dict(OK))
    _pass(now=1001)
    assert _rc(channel) == {}  # probe ok -> forget the failures


def test_reaching_threshold_reresolves_and_swaps_wg(channel):
    store = Store.load()
    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        _pass(now=1000 + i)
    ch = Store.load().get_channel("nordvpn", channel)
    assert ch is not None
    assert ch.wg["peer"]["endpoint_host"] == "us2.example.com"  # server re-resolved
    assert ch.reconnect["attempts"] == 1
    assert (
        ch.reconnect["next_at"]
        == int(1000 + reconnect.FAIL_THRESHOLD - 1) + reconnect.BACKOFF[0]
    )


def test_backoff_blocks_a_second_attempt_until_next_at(channel):
    store = Store.load()
    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        _pass(now=1000 + i)
    assert _rc(channel)["attempts"] == 1
    next_at = _rc(channel)["next_at"]
    # still failing, but inside the backoff window -> no new attempt
    store.set_probe("nordvpn", channel, dict(FAIL))
    _pass(now=next_at - 1)
    assert _rc(channel)["attempts"] == 1
    # past the backoff window -> a second attempt
    store.set_probe("nordvpn", channel, dict(FAIL))
    _pass(now=next_at + 1)
    assert _rc(channel)["attempts"] == 2


def test_non_retryable_auth_error_gives_up_immediately(channel):
    store = Store.load()

    def bad_token(p, c, city):
        raise ProviderAuthError("nordvpn token rejected by API (HTTP 401).")

    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        _pass(now=1000 + i, resolve=bad_token)
    rc = _rc(channel)
    assert rc["failed"] is True
    assert "401" in rc["error"]


def test_transient_error_wording_does_not_trigger_giveup(channel):
    # Non-retryability is decided by exception type, never by message text: a
    # transient failure whose message contains "invalid"/"missing" must keep
    # retrying instead of permanently failing the channel.
    store = Store.load()

    def flaky_api(p, c, city):
        raise ProviderError("could not resolve a nordvpn server: invalid response")

    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        _pass(now=1000 + i, resolve=flaky_api)
    rc = _rc(channel)
    assert rc.get("failed") is not True  # still retryable
    assert rc["attempts"] == 1
    assert "invalid response" in rc["error"]


def test_failed_channel_is_left_alone(channel):
    store = Store.load()

    def bad_token(p, c, city):
        raise ProviderAuthError("nordvpn token rejected by API (HTTP 401).")

    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        _pass(now=1000 + i, resolve=bad_token)
    assert _rc(channel)["failed"] is True
    attempts = _rc(channel)["attempts"]
    # further failing cycles must not touch a given-up channel
    store.set_probe("nordvpn", channel, dict(FAIL))
    _pass(now=9999)
    assert _rc(channel)["attempts"] == attempts


def test_gives_up_after_max_attempts(channel):
    store = Store.load()
    now = 1000
    # reach the threshold: first attempt
    for _ in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        _pass(now=now)
        now += 1
    assert _rc(channel)["attempts"] == 1
    # keep jumping past each backoff window until we exhaust MAX_ATTEMPTS
    for _ in range(reconnect.MAX_ATTEMPTS + 2):
        store.set_probe("nordvpn", channel, dict(FAIL))
        now = _rc(channel).get("next_at", now) + 1
        _pass(now=now)
    rc = _rc(channel)
    assert rc["failed"] is True
    assert rc["attempts"] == reconnect.MAX_ATTEMPTS


def test_reconnect_logs_failure_attempt_and_recovery(channel):
    store = Store.load()
    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        _pass(now=1000 + i)
    log = applog.tail()
    assert f"reconnect: nordvpn/{channel} failure 1/3" in log
    assert "still failing after 3/3 probes" in log  # threshold crossing caps the count
    assert "attempt 1/5" in log

    store.set_probe("nordvpn", channel, dict(OK))
    _pass(now=2000)
    log = applog.tail()
    assert f"reconnect: nordvpn/{channel} recovered" in log
    assert "exit_ip=1.2.3.4" in log


def test_stopped_service_is_not_a_channel_failure(channel):
    store = Store.load()
    store.set_probe("nordvpn", channel, {**FAIL, "error": "stopped"})
    _pass(now=1000)
    assert _rc(channel) == {}  # service down != this channel failing


def test_config_provider_restarts_singbox(channel, monkeypatch):
    # Simulate a non-token/config channel: run_pass should restart sing-box
    # rather than re-resolve. Patch the archetype checks for this channel's provider.
    monkeypatch.setattr(reconnect, "is_functional", lambda p: False)
    monkeypatch.setattr(reconnect, "kind", lambda p: "config")
    store = Store.load()
    runner = _Runner()
    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        reconnect.run_pass(
            Store.load(),
            cast(singbox.Runner, runner),
            now=1000 + i,
            resolve=lambda *a: dict(NEW_WG),
        )
    assert runner.restarts == 1
    assert _rc(channel)["attempts"] == 1


def test_config_channels_coalesce_into_one_restart(monkeypatch):
    """However many config channels come due in one pass, sing-box restarts
    at most once — a restart is process-wide, so per-channel restarts would
    bounce every healthy tunnel N times for no benefit."""
    monkeypatch.setattr(reconnect, "is_functional", lambda p: False)
    monkeypatch.setattr(reconnect, "kind", lambda p: "config")
    store = Store.load()
    store.add_provider("protonvpn")
    a, _ = store.upsert_channel("protonvpn", "us_1", "", "", dict(WG))
    b, _ = store.upsert_channel("protonvpn", "us_2", "", "", dict(WG))
    c, _ = store.upsert_channel("protonvpn", "us_3", "", "", dict(WG))
    runner = _Runner()
    # Advance every channel to the threshold together: FAIL_THRESHOLD passes,
    # each setting all three to FAIL before one run_pass. On the final pass all
    # three cross at once — the coalesced case.
    for i in range(reconnect.FAIL_THRESHOLD):
        for cid in (a.id, b.id, c.id):
            store.set_probe("protonvpn", cid, dict(FAIL))
        reconnect.run_pass(Store.load(), cast(singbox.Runner, runner), now=1000 + i)
    assert runner.restarts == 1  # one restart for three due channels
    reloaded = Store.load()
    for cid in (a.id, b.id, c.id):
        ch = reloaded.get_channel("protonvpn", cid)
        assert ch is not None
        assert ch.reconnect["attempts"] == 1  # bookkeeping advanced for each


def test_attempt_bookkeeping_persists_when_resolve_raises(channel):
    """A crash or error mid-attempt must not lose the backoff state — otherwise
    the next pass would treat the attempt as never-made and retry immediately,
    a storm under repeated failure."""

    def boom(*_a, **_k):
        raise ProviderError("api exploded")

    store = Store.load()
    runner = _Runner()
    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        reconnect.run_pass(
            Store.load(), cast(singbox.Runner, runner), now=1000 + i, resolve=boom
        )
    rc = _rc(channel)
    # the attempt was counted and the backoff scheduled even though resolve raised
    assert rc["attempts"] == 1
    assert rc["next_at"] > 1000  # backoff set, so the next pass won't retry at once
    assert "api exploded" in rc["error"]


def test_bookkeeping_persists_when_a_config_restart_fails(monkeypatch, channel):
    """A restart failure is reported but does not lose attempt bookkeeping — the
    next due pass retries (config archetype, so restart is the recovery)."""
    monkeypatch.setattr(reconnect, "is_functional", lambda p: False)
    monkeypatch.setattr(reconnect, "kind", lambda p: "config")

    class _BoomRunner(_Runner):
        def restart(self):
            raise RuntimeError("sing-box refused")

    store = Store.load()
    for i in range(reconnect.FAIL_THRESHOLD):
        store.set_probe("nordvpn", channel, dict(FAIL))
        reconnect.run_pass(
            Store.load(), cast(singbox.Runner, _BoomRunner()), now=1000 + i
        )
    rc = _rc(channel)
    assert rc["attempts"] == 1  # persisted despite the restart raising
    assert "restart failed" in applog.tail()
