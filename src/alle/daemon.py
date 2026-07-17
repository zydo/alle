"""alled: alle's local background service.

The daemon owns runtime reconciliation and probing for the CLI today and future
Web UI / desktop clients. It keeps the single sing-box process matched to
``state.json`` and continuously probes every channel's connectivity.

Its single 1 Hz loop drives four duties:

1. **Reconcile** — when the config-relevant part of ``state.json`` changes (a
   channel added/removed/relocated), rebuild the one sing-box config and restart
   it only if it actually changed. A file edit from the application layer is the
   trigger.
2. **Heartbeat probe** — every ``PROBE_INTERVAL`` seconds, route a tiny request
   through each channel's proxy to record its exit IP + latency (or a failure)
   back into ``state.json``. This is what ``alle status`` reads. Runs (with
   auto-reconnect) on its own worker thread so a slow pass never delays a
   reconcile.
3. **Traffic sampling** — every ``METRICS_INTERVAL`` seconds, read the Clash
   API's live connections and bank per-channel byte deltas (see ``metrics``).
4. **Supervision** — every ``SUPERVISE_INTERVAL`` seconds, restart an
   unexpectedly-exited sing-box with capped exponential backoff and publish
   its runtime health into ``applier.info.json``.

It is auto-started by CLI mutations (and by ``alle start``) when not already
running, runs detached with a pidfile, and owns the single sing-box process for
its lifetime. The legacy hidden ``alle applier`` entrypoint remains as an alias.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from alle import __version__, applog, paths, proc

POLL_SECONDS = 1.0  # how often we check for a config change
PROBE_INTERVAL = 30.0  # how often each channel is probed
METRICS_INTERVAL = 2.0  # how often traffic counters are sampled from the Clash API
RECONCILE_RETRY = 60.0  # retry a *failed* reconcile this often, sans state change
VERSION_CHECK = 30.0  # how often a supervised daemon checks for an upgraded package
SUPERVISE_INTERVAL = 2.0  # how often sing-box liveness is checked (sans probes)
CRASH_BACKOFF_MAX = 60.0  # cap between supervised restart attempts
CRASH_RESET = 60.0  # this long alive after a crash forgets the crash history

# Exit code for the intentional self-restart-on-upgrade exit. Non-zero on
# purpose: new units use Restart=always, but units installed before that were
# Restart=on-failure — a clean exit under those left the daemon down after
# every upgrade until the next login.
UPGRADE_EXIT_CODE = 3

# What the applier's command line looks like: ensure_running() spawns
# ``<python> -m alle applier``; a supervised or hand-run ``alle applier`` shows
# as ``.../bin/alle applier``; the foreground form (a container's PID 1) as
# ``.../bin/alle run``. Used to reject recycled PIDs.
_MARKERS = ("-m alle", "alle applier", "alle run")


def _pid_path() -> Path:
    return paths.state_dir() / "applier.pid"


def _info_path() -> Path:
    return paths.state_dir() / "applier.info.json"


def _write_info(runtime: dict | None = None) -> None:
    """Record the running daemon's pid + version alongside the pidfile.

    Additive to the pidfile (old/new CLI↔daemon combos still parse each other):
    ``alle status`` reads the version here to warn about CLI↔daemon skew after
    an upgrade, and the optional ``runtime`` dict is how the loop surfaces a
    degraded sing-box (``{"singbox": <status>, "detail": …}``).
    """
    info = {"pid": os.getpid(), "version": __version__, "at": int(time.time())}
    if runtime is not None:
        info["runtime"] = runtime
    try:
        _info_path().write_text(json.dumps(info))
    except OSError:
        pass


def daemon_info() -> dict | None:
    """The running daemon's ``{pid, version, at}`` info, or None if not running.

    Only trusts the file when its pid is the live daemon, so a stale info file
    from a crashed daemon never reports a phantom version.
    """
    pid = running_pid()
    if pid is None:
        return None
    try:
        info = json.loads(_info_path().read_text())
    except (OSError, ValueError):
        return {"pid": pid, "version": None}
    if info.get("pid") != pid:
        return {"pid": pid, "version": None}
    return info


def installed_version() -> str:
    """The alle version currently on disk (re-read fresh, unlike the import-time
    ``__version__``) — differs from ``__version__`` after an in-place upgrade.

    The canonical version accessor: the single source of truth is
    ``pyproject.toml``, surfaced at runtime via ``importlib.metadata``. The
    Web UI masthead reads this so the badge tracks the installed package
    rather than the daemon's startup snapshot.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("alle-proxy")
    except PackageNotFoundError:
        return __version__


