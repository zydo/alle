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
  Restarts are **coalesced**: however many config channels come due in one
  pass, sing-box restarts at most once — a restart is process-wide, so per-
  channel restarts would bounce every healthy tunnel N times for no benefit.

Bookkeeping durability: each attempt's counters (attempts/backoff timestamps)
are persisted even when the attempt itself raises, so a crash or an error
mid-attempt can never lose the backoff state and turn into a retry storm.
"""

from __future__ import annotations

import secrets
import time

from alle import applog, credentials
from alle.providers import (
    ProviderAuthError,
    ProviderError,
    is_functional,
    kind,
    provider_resolver,
    provider_wg,
)
from alle.state import Store, channel_fingerprint
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
    """True for auth-class failures that retrying can never fix (bad/missing token).

    Decided by exception *type*, not message text — a substring heuristic would
    permanently fail a channel over a transient error whose message merely
    contains a word like "invalid"."""
    return isinstance(err, ProviderAuthError)


def _probe_summary(probe: dict) -> str:
    bits = []
    if probe.get("ip"):
        bits.append(f"exit_ip={probe['ip']}")
    if probe.get("latency_ms") is not None:
        bits.append(f"latency={probe['latency_ms']}ms")
    if probe.get("error"):
        bits.append(f"error={probe['error']}")
    return ", ".join(bits) if bits else "no probe detail"


def _killswitch_diagnostic(store: Store) -> str:
    """A targeted note when alle's own recovery egress is policy-blocked.

    With TUN + kill switch on and the tunnel down, alle's provider-API calls
    are captured and rejected like any other unmatched traffic — that is the
    fail-closed design, deliberately kept: every narrow carve-out evaluated
    (interpreter process-path route rules, SO_MARK exceptions) is reusable by
    or unavailable to application traffic somewhere in the host/container
    privilege matrix (see docs/security.md). So instead of silently lifting
    the policy, the failure is *named*: recovery needs the tunnel back, a
    human decision, or a reachable healthy channel.
    """
    router = store.router
    if router.get("tun") and router.get("killswitch"):
        return (
            " (TUN + kill switch are on: with the tunnel down, alle's own "
            "provider-API egress is blocked too — fail-closed by design; "
            "recovery needs the tunnel back, or a deliberate policy lift: "
            "alle routes killswitch off)"
        )
    return ""


def run_pass(
    store: Store, runner: Runner, *, now: float | None = None, resolve=provider_wg
) -> None:
    """Advance the reconnect state machine one step for every channel.

    Call once per probe cycle, after probe results are persisted. ``now`` and
    ``resolve`` are injectable for tests. Config-archetype recoveries are
    coalesced into at most ONE sing-box restart at the end of the pass, after
    every channel's bookkeeping is already persisted — a restart failure
    costs nothing but this pass's shake-out attempt.
    """
    now = time.time() if now is None else now
    updates: dict = {}
    actions = {}
    messages = {}
    for ch in store.channels():
        if not ch.enabled:
            # Never re-resolve or re-materialise a disabled channel — that
            # would dial the provider again. Disabling also cleared its probe/
            # reconnect bookkeeping, so there is nothing stale to act on.
            continue
        probe = ch.probe or {}
        rc = dict(ch.reconnect or {})

        if probe.get("ok"):
            if rc:  # recovered — forget all the failure bookkeeping
                messages[(ch.provider, ch.id)] = (
                    f"reconnect: {ch.provider}/{ch.id} recovered after "
                    f"{rc.get('fails', 0)} failed probe(s) — {_probe_summary(probe)}"
                )
                updates[(ch.provider, ch.id)] = (channel_fingerprint(ch), rc, {})
            continue
        if probe.get("error") == "stopped":
            continue  # the whole service is down, not this channel — nothing to do
        if rc.get("failed"):
            continue  # already gave up; wait for a human (alle restart / re-add)

        rc["fails"] = rc.get("fails", 0) + 1
        if rc["fails"] < FAIL_THRESHOLD:
            messages[(ch.provider, ch.id)] = (
                f"reconnect: {ch.provider}/{ch.id} failure {rc['fails']}/{FAIL_THRESHOLD} — "
                f"{probe.get('error') or 'probe failed'}"
            )
            updates[(ch.provider, ch.id)] = (
                channel_fingerprint(ch),
                dict(ch.reconnect or {}),
                rc,
            )
            continue
        if now < rc.get("next_at", 0):
            updates[(ch.provider, ch.id)] = (
                channel_fingerprint(ch),
                dict(ch.reconnect or {}),
                rc,
            )
            continue

        attempts = rc.get("attempts", 0)
        if attempts >= MAX_ATTEMPTS:
            rc["failed"] = True
            last_error = probe.get("error") or "probe failed"
            rc["error"] = f"gave up after {attempts} reconnect attempts"
            updates[(ch.provider, ch.id)] = (
                channel_fingerprint(ch),
                dict(ch.reconnect or {}),
                rc,
            )
            messages[(ch.provider, ch.id)] = (
                f"reconnect: {ch.provider}/{ch.id} failed — {rc['error']}; last probe: {last_error}"
            )
            continue

        nonce = secrets.token_hex(16)
        rc["attempts"] = attempts + 1
        rc["last_at"] = int(now)
        rc["next_at"] = int(now) + _backoff(attempts)
        rc["attempt_nonce"] = nonce
        messages[(ch.provider, ch.id)] = (
            f"reconnect: {ch.provider}/{ch.id} still failing after "
            f"{min(rc['fails'], FAIL_THRESHOLD)}/{FAIL_THRESHOLD} probes — "
            f"attempt {attempts + 1}/{MAX_ATTEMPTS} due"
        )
        ref = (ch.provider, ch.id)
        fingerprint = channel_fingerprint(ch)
        updates[ref] = (fingerprint, dict(ch.reconnect or {}), rc)
        actions[ref] = (ch, dict(rc), fingerprint, nonce, attempts)

    committed_order = store.prepare_reconnects(updates)
    committed = set(committed_order)
    for ref in committed_order:
        if ref in messages:
            applog.log(messages[ref])

    restart_refs: list[str] = []
    resolver_cache = {}
    stored_credentials = credentials.snapshot() if resolve is provider_wg else {}

    def resolve_channel(ch):
        if resolve is not provider_wg:
            return resolve(ch.provider, ch.country, ch.city or "")
        if ch.provider not in resolver_cache:
            try:
                resolver_cache[ch.provider] = provider_resolver(
                    ch.provider, stored_credentials.get(ch.provider) or {}
                )
            except ProviderError as error:
                resolver_cache[ch.provider] = error
        provider_resolve = resolver_cache[ch.provider]
        if isinstance(provider_resolve, ProviderError):
            raise provider_resolve
        return provider_resolve(ch.country, ch.city or "")

    for ref, action in actions.items():
        if ref not in committed:
            continue
        ch, rc, fingerprint, nonce, attempts = action
        if is_functional(ch.provider) and kind(ch.provider) == "token":
            try:
                wg = resolve_channel(ch)
                if not store.finish_reconnect_attempt(
                    ch.provider, ch.id, fingerprint, nonce, rc, wg=wg
                ):
                    applog.log(
                        f"reconnect: {ch.provider}/{ch.id} result discarded — "
                        "channel changed during resolution"
                    )
                    continue
                applog.log(
                    f"reconnect: {ch.provider}/{ch.id} attempt {rc['attempts']}/{MAX_ATTEMPTS} — "
                    f"re-resolved server; reload pending; next retry in {_backoff(attempts)}s if still unhealthy"
                )
            except ProviderError as e:
                if _is_non_retryable(e):
                    rc["failed"] = True
                    rc["error"] = str(e)
                    message = (
                        f"reconnect: {ch.provider}/{ch.id} permanent failure — {e}"
                    )
                else:
                    rc["error"] = str(e)
                    message = (
                        f"reconnect: {ch.provider}/{ch.id} attempt "
                        f"{rc['attempts']}/{MAX_ATTEMPTS} failed — {e}"
                        + _killswitch_diagnostic(store)
                    )
                if store.finish_reconnect_attempt(
                    ch.provider, ch.id, fingerprint, nonce, rc
                ):
                    applog.log(message)
        else:
            restart_refs.append(f"{ch.provider}/{ch.id}")
            applog.log(
                f"reconnect: {ch.provider}/{ch.id} attempt {rc['attempts']}/{MAX_ATTEMPTS} — "
                f"sing-box restart queued; next retry in {_backoff(attempts)}s if still unhealthy"
            )

    if restart_refs:
        try:
            runner.restart()
            applog.log(
                "reconnect: restarted sing-box once for "
                f"{len(restart_refs)} channel(s): {', '.join(restart_refs)}"
            )
        except Exception as e:  # noqa: BLE001 — bookkeeping is already persisted
            applog.log(
                f"reconnect: sing-box restart failed — {e} "
                "(attempt bookkeeping kept; the next due pass retries)"
            )
