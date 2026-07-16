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
        # Point the privileged-helper socket at a path nothing binds, so no
        # test ever sees a host's real helper (which would make helper.request
        # succeed and the tun privilege gate / singbox running_pid take the
        # helper path instead of the code path under test).
        monkeypatch.setenv("ALLE_HELPER_SOCKET", os.path.join(tmp, "nope.sock"))
        # The opt-in profile knobs must never leak from the invoking shell (or
        # a container CI runner) into tests that assert the defaults.
        for var in ("ALLE_LISTEN", "ALLE_PORT_BASE", "ALLE_SINGBOX", "ALLE_CONTAINER"):
            monkeypatch.delenv(var, raising=False)
        yield


def wg_config(endpoint_host: str = "1.2.3.4") -> dict:
    """A minimal WireGuard-params dict, like the one a provider hands the store.

    Every channel-bearing test needs one; only ``endpoint_host`` differs between
    them (and some tests assert on it), so this keeps the boilerplate structure
    in one place while letting each module pick its own endpoint.
    """
    return {
        "private_key": "PRIV=",
        "address": ["10.5.0.2/32"],
        "peer": {
            "public_key": "PUB=",
            "endpoint_host": endpoint_host,
            "endpoint_port": 51820,
            "preshared_key": None,
            "allowed_ips": ["0.0.0.0/0", "::/0"],
            "keepalive": 25,
        },
    }