def _state_stamp() -> tuple[int, int]:
    """Cheap change detector for state.json: ``(mtime_ns, size)``.

    The 1 Hz poll compares this before parsing the file — every write replaces
    state.json atomically (new inode, fresh mtime), so an unchanged stamp means
    an unchanged file and the JSON parse + signature hash can be skipped.
    """
    from alle.state import _state_path

    try:
        st = os.stat(_state_path())
    except OSError:
        return (0, 0)
    return (st.st_mtime_ns, st.st_size)


def running_pid() -> int | None:
    # PID-recycling guard: believe the pidfile only if the process behind the
    # number matches the identity recorded at spawn (kernel start time; see
    # alle.proc), so a stale file can neither block a fresh start nor let
    # stop() kill a stranger.
    return proc.read_pidfile(_pid_path(), _MARKERS)


def is_running() -> bool:
    return running_pid() is not None


def in_daemon_process() -> bool:
    """True when code is running in the daemon that owns the Web UI."""
    if not (os.environ.get("ALLE_APPLIER") or os.environ.get("ALLE_SERVICE")):
        return False
    return running_pid() == os.getpid()


def spawn_detached(code: str) -> None:
    """Run ``code`` in a detached interpreter: its own session, output to the
    applier log, daemon markers scrubbed — so the child outlives its spawner
    and never mistakes itself for (or recurses into) the daemon."""
    env = dict(os.environ)
    env.pop("ALLE_APPLIER", None)
    env.pop("ALLE_SERVICE", None)
    log = paths.state_dir() / "applier.log"
    applog.rotate_if_needed(log, applog.MAX_LOG_BYTES)
    with open(log, "ab") as lf:
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )


def schedule_lifecycle(action: str, delay: float = 0.35) -> None:
    """Run stop/restart shortly after the Web UI response has been flushed."""
    if action not in {"stop", "restart"}:
        raise ValueError(f"unsupported lifecycle action {action!r}")
    spawn_detached(
        "import time\n"
        f"time.sleep({delay!r})\n"
        "from alle import service\n"
        f"service.{action}()\n"
    )


