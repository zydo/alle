"""Platform selection is the only arch-dependent code, so it's table-tested with
mocked ``platform`` values — covering every supported (and unsupported) host
without containers."""

from __future__ import annotations

import pytest

from alle import singbox
from alle.singbox import SingBoxError


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-amd64"),
        ("Linux", "x86_64", "linux-amd64"),
        ("Linux", "amd64", "linux-amd64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Linux", "arm64", "linux-arm64"),
    ],
)
def test_supported_hosts(monkeypatch, system, machine, expected):
    monkeypatch.setattr(singbox.platform, "system", lambda: system)
    monkeypatch.setattr(singbox.platform, "machine", lambda: machine)
    assert singbox.host_platform() == expected


@pytest.mark.parametrize(
    ("system", "machine"),
    [
        ("Windows", "AMD64"),  # mainstream OS, but POSIX-only daemon + no support
        ("Linux", "armv7l"),  # 32-bit ARM — not in the pinned mainstream set
        ("Linux", "i686"),
        ("Linux", "riscv64"),
        ("Darwin", "ppc"),
    ],
)
def test_unsupported_hosts_hard_error(monkeypatch, system, machine):
    monkeypatch.setattr(singbox.platform, "system", lambda: system)
    monkeypatch.setattr(singbox.platform, "machine", lambda: machine)
    with pytest.raises(SingBoxError, match="unsupported platform"):
        singbox.host_platform()


def test_every_supported_key_has_a_checksum():
    # the allowlist and the checksum table must stay in lockstep
    from alle.constants import SINGBOX_SHA256

    for key in ("darwin-arm64", "darwin-amd64", "linux-amd64", "linux-arm64"):
        assert key in SINGBOX_SHA256
        assert len(SINGBOX_SHA256[key]) == 64  # sha256 hex
