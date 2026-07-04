"""alle's own operation log: ``~/.alle/alle.log``.

This is *alle's* record of what it did — providers/channels added or removed,
heartbeat probes, reconciles, sing-box binary downloads, ``up``/``down`` — not
sing-box's logging (that lives in ``singbox.log`` and surfaces elsewhere). Both
the CLI and the applier daemon append here, so a user running ``alle logs -f``
sees a single timeline of everything happening.

Appends are line-oriented and O_APPEND, which is atomic for the short lines we
write, so concurrent writers from different processes don't interleave.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from alle import paths


def _log_path() -> Path:
    return paths.state_dir() / "alle.log"


def log(message: str) -> None:
    """Append one timestamped line. Best-effort: never raises into the caller."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}\n"
    try:
        with open(_log_path(), "a") as f:
            f.write(line)
    except OSError:
        pass


def tail(n: int = 200) -> str:
    p = _log_path()
    if not p.exists():
        return "(no logs yet)"
    lines = p.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:]) or "(no logs yet)"


def follow(n: int = 50) -> None:
    """Print the last ``n`` lines then stream new ones (``alle logs -f``).

    Blocks until interrupted. Re-opens the file if it is rotated/recreated.
    """
    p = _log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)
    # seed with the tail
    existing = p.read_text(errors="replace").splitlines()
    for ln in existing[-n:]:
        print(ln, flush=True)
    f = open(p)
    try:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                print(line.rstrip("\n"), flush=True)
                continue
            # nothing new; detect truncation/rotation then wait briefly
            time.sleep(0.4)
            try:
                if p.stat().st_size < f.tell():
                    f.close()
                    f = open(p)
            except OSError:
                pass
    finally:
        f.close()
