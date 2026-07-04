"""alled: alle's local background service.

The daemon owns runtime reconciliation and probing for the CLI today and future
Web UI / desktop clients. It keeps the single sing-box process matched to
``state.json`` and continuously probes every channel's connectivity.

It does two jobs on a single loop:

1. **Reconcile** — when the config-relevant part of ``state.json`` changes (a
   channel added/removed/relocated), rebuild the one sing-box config and restart
   it only if it actually changed. A file edit from the application layer is the
   trigger.
2. **Heartbeat probe** — every ``PROBE_INTERVAL`` seconds, route a tiny request
   through each channel's proxy to record its exit IP + latency (or a failure)
   back into ``state.json``. This is what ``alle status`` reads.

It is auto-started by CLI mutations (and by ``alle start``) when not already
running, runs detached with a pidfile, and owns the single sing-box process for
its lifetime. The legacy hidden ``alle applier`` entrypoint remains as an alias.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from alle import applog, paths

POLL_SECONDS = 1.0  # how often we check for a config change
PROBE_INTERVAL = 30.0  # how often each channel is probed
METRICS_INTERVAL = 2.0  # how often traffic counters are sampled from the Clash API


def _pid_path() -> Path:
    return paths.state_dir() / "applier.pid"


def running_pid() -> int | None:
    pf = _pid_path()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)  # liveness check
        return pid
    except (ValueError, OSError):
        return None


def is_running() -> bool:
    return running_pid() is not None


def ensure_running() -> None:
    """Start the applier detached if it isn't already running.

    Called by every CLI mutation so configuring a channel is enough to get it
    applied and probed — there is never a separate "apply" step.
    """
    if is_running():
        return
    if os.environ.get("ALLE_APPLIER"):  # don't recurse from inside the daemon
        return
    env = dict(os.environ, ALLE_APPLIER="1")
    log = paths.state_dir() / "applier.log"
    with open(log, "ab") as lf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "alle", "applier"],
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    _pid_path().write_text(str(proc.pid))
    applog.log("applier started")


def stop() -> bool:
    """Stop the applier (SIGTERM, then SIGKILL). True if one was running."""
    pid = running_pid()
    if pid is None:
        _pid_path().unlink(missing_ok=True)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:  # exited between the liveness check and the signal
        pass
    for _ in range(40):
        if running_pid() is None:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    _pid_path().unlink(missing_ok=True)
    applog.log("applier stopped")
    return True


def run_applier() -> None:
    """Blocking reconcile + probe loop. Run inside the detached daemon process."""
    # Imported here so the lightweight lifecycle helpers above don't pull the
    # engine/sing-box stack into every CLI invocation.
    import fcntl

    from alle import metrics, reconnect, singbox
    from alle.engine import Engine
    from alle.state import Store, config_signature, _read_raw

    # Exclusive instance lock: two CLI mutations racing through ensure_running()
    # can both see "not running" and spawn two appliers — the flock makes the
    # loser exit instead of fighting the winner over the one sing-box process.
    # Held (not closed) for the daemon's lifetime; released by the OS on exit.
    # The holder's pid is kept in the lock file so a losing duplicate can repair
    # the pidfile its spawner just clobbered (otherwise `alle stop` would miss
    # the real daemon).
    instance_lock = open(paths.state_dir() / "applier.lock", "a+")  # noqa: SIM115
    try:
        fcntl.flock(instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            instance_lock.seek(0)
            holder = int(instance_lock.read().strip())
            os.kill(holder, 0)  # liveness check
            _pid_path().write_text(str(holder))
        except (ValueError, OSError):
            pass
        applog.log("applier already running; duplicate exiting")
        return
    instance_lock.truncate(0)
    instance_lock.write(str(os.getpid()))
    instance_lock.flush()

    accumulator = metrics.Accumulator()
    stop_flag = {"stop": False}

    def _handle(_sig, _frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    _pid_path().write_text(str(os.getpid()))
    last_sig = None
    last_probe = 0.0
    last_metrics = 0.0
    try:
        while not stop_flag["stop"]:
            sig = config_signature(_read_raw())
            if sig != last_sig:
                try:
                    Engine(Store.load()).reconcile()
                except Exception as e:  # noqa: BLE001 — one bad state must not kill the loop
                    applog.log(f"reconcile failed: {e}")
                    print(
                        f"applier: reconcile failed: {e}", file=sys.stderr, flush=True
                    )
                last_sig = sig

            now = time.monotonic()
            # Traffic sampling runs on its own faster cadence than probing: the
            # Clash API only reports live connections, so the more often we look
            # the fewer short-lived connections slip through between samples.
            if now - last_metrics >= METRICS_INTERVAL:
                try:
                    runner = singbox.Runner()
                    if runner.is_running():
                        accumulator.observe(runner.connections())
                except Exception as e:  # noqa: BLE001
                    applog.log(f"metrics sample failed: {e}")
                last_metrics = now

            if now - last_probe >= PROBE_INTERVAL:
                try:
                    eng = Engine(Store.load())
                    if eng.store.channels():
                        eng.probe_all()
                        reconnect.run_pass(Store.load(), eng.runner)
                except Exception as e:  # noqa: BLE001
                    applog.log(f"probe cycle failed: {e}")
                last_probe = now

            time.sleep(POLL_SECONDS)
    finally:
        if running_pid() == os.getpid():
            _pid_path().unlink(missing_ok=True)
