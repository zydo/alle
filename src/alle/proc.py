"""Verify what is actually behind a PID before trusting it.

alle addresses its detached processes (sing-box, the applier daemon) by
pidfile. The OS recycles PIDs, so a stale pidfile can point at a live but
unrelated process — a bare ``kill(pid, 0)`` liveness check would then report
alle as running, block a real start, and (worst) let ``stop()`` escalate to
SIGKILL against an innocent process. Every pidfile read therefore also checks
that the process's command line looks like the one alle spawned.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


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
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def alive_matching(pid: int, markers: tuple[str, ...]) -> bool:
    """True iff ``pid`` is a live process whose command line contains a marker.

    Deliberately strict when the command line cannot be determined: the process
    is treated as *not ours*. Wrongly reporting "stopped" costs at most a
    redundant start; wrongly reporting "running" keeps a stale pidfile trusted
    and would let ``stop()`` signal a stranger.
    """
    try:
        os.kill(pid, 0)  # PermissionError = exists but not our user = not ours
    except OSError:
        return False
    cmd = command_of(pid)
    return cmd is not None and any(m in cmd for m in markers)
