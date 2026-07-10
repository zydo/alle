"""The local credential store round-trips per-provider logins and stays private."""

from __future__ import annotations

import stat

import pytest

from alle import credentials, paths
from alle.state import StoreReadError


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


def test_save_leaves_no_temp_files():
    credentials.set_("nordvpn", {"token": "t"})
    credentials.set_("nordvpn", {"token": "t2"})  # overwrite goes through a temp file
    leftovers = list(paths.state_dir().glob(".credentials-*"))
    assert leftovers == []


def test_corrupt_credentials_are_quarantined_not_silently_wiped(capsys):
    credentials.set_("nordvpn", {"token": "t"})
    path = paths.state_dir() / "credentials.yaml"
    path.write_text("providers: [unclosed")  # truncated write

    # Unparseable file reads as "nothing configured", but the bytes are moved
    # aside — a later set_/remove can't persist emptiness over the only copy.
    assert credentials.configured() == []
    backups = list(paths.state_dir().glob("credentials.yaml.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "providers: [unclosed"
    assert "corrupt" in capsys.readouterr().err


def test_non_mapping_credentials_are_quarantined():
    path = paths.state_dir() / "credentials.yaml"
    path.write_text("- just\n- a\n- list\n")  # valid YAML, wrong shape
    assert credentials.configured() == []
    assert len(list(paths.state_dir().glob("credentials.yaml.corrupt-*"))) == 1


def test_unreadable_credentials_abort_instead_of_wiping():
    credentials.set_("nordvpn", {"token": "tok_12345678"})
    path = paths.state_dir() / "credentials.yaml"
    path.chmod(0)  # permission error ≠ absent file
    try:
        with pytest.raises(StoreReadError):
            credentials.get("nordvpn")
        # a write built on the unreadable view must abort, not wipe the file
        with pytest.raises(StoreReadError):
            credentials.set_("protonvpn", {"token": "x"})
    finally:
        path.chmod(0o600)
    assert credentials.get("nordvpn") == {"token": "tok_12345678"}
    assert list(paths.state_dir().glob("credentials.yaml.corrupt-*")) == []
