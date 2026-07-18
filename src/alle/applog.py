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
import sys
import time
from pathlib import Path

from alle import fsio, paths


MAX_LOG_BYTES = 5 * 1024 * 1024  # rotate a log file that grows past this
TAIL_BLOCK_BYTES = 8192

# Foreground mode (`alle run`, the container's PID 1): every log line is also
# written to stderr so `docker logs` sees the same timeline as `alle logs`.
# Off by default — background daemons and CLI calls keep file-only logging.
echo_stderr = False


def _log_path() -> Path:
    return paths.state_dir() / "alle.log"


def rotate_if_needed(p: Path, max_bytes: int) -> None:
    """Move ``p`` to ``<p>.1`` once it exceeds ``max_bytes`` (one backup kept).

    Keeps append-forever logs (this one, sing-box's) from growing without
    bound. Concurrent writers can race the rename; worst case a few lines land
    in the rotated file, which is fine for an operations log.
    """
    try:
        if p.stat().st_size >= max_bytes:
            os.replace(p, p.with_name(p.name + ".1"))
    except OSError:
        pass


def log(message: str) -> None:
    """Append one timestamped line. Best-effort: never raises into the caller.

    The message is sanitized (ANSI/control sequences stripped) because the
    file is replayed raw into terminals by ``alle logs``/``logs -f`` — a
    hostile provider/user label must not clear the screen there, and an
    embedded newline must not forge extra log lines.
    """
    from alle import output  # function-level: applog is imported by everything

    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {output.sanitize_text(message)}\n"
    if echo_stderr:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except OSError:
            pass
    try:
        p = _log_path()
        rotate_if_needed(p, MAX_LOG_BYTES)
        existed = p.exists()
        with open(p, "a") as f:
            f.write(line)
        if not existed:
            # a root-run CLI (docker exec, sudo) creating the log must not
            # own it — the unprivileged daemon appends here too
            fsio._preserve_owner(p, p.parent)
    except OSError:
        pass


def reverse_tail(path: Path, n: int, block_size: int = TAIL_BLOCK_BYTES) -> list[str]:
    """Return the final ``n`` text lines without reading the complete file.

    The path is opened once, so a concurrent rotation cannot mix generations.
    Invalid UTF-8 is replaced just like the previous ``read_text`` behavior.
    """
    if n <= 0:
        return []
    chunks: list[bytes] = []
    newlines = 0
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            while position > 0 and newlines <= n:
                size = min(block_size, position)
                position -= size
                stream.seek(position)
                chunk = stream.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
    except OSError:
        return []
    text = b"".join(reversed(chunks)).decode(errors="replace")
    return text.splitlines()[-n:]


def tail(n: int = 200) -> str:
    return "\n".join(reverse_tail(_log_path(), n)) or "(no logs yet)"


def follow(n: int = 50) -> None:
    """Print the last ``n`` lines then stream new ones (``alle logs -f``).

    Blocks until interrupted. Re-opens the file if it is rotated/recreated.
    """
    p = _log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch(exist_ok=True)
    # seed with the tail
    for ln in reverse_tail(p, n):
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
