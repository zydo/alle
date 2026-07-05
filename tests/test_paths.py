"""The state directory itself is the first privacy barrier: everything under it
(WireGuard keys, credentials, logs with exit IPs) is owner-only, so the dir is
created 0700 and re-tightened if something loosened it."""

from __future__ import annotations

import os
import stat

from alle import paths


def test_state_dir_is_owner_only():
    d = paths.state_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_state_dir_is_retightened_when_loosened():
    d = paths.state_dir()
    os.chmod(d, 0o755)
    assert stat.S_IMODE(paths.state_dir().stat().st_mode) == 0o700
