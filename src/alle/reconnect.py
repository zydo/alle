"""Automatic recovery for channels the heartbeat probe finds dead.

WireGuard is connectionless, so a channel can go silently dead — an ISP blip, the
provider rotating the server out, a NAT mapping expiring — while its config still
looks fine. The daemon's probe already detects this (``probe.ok == False``); this
module is the recovery layer that runs right after each probe cycle.

The state machine per channel lives in ``state.json`` under the channel's
``reconnect`` dict, so it survives a daemon restart:

    fails     consecutive probe failures since the last success
    attempts  reconnect attempts already made
    last_at   epoch of the last attempt
    next_at   epoch before which we won't retry (exponential backoff)
    failed    True once we've permanently given up
    error     why we gave up (shown by ``alle status``)

Flow: swallow the first ``FAIL_THRESHOLD`` failures (transient blips self-heal),
then start attempting a reconnect, backing off between tries, until the channel
recovers (probe succeeds → state cleared) or we exhaust ``MAX_ATTEMPTS`` / hit a
non-retryable auth error → ``failed``. A ``failed`` channel is left alone until a
human intervenes (``alle restart`` clears every flag; re-adding a channel starts
it fresh).

Recovery action depends on the provider archetype:

* token/API (NordVPN): re-resolve a fresh server via ``provider_wg`` and overwrite
  the channel's ``wg`` params. That moves the config signature, so the daemon
  reconciles and sing-box reloads onto the new server.
* config (ProtonVPN): no API to re-resolve, so force a sing-box restart to shake
  out transient issues; a permanently bad imported server surfaces as ``failed``.
"""

from __future__ import annotations

import time

from alle import applog
from alle.providers import ProviderError, is_functional, kind, provider_wg
from alle.state import Store
from alle.singbox import Runner

FAIL_THRESHOLD = (
    3  # consecutive probe failures before we start reconnecting (~90s @ 30s)
)
MAX_ATTEMPTS = 5  # give up after this many reconnect attempts
BACKOFF = [30, 60, 120, 300, 600]  # seconds between successive attempts


def _backoff(attempt: int) -> int:
    """Seconds to wait after the ``attempt``-th reconnect (0-indexed), capped."""
    return BACKOFF[min(attempt, len(BACKOFF) - 1)]


def _is_non_retryable(err: Exception) -> bool:
    """True for auth-class failures that retrying can never fix (bad/missing token)."""
    msg = str(err).lower()
    return any(
        marker in msg
        for marker in ("401", "rejected", "not authenticated", "missing", "invalid")
    )


def _probe_summary(probe: dict) -> str:
    bits = []
    if probe.get("ip"):
        bits.append(f"exit_ip={probe['ip']}")
    if probe.get("latency_ms") is not None:
        bits.append(f"latency={probe['latency_ms']}ms")
    if probe.get("error"):
        bits.append(f"error={probe['error']}")
    return ", ".join(bits) if bits else "no probe detail"


def run_pass(
    store: Store, runner: Runner, *, now: float | None = None, resolve=provider_wg
) -> None:
    """Advance the reconnect state machine one step for every channel.

    Call once per probe cycle, after probe results are persisted. ``now`` and
    ``resolve`` are injectable for tests.
    """
    now = time.time() if now is None else now
    for ch in store.channels():
        probe = ch.probe or {}
        rc = dict(ch.reconnect or {})

        if probe.get("ok"):
            if rc:  # recovered — forget all the failure bookkeeping
                applog.log(
                    f"reconnect: {ch.provider}/{ch.id} recovered after "
                    f"{rc.get('fails', 0)} failed probe(s) — {_probe_summary(probe)}"
                )
                store.set_reconnect(ch.provider, ch.id, {})
            continue
        if probe.get("error") == "stopped":
            continue  # the whole service is down, not this channel — nothing to do
        if rc.get("failed"):
            continue  # already gave up; wait for a human (alle restart / re-add)

        rc["fails"] = rc.get("fails", 0) + 1
        if rc["fails"] < FAIL_THRESHOLD:
            applog.log(
                f"reconnect: {ch.provider}/{ch.id} failure {rc['fails']}/{FAIL_THRESHOLD} — "
                f"{probe.get('error') or 'probe failed'}"
            )
            store.set_reconnect(ch.provider, ch.id, rc)
            continue
        if now < rc.get("next_at", 0):
            store.set_reconnect(ch.provider, ch.id, rc)  # still backing off
            continue

        attempts = rc.get("attempts", 0)
        if attempts >= MAX_ATTEMPTS:
            rc["failed"] = True
            last_error = probe.get("error") or "probe failed"
            rc["error"] = f"gave up after {attempts} reconnect attempts"
            store.set_reconnect(ch.provider, ch.id, rc)
            applog.log(
                f"reconnect: {ch.provider}/{ch.id} failed — {rc['error']}; last probe: {last_error}"
            )
            continue

        applog.log(
            f"reconnect: {ch.provider}/{ch.id} still failing after "
            f"{min(rc['fails'], FAIL_THRESHOLD)}/{FAIL_THRESHOLD} probes — "
            f"attempt {attempts + 1}/{MAX_ATTEMPTS} due"
        )
        _attempt(store, runner, ch, rc, now, attempts, resolve)


def _attempt(store, runner, ch, rc, now, attempts, resolve) -> None:
    """Make one reconnect attempt for a channel and record the outcome."""
    rc["attempts"] = attempts + 1
    rc["last_at"] = int(now)
    rc["next_at"] = int(now) + _backoff(attempts)
    try:
        if is_functional(ch.provider) and kind(ch.provider) == "token":
            wg = resolve(ch.provider, ch.country, ch.city or "")
            store.update_channel_wg(ch.provider, ch.id, wg)
            applog.log(
                f"reconnect: {ch.provider}/{ch.id} attempt {rc['attempts']}/{MAX_ATTEMPTS} — "
                f"re-resolved server; reload pending; next retry in {_backoff(attempts)}s if still unhealthy"
            )
        else:
            runner.restart()
            applog.log(
                f"reconnect: {ch.provider}/{ch.id} attempt {rc['attempts']}/{MAX_ATTEMPTS} — "
                f"restarted sing-box; next retry in {_backoff(attempts)}s if still unhealthy"
            )
    except ProviderError as e:
        if _is_non_retryable(e):
            rc["failed"] = True
            rc["error"] = str(e)
            applog.log(f"reconnect: {ch.provider}/{ch.id} permanent failure — {e}")
        else:
            rc["error"] = str(e)
            applog.log(
                f"reconnect: {ch.provider}/{ch.id} attempt {rc['attempts']}/{MAX_ATTEMPTS} failed — {e}"
            )
    store.set_reconnect(ch.provider, ch.id, rc)
