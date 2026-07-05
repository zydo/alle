"""Runtime state location for alle's generated files (sing-box config, provider lists).

Defaults to ``$HOME/.alle`` so generated artifacts never pollute the project
directory; override with ``$ALLE_HOME``.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def state_dir() -> Path:
    """The state directory, created/kept owner-only (0700).

    Everything under it — WireGuard private keys in ``state.json`` and the
    sing-box config, provider credentials, logs carrying exit IPs — is private
    to the user, so the directory itself is the first barrier, not just the
    per-file modes.
    """
    d = Path(os.environ.get("ALLE_HOME") or (Path.home() / ".alle"))
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(d.stat().st_mode) & 0o077:
        os.chmod(d, 0o700)  # tighten a pre-existing looser dir too
    return d