def ensure_running() -> None:
    """Start the applier detached if it isn't already running.

    Called by every CLI mutation so configuring a channel is enough to get it
    applied and probed — there is never a separate "apply" step.

    When a login service owns the daemon (launchd/systemd), self-spawning would
    fight the supervisor, so we ask the supervisor to (re)start it instead — it,
    not us, keeps it alive.
    """
    if is_running():
        return
    if os.environ.get("ALLE_APPLIER") or os.environ.get("ALLE_SERVICE"):
        return  # don't recurse from inside the daemon / supervised process
    from alle import daemonctl

    if daemonctl.is_installed():
        daemonctl.start_service()
        return
    env = dict(os.environ, ALLE_APPLIER="1")
    log = paths.state_dir() / "applier.log"
    applog.rotate_if_needed(log, applog.MAX_LOG_BYTES)
    with open(log, "ab") as lf:
        child = subprocess.Popen(
            [sys.executable, "-m", "alle", "applier"],
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    proc.write_pidfile(_pid_path(), child.pid)
    applog.log("applier started")


def stop() -> bool:
    """Stop the applier (SIGTERM, then SIGKILL). True if one was running.

    When a supervisor owns the daemon, signalling it directly is futile —
    KeepAlive/Restart would resurrect it — so route through the service manager,
    which stops it for the session.
    """
    from alle import daemonctl

    if daemonctl.is_installed():
        was = is_running()
        daemonctl.stop_service()
        _info_path().unlink(missing_ok=True)
        _pid_path().unlink(missing_ok=True)
        applog.log("applier stopped (via service manager)")
        return was
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
    _info_path().unlink(missing_ok=True)
    applog.log("applier stopped")
    return True


def run_applier() -> None:
    """Blocking reconcile + probe loop. Run inside the detached daemon process."""
    # Imported here so the lightweight lifecycle helpers above don't pull the
    # engine/sing-box stack into every CLI invocation.
    import fcntl

    from alle import metrics, reconnect, singbox
    from alle.engine import Engine
    from alle.state import Store, StoreReadError, config_signature, _read_raw

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
            holder = proc.parse_record(instance_lock.read())
            if holder and proc.verify(holder, _MARKERS):
                _pid_path().write_text(json.dumps(holder))
        except OSError:
            pass
        applog.log("applier already running; duplicate exiting")
        return
    instance_lock.truncate(0)
    instance_lock.write(json.dumps(proc.record(os.getpid())))
    instance_lock.flush()

    try:
        # A compound setup change (token update, bundle apply, …) that crashed
        # before its commit point leaves a rollback journal; heal it before
        # the first reconcile reads a half-changed setup.
        from alle import txn

        txn.recover()
    except Exception as e:  # noqa: BLE001 — recovery is best-effort at startup
        applog.log(f"setup-journal recovery failed: {e}")

    try:
        # The always-on router entrypoint's contract port: allocated once, here,
        # so a fresh install gets its router on the first daemon start. The
        # resulting state change is picked up by the first reconcile below.
        Store.load().ensure_router_port()
    except Exception as e:  # noqa: BLE001 — a full state dir must not kill the daemon
        applog.log(f"router port allocation failed: {e}")

    try:
        # The control API server (REST API + Web UI) runs as a thread in this
        # process, so it ships and runs with the daemon (nothing extra to deploy).
        from alle.api import server as api_server

        api_server.start_in_thread()
    except Exception as e:  # noqa: BLE001 — the API is optional; never kill the daemon
        applog.log(f"api failed to start: {e}")

    accumulator = metrics.Accumulator()
    stop_flag = {"stop": False}

    def _handle(_sig, _frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    proc.write_pidfile(_pid_path(), os.getpid())

    runtime_state: dict = {"cur": None}

    def _set_runtime(status: str, detail: str = "") -> None:
        """Publish the sing-box runtime status into the info file (on change).

        Read back by ``alle status`` / the Web UI via :func:`daemon_info` —
        this is where "degraded" and "crash-looping" become visible outside
        the log. Detail is clipped to one line; the full text is in applog.
        """
        detail = detail.splitlines()[0][:200] if detail else ""
        if runtime_state["cur"] == (status, detail):
            return
        runtime_state["cur"] = (status, detail)
        _write_info({"singbox": status, "detail": detail})

    _set_runtime("starting")

    def _probe_pass() -> None:
        """One probe + reconnect pass. Runs on its own thread so a slow pass
        (many dead channels) can never delay a reconcile — kill-switch and
        config changes keep applying within one poll interval regardless."""
        try:
            eng = Engine(Store.load())
            # Only enabled channels are probed/reconnected; when every channel
            # is disabled the pass is a no-op (no empty probe log each cycle).
            if any(ch.enabled for ch in eng.store.channels()):
                eng.probe_all()
                reconnect.run_pass(Store.load(), eng.runner)
        except Exception as e:  # noqa: BLE001 — a bad pass must not kill the worker
            applog.log(f"probe cycle failed: {e}")

    probe_worker: dict = {"thread": None}

    # The self-exit-on-upgrade only makes sense when a supervisor respawns us
    # onto *new code*. A container image is immutable — the installed version
    # cannot change under a running container, and exiting would just make the
    # restart policy relaunch the same code — so the check is skipped there.
    from alle import runtime

    supervised = bool(os.environ.get("ALLE_SERVICE")) and not runtime.in_container()
    last_stamp: tuple[int, int] | None = None
    sig = None
    last_sig = None
    last_probe = 0.0
    last_metrics = 0.0
    last_version_check = 0.0
    reconcile_ok = True
    reconcile_retry_at = 0.0
    read_error: str | None = None
    expected_running = False  # a reconcile succeeded → sing-box should be up
    last_supervise = float("-inf")
    crashes = 0
    last_crash_at = 0.0
    next_restart_at = 0.0
    try:
        while not stop_flag["stop"]:
            now = time.monotonic()
            # Self-restart on in-place upgrade: only when a supervisor will
            # respawn us on the new code — otherwise exiting would just leave
            # the daemon down until the next CLI call.
            if supervised and now - last_version_check >= VERSION_CHECK:
                last_version_check = now
                if installed_version() != __version__:
                    applog.log(
                        f"applier: package upgraded {__version__} -> "
                        f"{installed_version()}; exiting for supervisor respawn"
                    )
                    # non-zero so even a Restart=on-failure unit (pre-
                    # Restart=always installs) respawns onto the new code;
                    # the finally block below still cleans up the pidfile
                    raise SystemExit(UPGRADE_EXIT_CODE)
            stamp = _state_stamp()
            if stamp != last_stamp:
                # An unreadable state file must not kill the loop (or be
                # mistaken for an empty one — see StoreReadError): keep the
                # previous signature and retry next poll, logging the failure
                # once per distinct error rather than at 1 Hz.
                try:
                    sig = config_signature(_read_raw())
                except StoreReadError as e:
                    if str(e) != read_error:
                        read_error = str(e)
                        applog.log(f"state unreadable (will retry): {e}")
                else:
                    last_stamp = stamp
                    read_error = None
            # A failed reconcile is retried on a timer even when the state file
            # hasn't moved — the failure may be environmental (binary download
            # offline, stolen port) and heal without a user edit. A *rejected*
            # config is deterministic (same state, same config, same refusal),
            # so it is retried only on the next state change — never a storm.
            if sig != last_sig or (not reconcile_ok and now >= reconcile_retry_at):
                try:
                    Engine(Store.load()).reconcile()
                    reconcile_ok = True
                    expected_running = True
                    _set_runtime("ok")
                except singbox.ConfigRejectedError as e:
                    reconcile_ok = False
                    reconcile_retry_at = float("inf")
                    applog.log(
                        "reconcile: sing-box rejected the generated config — "
                        f"keeping the last known-good one until state changes: {e}"
                    )
                    print(f"applier: config rejected: {e}", file=sys.stderr, flush=True)
                    _set_runtime("config_rejected", str(e))
                except Exception as e:  # noqa: BLE001 — one bad state must not kill the loop
                    reconcile_ok = False
                    reconcile_retry_at = now + RECONCILE_RETRY
                    applog.log(
                        f"reconcile failed (retrying in {int(RECONCILE_RETRY)}s): {e}"
                    )
                    print(
                        f"applier: reconcile failed: {e}", file=sys.stderr, flush=True
                    )
                    _set_runtime("degraded", str(e))
                last_sig = sig

            # Supervision: sing-box liveness independent of probes (which only
            # *record* "stopped") — an unexpected exit is restarted with capped
            # exponential backoff so a crash-looping config can't start a storm.
            if now - last_supervise >= SUPERVISE_INTERVAL:
                last_supervise = now
                if singbox.Runner().is_running():
                    if crashes and now - last_crash_at >= CRASH_RESET:
                        crashes = 0  # stable again — forget the crash history
                        _set_runtime("ok")
                elif expected_running and now >= next_restart_at:
                    crashes += 1
                    last_crash_at = now
                    delay = min(2.0 ** (crashes - 1), CRASH_BACKOFF_MAX)
                    next_restart_at = now + delay
                    _set_runtime(
                        "crash_looping" if crashes >= 3 else "crashed",
                        f"{crashes} unexpected exit(s); restarting",
                    )
                    applog.log(
                        f"sing-box exited unexpectedly (crash {crashes}); "
                        f"restarting (next attempt in {int(delay)}s if it "
                        "crashes again)"
                    )
                    try:
                        Engine(Store.load()).reconcile()
                        applog.log("sing-box restarted after unexpected exit")
                    except Exception as e:  # noqa: BLE001
                        if singbox.Runner().is_running():
                            applog.log(
                                "sing-box restarted on the last known-good "
                                f"config (desired config still failing: {e})"
                            )
                        else:
                            applog.log(f"supervised restart failed: {e}")
            # Traffic sampling runs on its own faster cadence than probing: the
            # Clash API only reports live connections, so the more often we look
            # the fewer short-lived connections slip through between samples.
            if now - last_metrics >= METRICS_INTERVAL:
                try:
                    runner = singbox.Runner()
                    if runner.is_running():
                        # generation keys the counter watermarks: a restarted
                        # sing-box re-baselines instead of reading its fresh
                        # counters as deltas; a failed sample (None) banks
                        # nothing and keeps the watermarks.
                        accumulator.observe(
                            runner.connections(), generation=runner.generation()
                        )
                except Exception as e:  # noqa: BLE001
                    applog.log(f"metrics sample failed: {e}")
                last_metrics = now

            if now - last_probe >= PROBE_INTERVAL:
                worker = probe_worker["thread"]
                if worker is None or not worker.is_alive():
                    worker = threading.Thread(
                        target=_probe_pass, name="alle-probe", daemon=True
                    )
                    probe_worker["thread"] = worker
                    worker.start()
                # else: the previous pass is still running — skip this tick
                # rather than stack passes (each pass is internally bounded).
                last_probe = now

            time.sleep(POLL_SECONDS)
    finally:
        if running_pid() == os.getpid():
            _pid_path().unlink(missing_ok=True)
            _info_path().unlink(missing_ok=True)
