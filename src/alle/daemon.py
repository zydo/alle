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

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from alle import __version__, applog, paths, proc

POLL_SECONDS = 1.0  # how often we check for a config change
PROBE_INTERVAL = 30.0  # how often each channel is probed
METRICS_INTERVAL = 2.0  # how often traffic counters are sampled from the Clash API
RECONCILE_RETRY = 60.0  # retry a *failed* reconcile this often, sans state change
VERSION_CHECK = 30.0  # how often a supervised daemon checks for an upgraded package

# What the applier's command line looks like: ensure_running() spawns
# ``<python> -m alle applier``; a supervised or hand-run ``alle applier`` shows
# as ``.../bin/alle applier``. Used to reject recycled PIDs.
_MARKERS = ("-m alle", "alle applier")


def _pid_path() -> Path:
    return paths.state_dir() / "applier.pid"


def _info_path() -> Path:
    return paths.state_dir() / "applier.info.json"


def _write_info() -> None:
    """Record the running daemon's pid + version alongside the pidfile.

    Additive to the pidfile (old/new CLI↔daemon combos still parse each other):
    ``alle status`` reads the version here to warn about CLI↔daemon skew after
    an upgrade.
    """
    try:
        _info_path().write_text(
            json.dumps(
                {"pid": os.getpid(), "version": __version__, "at": int(time.time())}
            )
        )
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


def _installed_version() -> str:
    """The alle version currently on disk (re-read fresh, unlike the import-time
    ``__version__``) — differs from ``__version__`` after an in-place upgrade."""
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
    pf = _pid_path()
    try:
        pid = int(pf.read_text().strip())
    except (OSError, ValueError):
        return None
    # PID-recycling guard: believe the pidfile only if the process behind the
    # number really is an applier, so a stale file can neither block a fresh
    # start nor let stop() kill a stranger.
    return pid if proc.alive_matching(pid, _MARKERS) else None


def is_running() -> bool:
    return running_pid() is not None


def in_daemon_process() -> bool:
    """True when code is running in the daemon that owns the Web UI."""
    if not (os.environ.get("ALLE_APPLIER") or os.environ.get("ALLE_SERVICE")):
        return False
    return running_pid() == os.getpid()


def schedule_lifecycle(action: str, delay: float = 0.35) -> None:
    """Run stop/restart shortly after the Web UI response has been flushed."""
    if action not in {"stop", "restart"}:
        raise ValueError(f"unsupported lifecycle action {action!r}")
    env = dict(os.environ)
    env.pop("ALLE_APPLIER", None)
    env.pop("ALLE_SERVICE", None)
    log = paths.state_dir() / "applier.log"
    applog.rotate_if_needed(log, applog.MAX_LOG_BYTES)
    code = (
        "import time\n"
        f"time.sleep({delay!r})\n"
        "from alle import service\n"
        f"service.{action}()\n"
    )
    with open(log, "ab") as lf:
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=lf,
            stderr=lf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
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
            if proc.alive_matching(holder, _MARKERS):
                _pid_path().write_text(str(holder))
        except (ValueError, OSError):
            pass
        applog.log("applier already running; duplicate exiting")
        return
    instance_lock.truncate(0)
    instance_lock.write(str(os.getpid()))
    instance_lock.flush()

    try:
        # The always-on router entrypoint's contract port: allocated once, here,
        # so a fresh install gets its router on the first daemon start. The
        # resulting state change is picked up by the first reconcile below.
        Store.load().ensure_router_port()
    except Exception as e:  # noqa: BLE001 — a full state dir must not kill the daemon
        applog.log(f"router port allocation failed: {e}")

    try:
        # The Web UI control server runs as a thread in this process, so the UI
        # ships and runs with the daemon (nothing extra to deploy).
        from alle.webui import server as webui_server

        webui_server.start_in_thread()
    except Exception as e:  # noqa: BLE001 — the UI is optional; never kill the daemon
        applog.log(f"web ui failed to start: {e}")

    accumulator = metrics.Accumulator()
    stop_flag = {"stop": False}

    def _handle(_sig, _frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    _pid_path().write_text(str(os.getpid()))
    _write_info()
    supervised = bool(os.environ.get("ALLE_SERVICE"))
    last_stamp: tuple[int, int] | None = None
    sig = None
    last_sig = None
    last_probe = 0.0
    last_metrics = 0.0
    last_version_check = 0.0
    reconcile_ok = True
    reconcile_retry_at = 0.0
    try:
        while not stop_flag["stop"]:
            now = time.monotonic()
            # Self-restart on in-place upgrade: only when a supervisor will
            # respawn us on the new code — otherwise exiting would just leave
            # the daemon down until the next CLI call.
            if supervised and now - last_version_check >= VERSION_CHECK:
                last_version_check = now
                if _installed_version() != __version__:
                    applog.log(
                        f"applier: package upgraded {__version__} -> "
                        f"{_installed_version()}; exiting for supervisor respawn"
                    )
                    break
            stamp = _state_stamp()
            if stamp != last_stamp:
                last_stamp = stamp
                sig = config_signature(_read_raw())
            # A failed reconcile is retried on a timer even when the state file
            # hasn't moved — the failure may be environmental (binary download
            # offline, stolen port) and heal without a user edit.
            if sig != last_sig or (not reconcile_ok and now >= reconcile_retry_at):
                try:
                    Engine(Store.load()).reconcile()
                    reconcile_ok = True
                except Exception as e:  # noqa: BLE001 — one bad state must not kill the loop
                    reconcile_ok = False
                    reconcile_retry_at = now + RECONCILE_RETRY
                    applog.log(
                        f"reconcile failed (retrying in {int(RECONCILE_RETRY)}s): {e}"
                    )
                    print(
                        f"applier: reconcile failed: {e}", file=sys.stderr, flush=True
                    )
                last_sig = sig
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
            _info_path().unlink(missing_ok=True)
