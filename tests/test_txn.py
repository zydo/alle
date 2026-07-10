"""Cross-file setup transactions: the setup lock, the credentials rollback
journal, and crash recovery."""

from __future__ import annotations

import json

import pytest

from alle import credentials, paths, txn


def _journal():
    return paths.state_dir() / "setup-journal.json"


def test_commit_keeps_changes_and_clears_journal():
    credentials.set_("nordvpn", {"token": "old"})

    with txn.setup_transaction("test op") as t:
        assert _journal().exists()  # pre-op copy journalled before writes
        credentials.set_("nordvpn", {"token": "new"})
        t.commit()

    assert credentials.get("nordvpn") == {"token": "new"}
    assert not _journal().exists()


def test_failure_before_commit_rolls_credentials_back():
    credentials.set_("nordvpn", {"token": "old"})

    with pytest.raises(RuntimeError, match="state write failed"):
        with txn.setup_transaction("test op"):
            credentials.set_("nordvpn", {"token": "new"})
            raise RuntimeError("state write failed")

    assert credentials.get("nordvpn") == {"token": "old"}
    assert not _journal().exists()


def test_failure_after_commit_does_not_roll_back():
    credentials.set_("nordvpn", {"token": "old"})

    with pytest.raises(RuntimeError, match="post-commit step"):
        with txn.setup_transaction("test op") as t:
            credentials.set_("nordvpn", {"token": "new"})
            t.commit()  # the state write happened; later steps are best-effort
            raise RuntimeError("post-commit step failed")

    assert credentials.get("nordvpn") == {"token": "new"}
    assert not _journal().exists()


def test_recover_rolls_back_a_crashed_transaction():
    # Simulate a crash mid-op: the journal holds the pre-op credentials, the
    # live file already carries the new ones, and the process died before its
    # commit point.
    credentials.set_("nordvpn", {"token": "new"})
    _journal().write_text(
        json.dumps({"op": "crashed op", "credentials": {"nordvpn": {"token": "old"}}})
    )

    assert txn.recover() is True
    assert credentials.get("nordvpn") == {"token": "old"}
    assert not _journal().exists()
    assert txn.recover() is False  # nothing left to heal


def test_next_transaction_heals_a_crashed_predecessor():
    credentials.set_("nordvpn", {"token": "leaked"})
    _journal().write_text(json.dumps({"op": "crashed op", "credentials": {}}))

    with txn.setup_transaction("next op") as t:
        # the predecessor was rolled back before this op staged anything
        assert credentials.get("nordvpn") is None
        t.commit()


def test_corrupt_journal_is_moved_aside_not_replayed():
    credentials.set_("nordvpn", {"token": "keep"})
    _journal().write_text("{not json")

    assert txn.recover() is False
    assert not _journal().exists()
    assert any(
        p.name.startswith("setup-journal.json.corrupt-")
        for p in paths.state_dir().iterdir()
    )
    assert credentials.get("nordvpn") == {"token": "keep"}  # untouched


def test_journal_is_private():
    import stat

    credentials.set_("nordvpn", {"token": "secret"})
    with txn.setup_transaction("test op") as t:
        mode = stat.S_IMODE(_journal().stat().st_mode)
        t.commit()
    assert mode == 0o600  # the journal carries credentials
