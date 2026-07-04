"""Narrow CLI adapter tests with service calls stubbed out."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alle import cli, service


def test_channels_ls_rejects_conflicting_machine_outputs():
    args = SimpleNamespace(json=True, ids=True, refs=False)

    with pytest.raises(service.ServiceError) as exc:
        cli.cmd_channels_ls(args)

    assert "--json, --ids, and --refs are mutually exclusive" in str(exc.value)


def test_channels_rm_legacy_provider_conflicts_with_provider_flag():
    args = SimpleNamespace(
        channel=[["japan_1"]],
        provider="nordvpn",
        refs=["protonvpn"],
        dry_run=False,
        all=False,
    )

    with pytest.raises(service.ServiceError) as exc:
        cli.cmd_channels_rm(args)

    assert "--provider cannot be combined" in str(exc.value)


def test_channels_rm_legacy_requires_single_provider_ref():
    args = SimpleNamespace(
        channel=[["japan_1"]],
        provider=None,
        refs=["nordvpn", "protonvpn"],
        dry_run=False,
        all=False,
    )

    with pytest.raises(service.ServiceError) as exc:
        cli.cmd_channels_rm(args)

    assert "legacy --channel form requires exactly one provider" in str(exc.value)


def test_providers_rm_rejects_all_with_names():
    args = SimpleNamespace(all=True, providers=["nordvpn"], dry_run=False, yes=True)

    with pytest.raises(service.ServiceError) as exc:
        cli.cmd_providers_rm(args)

    assert "--all cannot be combined with provider names" in str(exc.value)


def test_providers_rm_all_empty_is_a_noop(capsys):
    args = SimpleNamespace(all=True, providers=[], dry_run=False, yes=True)

    cli.cmd_providers_rm(args)

    assert capsys.readouterr().out.rstrip("\n") == "No providers added."


def test_start_stop_restart_and_logs_are_thin_adapters(monkeypatch, capsys):
    monkeypatch.setattr(cli.service, "start", lambda: {"has_channels": True})
    monkeypatch.setattr(cli.service, "stop", lambda: {"was_running": True})
    restarted = []
    monkeypatch.setattr(cli.service, "restart", lambda: restarted.append(True))
    monkeypatch.setattr(cli.service, "logs_tail", lambda lines: f"tail {lines}")

    cli.cmd_start(SimpleNamespace())
    cli.cmd_stop(SimpleNamespace())
    cli.cmd_restart(SimpleNamespace())
    cli.cmd_logs(SimpleNamespace(follow=False, lines=7))

    assert capsys.readouterr().out.splitlines() == [
        "Alle started; channels are being applied and probed. See: alle status",
        "Alle stopped (channels kept in config).",
        "Alle restarted. See: alle status",
        "tail 7",
    ]
    assert restarted == [True]


def test_logs_follow_delegates(monkeypatch):
    called = []
    monkeypatch.setattr(cli.applog, "follow", lambda lines: called.append(lines))

    cli.cmd_logs(SimpleNamespace(follow=True, lines=3))

    assert called == [3]
