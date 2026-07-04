"""The local credential store round-trips per-provider logins and stays private."""

from __future__ import annotations

import stat

from alle import credentials, paths


def test_set_get_remove_roundtrip():
    assert credentials.get("nordvpn") is None
    assert credentials.configured() == []

    credentials.set_("nordvpn", {"token": "  abc123  "})
    # values are stripped on the way in
    assert credentials.get("nordvpn") == {"token": "abc123"}
    assert credentials.configured() == ["nordvpn"]

    assert credentials.remove("nordvpn") is True
    assert credentials.remove("nordvpn") is False
    assert credentials.get("nordvpn") is None


def test_multiple_providers_are_independent():
    credentials.set_("nordvpn", {"token": "t"})
    credentials.set_("protonvpn", {"username": "p1", "password": "secret"})
    assert credentials.configured() == ["nordvpn", "protonvpn"]
    credentials.remove("nordvpn")
    assert credentials.configured() == ["protonvpn"]
    assert credentials.get("protonvpn") == {"username": "p1", "password": "secret"}


def test_credentials_file_is_private():
    credentials.set_("nordvpn", {"token": "t"})
    path = paths.state_dir() / "credentials.yaml"
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600  # owner read/write only
