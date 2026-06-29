"""Keep tests hermetic: point alle's state dir at a throwaway directory so
they never read or write the real ``~/.alle``."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("ALLE_HOME", os.path.join(tmp, "state"))
        yield
