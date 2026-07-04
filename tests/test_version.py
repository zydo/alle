"""Package version discovery."""

from __future__ import annotations

import runpy
import sys
import types


def test_version_falls_back_when_package_metadata_is_missing(monkeypatch):
    class PackageNotFoundError(Exception):
        pass

    metadata = types.ModuleType("importlib.metadata")
    metadata.PackageNotFoundError = PackageNotFoundError

    def missing(_name):
        raise PackageNotFoundError

    metadata.version = missing
    monkeypatch.setitem(sys.modules, "importlib.metadata", metadata)

    module = runpy.run_path("src/alle/__init__.py")

    assert module["__version__"] == "0.0.0+unknown"
