"""Hermetic test homes plus explicit ownership of background runtimes."""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import pytest

from alle import daemon, singbox
from _runtime_hygiene import RuntimeHandle, RuntimeSession


@pytest.fixture(scope="session")
def runtime_session():
    """The one marked root that owns every per-test home and child PID."""
    session = RuntimeSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def runtime_guard(monkeypatch, runtime_session, shared_singbox):
    """Make daemon spawning inert unless a test requests the capability.

    Cleanup precedes monkeypatch teardown, so even a failing test cannot leave
    its detached applier or adopted sing-box behind in a deleted temp home.
    """
    home = runtime_session.new_home()
    monkeypatch.setenv("ALLE_HOME", str(home))
    monkeypatch.setenv("ALLE_TEST_HOME", str(home))
    monkeypatch.setenv("ALLE_TEST_SESSION", runtime_session.token)
    # Point the privileged-helper socket at a path nothing binds, so no test
    # ever sees or signals the developer's real helper/runtime.
    monkeypatch.setenv("ALLE_HELPER_SOCKET", str(home.parent / "no-helper.sock"))
    # Opt-in profile knobs must never leak from the invoking shell into a test.
    for var in (
        "ALLE_LISTEN",
        "ALLE_PORT_BASE",
        "ALLE_CONTAINER",
        "ALLE_APPLIER",
        "ALLE_SERVICE",
        "ALLE_GATEWAY",
        "_ALLE_INSTALL_TEST_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALLE_SINGBOX", str(shared_singbox))
    handle = RuntimeHandle(runtime_session, home, daemon.ensure_running)
    monkeypatch.setattr(daemon, "ensure_running", handle.dispatch)
    try:
        yield handle
    finally:
        handle.stop()


@pytest.fixture
def background_runtime(runtime_guard):
    """Allow code under test to call the real daemon spawning function."""
    runtime_guard.enabled = True
    return runtime_guard


@pytest.fixture(scope="session")
def shared_singbox(runtime_session):
    """One checksum-verified binary for every opted-in real runtime test."""
    key = singbox.host_platform()
    expected = singbox.SINGBOX_SHA256[key]
    shared = runtime_session.root / "shared"
    shared.mkdir(mode=0o700)
    target = shared / f"sing-box@{singbox.SINGBOX_VERSION}"

    candidates = []
    if configured := os.environ.get("ALLE_TEST_SINGBOX"):
        candidates.append(Path(configured))
    candidates.append(
        Path.home() / ".alle" / "bin" / f"sing-box@{singbox.SINGBOX_VERSION}"
    )
    source = next(
        (
            path
            for path in candidates
            if path.is_file() and singbox._sha256_file(path) == expected
        ),
        None,
    )
    if source is not None:
        try:
            shutil.copy2(source, target)
        except OSError as error:
            raise RuntimeError(
                f"could not stage shared test sing-box: {error}"
            ) from error
    else:
        provision = runtime_session.root / "provision"
        old_home = os.environ.get("ALLE_HOME")
        old_override = os.environ.pop("ALLE_SINGBOX", None)
        os.environ["ALLE_HOME"] = str(provision)
        try:
            target = singbox.ensure_binary()
        finally:
            if old_home is None:
                os.environ.pop("ALLE_HOME", None)
            else:
                os.environ["ALLE_HOME"] = old_home
            if old_override is not None:
                os.environ["ALLE_SINGBOX"] = old_override
    assert singbox._sha256_file(target) == expected
    return target


@pytest.fixture
def real_background_runtime(monkeypatch, background_runtime, shared_singbox):
    """A tracked real applier + shared sing-box capability for lifecycle tests."""
    from alle import daemonctl

    # A developer may run alle under a real login service. The explicit test
    # capability always owns its detached child; it must never address that
    # user-level service manager or the developer's daemon.
    monkeypatch.setattr(daemonctl, "is_installed", lambda: False)
    monkeypatch.setenv("ALLE_SINGBOX", str(shared_singbox))
    return background_runtime


def start_test_server(httpd, *, poll_interval: float = 0.02):
    """Start a test HTTP server and return the thread its owner must reap."""
    thread = threading.Thread(
        target=lambda: httpd.serve_forever(poll_interval=poll_interval),
        name="alle-test-http",
        daemon=True,
    )
    thread.start()
    return thread


def stop_test_server(httpd, thread) -> None:
    """Stop, close, and join a server before its test home disappears."""
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive(), "test HTTP server thread survived teardown"


def wg_config(endpoint_host: str = "1.2.3.4") -> dict:
    """A minimal WireGuard-params dict, like a provider hands the store."""
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
