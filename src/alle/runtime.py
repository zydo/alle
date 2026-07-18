"""Where alle is running: bare host or container.

Container mode is strictly additive (the Phase-invariant): nothing here ever
changes binds, ports, or lifecycle by itself. ``ALLE_CONTAINER=1`` — set by
the official image — is the authoritative signal; the ``/.dockerenv`` /
``/run/.containerenv`` marker files are advisory detection used only to
*refuse* host-only footguns (login-service install) and to phrase privilege
hints for the container, never to silently switch behavior.
"""

from __future__ import annotations

import os

# Marker files: Docker creates /.dockerenv in every container; podman
# creates /run/.containerenv.
_MARKER_FILES = ("/.dockerenv", "/run/.containerenv")


def in_container() -> bool:
    """True when running inside a container (env flag or marker file)."""
    if os.environ.get("ALLE_CONTAINER"):
        return True
    # Resolve the marker files through the installer's fake-root seam the same
    # way install.sh's root_path() does, so the login-service install path can
    # be exercised against a real systemd --user session inside the Docker
    # smoke harness. Unset in production, so real markers are read verbatim.
    root = os.environ.get("_ALLE_INSTALL_TEST_ROOT", "")
    return any(os.path.exists(root + p) for p in _MARKER_FILES)
