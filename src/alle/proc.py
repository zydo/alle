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


def _process_snapshot(pid: int) -> tuple[str, str] | None:
    """One process's ``(state, start_marker)``, or ``None`` if unreadable.

    Both facts come from a single observation on purpose. They used to be read
    separately — ``ps -o state=`` followed by ``ps -o lstart=`` — which cost
    two process spawns per verification on macOS *and* left a window in which
    the two answers could describe different processes, had the PID been
    recycled between them.

    Linux takes both fields out of the same ``/proc/<pid>/stat`` line; where
    there is no procfs, one ``ps`` prints both columns. The start marker is
    empty when a platform reports a state but no start time; every other
    ambiguity — no such process, unreadable output, a failed or timed-out
    command — is ``None``, which every caller reads as *not ours*.
    """
    if pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # comm (field 2) is in parens and may itself contain spaces/parens;
        # everything unambiguous starts after the *last* ')'.
        fields = stat.rsplit(")", 1)[1].split()
        return (fields[0][:1], f"ticks:{fields[19]}")  # starttime is field 22
    except (OSError, IndexError):
        pass
    try:
        out = subprocess.run(
            [PS, "-p", str(pid), "-o", "state=,lstart="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # "Ss   Mon Jul 28 04:00:00 2026": the state column carries BSD modifier
    # flags (only the leading kernel state means anything) and lstart itself
    # contains spaces, so the split is on the first whitespace run only.
    state, _, start = out.stdout.strip().partition(" ")
    if not state:
        return None  # no such process: ps exits non-zero and prints nothing
    return (state[:1], start.strip())


def process_state_of(pid: int) -> str | None:
    """Kernel process state, or ``None`` when it cannot be determined."""
    snapshot = _process_snapshot(pid)
    return snapshot[0] if snapshot is not None else None


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
    reuse). Elsewhere: ``ps lstart``, the full start timestamp. A zombie has a
    start time but is not a live process, so it reads as ``None``.

    The marker's spelling is a compatibility surface, not an implementation
    detail: a pidfile written before an upgrade must keep verifying after it,
    so both forms stay byte-for-byte what they have always been.
    """
    snapshot = _process_snapshot(pid)
    if snapshot is None or snapshot[0] == "Z":
        return None
    return snapshot[1] or None


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


def read_verified_record(path: Path, markers: tuple[str, ...]) -> dict | None:
    """The verified ``{"pid", "start"}`` record behind a pidfile, or ``None``.

    For callers that need the recorded start marker as well as the pid — the
    sing-box generation marker is exactly that pair, and rebuilding it from a
    second read would mean verifying the same process twice.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    rec = parse_record(text)
    if rec is None or not verify(rec, markers):
        return None
    return rec


def read_pidfile(path: Path, markers: tuple[str, ...]) -> int | None:
    """The verified live PID behind a pidfile, or ``None``.

    ``None`` for a missing/unparseable file, a dead PID, or a live PID whose
    identity does not match what was recorded at spawn.
    """
    rec = read_verified_record(path, markers)
    return rec["pid"] if rec is not None else None
