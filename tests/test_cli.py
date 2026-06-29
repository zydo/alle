"""CLI adapter behavior and machine-readable read commands."""

from __future__ import annotations

import json

import pytest

from alle import cli, service


@pytest.fixture
def no_background(monkeypatch):
    monkeypatch.setattr(service.daemon, "ensure_running", lambda: None)
    monkeypatch.setattr(service.daemon, "stop", lambda: False)


@pytest.fixture
def no_singbox(monkeypatch):
    class Runner:
        def is_running(self):
            return False

        def stop(self):
            raise AssertionError("stop should not be called when not running")

    monkeypatch.setattr(service.singbox, "Runner", Runner)


def run_cli(args, capsys):
    cli.main(args)
    return capsys.readouterr().out.rstrip("\n")


def test_empty_read_commands_keep_human_output(capsys, no_singbox):
    assert run_cli(["providers", "ls"], capsys) == (
        "No providers added yet. Add one:  alle providers add nordvpn"
    )
    assert run_cli(["channels", "ls"], capsys) == (
        "No providers added yet. Add one:  alle providers add nordvpn"
    )
    assert run_cli(["status"], capsys) == "Alle - Inactive"
    assert run_cli(["test"], capsys) == "No channels to test."


def test_json_read_commands(capsys, no_singbox):
    providers = json.loads(run_cli(["providers", "ls", "--json"], capsys))
    channels = json.loads(run_cli(["channels", "ls", "--json"], capsys))
    status = json.loads(run_cli(["status", "--json"], capsys))

    assert providers == {"providers": []}
    assert channels == {"providers": [], "channels": []}
    assert status["running"] is False
    assert status["state"] == "stopped"
    assert status["channels"] == []


def test_config_provider_lifecycle_keeps_cli_messages(capsys, no_background, no_singbox):
    added = run_cli(["providers", "add", "protonvpn"], capsys)
    assert added.startswith("Added provider ProtonVPN.")

    listed = run_cli(["providers", "ls"], capsys)
    assert listed == "  ProtonVPN"

    channels = run_cli(["channels", "ls"], capsys)
    assert channels == "protonvpn:\n    (no channels)"

    locations = run_cli(["locations", "protonvpn"], capsys)
    assert locations.startswith("ProtonVPN: locations are not listed here.")

    removed = run_cli(["providers", "rm", "protonvpn", "-y"], capsys)
    assert removed == "Removed ProtonVPN and its 0 channel(s)."


def test_existing_error_messages(capsys, no_background):
    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "add", "protonvpn", "--config", "/tmp/proton.conf"])
    assert str(exc.value) == "ProtonVPN is not added — run `alle providers add protonvpn` first."

    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "add", "protonvpn", "--config", "/tmp/proton.conf"])
    assert str(exc.value) == (
        "importing channels from a WireGuard .conf is not implemented yet (post-MVP). "
        "For now, ProtonVPN channels cannot be added."
    )
