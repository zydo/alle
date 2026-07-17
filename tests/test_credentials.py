"""The local credential store round-trips per-provider logins and stays private."""

from __future__ import annotations

import stat
import threading

import pytest

from alle import credentials, fsio, paths
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


def test_credential_parser_diagnostic_never_echoes_secret_source(capsys):
    path = paths.state_dir() / "credentials.yaml"
    path.write_text("providers:\n  nordvpn: [SUPER-SECRET-TOKEN\n")

    assert credentials.configured() == []

    captured = capsys.readouterr().err
    assert "corrupt" in captured
    assert "SUPER-SECRET-TOKEN" not in captured


def test_non_mapping_credentials_are_quarantined():
    path = paths.state_dir() / "credentials.yaml"
    path.write_text("- just\n- a\n- list\n")  # valid YAML, wrong shape
    assert credentials.configured() == []
    assert len(list(paths.state_dir().glob("credentials.yaml.corrupt-*"))) == 1


@pytest.mark.parametrize(
    "text",
    ["false\n", "providers: null\n", "providers:\n  nordvpn: token\n"],
)
def test_falsy_and_nested_wrong_credentials_are_quarantined(text):
    path = paths.state_dir() / "credentials.yaml"
    path.write_text(text)
    assert credentials.configured() == []
    backups = list(paths.state_dir().glob("credentials.yaml.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == text


def test_stale_credential_reader_never_moves_a_new_valid_generation(monkeypatch):
    path = paths.state_dir() / "credentials.yaml"
    path.write_text("providers: [broken")
    entered = threading.Event()
    published = threading.Event()
    original = credentials._quarantine

    def pause(*args, **kwargs):
        entered.set()
        assert published.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(credentials, "_quarantine", pause)
    result = {}

    def read():
        result["providers"] = credentials.configured()

    reader = threading.Thread(target=read)
    reader.start()
    assert entered.wait(2)
    with fsio.locked(credentials._lock_path()):
        credentials._save_all({"nordvpn": {"token": "valid"}})
    published.set()
    reader.join(2)

    assert not reader.is_alive()
    assert result["providers"] == ["nordvpn"]
    assert list(paths.state_dir().glob("credentials.yaml.corrupt-*")) == []


def test_writers_hold_the_credentials_lock():
    import fcntl

    with credentials.transaction() as data:
        data["nordvpn"] = {"token": "t"}
        lock = paths.state_dir() / "credentials.lock"
        with open(lock, "w") as second:  # another writer would block here
            with pytest.raises(OSError):
                fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert credentials.get("nordvpn") == {"token": "t"}  # written on exit


def test_snapshot_is_a_detached_copy():
    credentials.set_("nordvpn", {"token": "t"})
    snap = credentials.snapshot()
    assert snap == {"nordvpn": {"token": "t"}}
    snap["nordvpn"]["token"] = "mutated"
    assert credentials.get("nordvpn") == {"token": "t"}  # store unaffected


def test_unreadable_credentials_abort_instead_of_wiping():
    credentials.set_("nordvpn", {"token": "tok_test_token"})
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
    assert credentials.get("nordvpn") == {"token": "tok_test_token"}
    assert list(paths.state_dir().glob("credentials.yaml.corrupt-*")) == []
