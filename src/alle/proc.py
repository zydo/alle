"""Verify what is actually behind a PID before trusting it.

alle addresses its detached processes (sing-box, the applier daemon) by
pidfile. The OS recycles PIDs, so a stale pidfile can point at a live but
unrelated process — a bare ``kill(pid, 0)`` liveness check would then report
alle as running, block a real start, and (worst) let ``stop()`` escalate to
SIGKILL against an innocent process.

Identity is therefore recorded at spawn time as ``{"pid", "start"}`` — the
process's kernel start time (an opaque marker: ``/proc`` starttime ticks on
Linux, ``ps lstart`` elsewhere). A recycled PID necessarily has a different
start time, so matching it proves the process is the very one alle spawned,
not merely one that looks similar. Legacy plain-integer pidfiles (and records
whose start time could not be captured) fall back to the older, weaker
command-line marker check. Every ambiguity resolves to *not ours*: wrongly
reporting "stopped" costs at most a redundant start; wrongly reporting
"running" would let ``stop()`` signal a stranger.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

# `ps` by resolved absolute path: process-identity checks feed kill/stop
# decisions, so they must not exec whatever a mutated PATH puts first. Both
# locations are standard (macOS/BSD: /bin/ps; most Linux: /usr/bin/ps); the
# bare name remains only as a last resort for exotic layouts.
PS = next((p for p in ("/bin/ps", "/usr/bin/ps") if os.path.exists(p)), "ps")


def process_state_of(pid: int) -> str | None:
    """Kernel process state, or ``None`` when it cannot be determined."""
    if pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat.rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(
            [PS, "-p", str(pid), "-o", "state="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip()[:1] or None


def command_of(pid: int) -> str | None:
    """The command line of a live process, or ``None`` if it can't be read.

    Reads ``/proc`` where it exists (Linux) and falls back to ``ps``
    (macOS/BSD, where there is no procfs).
    """
    if pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            return raw.replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        pass
    try:
        out = subprocess.run(
            [PS, "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def start_time_of(pid: int) -> str | None:
    """An opaque start-time marker for a live process, or ``None``.

    Linux: field 22 of ``/proc/<pid>/stat`` (starttime in clock ticks since
    boot — immutable for the process's lifetime and different for any PID
    reuse). Elsewhere: ``ps lstart``, the full start timestamp.
    """
    if pid <= 0:
        return None
    if process_state_of(pid) == "Z":
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # comm (field 2) is in parens and may itself contain spaces/parens;
        # everything unambiguous starts after the *last* ')'.
        fields = stat.rsplit(")", 1)[1].split()
        return f"ticks:{fields[19]}"  # starttime is field 22 overall
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(
            [PS, "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def record(pid: int) -> dict:
    """The identity record to persist for a process alle just spawned.

    Must be captured while the process cannot yet have been reaped (it is our
    unwaited child, so the kernel still holds its PID) — the start time then
    provably belongs to the intended process.
    """
    return {"pid": pid, "start": start_time_of(pid)}


def parse_record(text: str) -> dict | None:
    """A ``{"pid", "start"}`` record from pidfile text, or ``None``.

    Accepts the JSON form written by :func:`write_pidfile` and the legacy
    plain-integer form (which carries no start time).
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, int) and not isinstance(data, bool):
        return {"pid": data, "start": None}  # legacy bare-integer pidfile
    if (
        isinstance(data, dict)
        and isinstance(data.get("pid"), int)
        and not isinstance(data.get("pid"), bool)
    ):
        return {"pid": data["pid"], "start": data.get("start")}
    return None


def verify(rec: dict, markers: tuple[str, ...]) -> bool:
    """True iff the recorded process is alive and is the one alle spawned.

    With a recorded start time, identity is exact: same PID *and* same kernel
    start time. Without one (legacy pidfile, or the marker was unreadable at
    spawn), fall back to requiring a command-line marker hit. Any ambiguity —
    dead PID, unreadable command line, changed start time — reads as *not
    ours* (see the module docstring for why that is the safe direction).
    """
    pid = rec.get("pid") if isinstance(rec, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # PermissionError = exists but not our user = not ours
    except OSError:
        return False
    recorded = rec.get("start")
    if recorded:
        return start_time_of(pid) == recorded
    cmd = command_of(pid)
    return cmd is not None and any(m in cmd for m in markers)


def write_pidfile(path: Path, pid: int) -> None:
    """Persist a spawn-time identity record for later :func:`read_pidfile`."""
    path.write_text(json.dumps(record(pid)))


def read_pidfile(path: Path, markers: tuple[str, ...]) -> int | None:
    """The verified live PID behind a pidfile, or ``None``.

    ``None`` for a missing/unparseable file, a dead PID, or a live PID whose
    identity does not match what was recorded at spawn.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    rec = parse_record(text)
    if rec is None:
        return None
    return rec["pid"] if verify(rec, markers) else None
