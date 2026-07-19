"""Scheduled setup-bundle backups: due-ness, retention, and the secret posture."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import pytest

from alle import backup, service
from alle.state import Store
from conftest import wg_config

WG = wg_config("1.2.3.4")


@pytest.fixture
def channel():
    store = Store.load()
    store.add_provider("nordvpn")
    return store.add_channel("nordvpn", "US", "", dict(WG))


def _age(path: Path, hours: float) -> None:
    past = time.time() - hours * 3600
    os.utime(path, (past, past))


# ---- the backup module -------------------------------------------------------


def test_run_force_writes_a_secret_bundle_into_a_private_dir(channel):
    report = backup.run(force=True)
    assert report is not None
    path = Path(report["path"])
    assert path.parent == backup.default_dir()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert text.startswith("# alle setup bundle")
    assert "nordvpn" in text


def test_run_skips_when_disabled_and_when_not_due(channel):
    assert backup.run() is None  # disabled by default
    Store.load().set_backup(enabled=True)
    first = backup.run()
    assert first is not None  # empty dir -> immediately due
    assert backup.run() is None  # fresh backup -> not due
    # age the backup past the 24h default (rename too: within one wall-clock
    # second a rerun would collide with the just-written timestamped name)
    aged = Path(first["path"]).parent / "alle-backup-20260101-000000.yaml"
    Path(first["path"]).rename(aged)
    _age(aged, hours=25)
    second = backup.run()
    assert second is not None and second["path"] != str(aged)


def test_due_ness_is_derived_from_file_mtime_not_stored_state(channel):
    Store.load().set_backup(enabled=True)
    report = backup.run()
    assert report is not None
    # deleting the rotation makes the schedule immediately due again — the
    # self-healing property of deriving due-ness from what is on disk
    Path(report["path"]).unlink()
    assert backup.run() is not None


def test_retention_prunes_only_our_files(channel):
    directory = backup.prepare_dir(backup.default_dir())
    stranger = directory / "not-a-backup.yaml"
    stranger.write_text("mine")
    old = []
    for i in range(3):
        p = directory / f"alle-backup-2026010{i + 1}-000000.yaml"
        p.write_text("old")
        _age(p, hours=100 + i)
        old.append(p)
    Store.load().set_backup(enabled=True, keep=2)
    report = backup.run()
    assert report is not None
    # keep=2: the fresh one + the newest old one (old[0], aged the least)
    # survive; the stranger is untouched even though it sits in the rotation
    # directory
    names = {p.name for p in backup.backup_files(directory)}
    assert len(names) == 2 and old[0].name in names
    assert sorted(report["pruned"]) == sorted([old[1].name, old[2].name])
    assert stranger.exists()


def test_prepare_dir_refuses_weak_or_foreign_destinations(tmp_path):
    victim = tmp_path / "loose"
    victim.mkdir()
    victim.chmod(0o777)
    with pytest.raises(backup.BackupError, match="group/world-writable"):
        backup.prepare_dir(victim)
    link = tmp_path / "link"
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link.symlink_to(real)
    with pytest.raises(backup.BackupError, match="symlink"):
        backup.prepare_dir(link)
    with pytest.raises(backup.BackupError, match="absolute"):
        backup.prepare_dir("relative/backups")


def test_run_due_logs_failures_once_and_recovers(channel, monkeypatch):
    Store.load().set_backup(enabled=True, directory=str(backup.default_dir()))
    logged: list[str] = []
    monkeypatch.setattr(backup.applog, "log", lambda m: logged.append(m))
    monkeypatch.setattr(
        backup, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    )
    backup.run_due()
    backup.run_due()  # same failure again — logged once, not spammed
    assert [m for m in logged if "disk full" in m] == [
        "scheduled backup failed: disk full"
    ]


# ---- the service layer -------------------------------------------------------


def test_backup_configure_enable_writes_first_backup_and_reports(channel):
    result = service.backup_configure(enabled=True, every_hours=12, keep=3)
    b = result["backup"]
    assert b["enabled"] is True and b["every_hours"] == 12.0 and b["keep"] == 3
    assert result["first_backup"] is not None
    assert b["count"] == 1 and b["last_backup"] is not None
    # a later settings tweak does not force another backup
    tweak = service.backup_configure(keep=5)
    assert tweak["first_backup"] is None
    assert tweak["backup"]["count"] == 1
    off = service.backup_configure(enabled=False)
    assert off["backup"]["enabled"] is False


def test_backup_configure_rejects_bad_input_strictly(channel):
    cases = [
        ({"enabled": "yes"}, "'enabled' must be a boolean"),
        ({"every_hours": "24"}, "'every_hours' must be a number"),
        ({"every_hours": 0}, "positive number of hours"),
        ({"every_hours": True}, "'every_hours' must be a number"),
        ({"keep": 0}, "integer >= 1"),
        ({"keep": 2.5}, "integer >= 1"),
        ({"directory": "   "}, "non-empty path"),
        ({}, "nothing to change"),
    ]
    for kwargs, msg in cases:
        with pytest.raises(service.ServiceError, match=msg):
            service.backup_configure(**kwargs)
    assert service.backup_status()["backup"]["enabled"] is False


def test_backup_configure_fails_fast_on_a_bad_destination(channel, tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o777)
    with pytest.raises(service.ServiceError, match="group/world-writable"):
        service.backup_configure(enabled=True, directory=str(loose))
    # nothing was persisted — the schedule must not fail forever in the log
    assert service.backup_status()["backup"]["enabled"] is False


def test_backup_now_works_while_schedule_is_off(channel):
    result = service.backup_now()
    assert Path(result["path"]).exists()
    assert result["backup"]["enabled"] is False
    assert result["backup"]["count"] == 1
