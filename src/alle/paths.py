"""Runtime state location for alle's generated files (sing-box config, provider lists).

Defaults to ``$HOME/.alle`` so generated artifacts never pollute the project
directory; override with ``$ALLE_HOME``.
"""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    d = Path(os.environ.get("ALLE_HOME") or (Path.home() / ".alle"))
    d.mkdir(parents=True, exist_ok=True)
    return d
