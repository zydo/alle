"""CLI adapter behavior and machine-readable read commands."""

from __future__ import annotations

import json

import pytest

from alle import __version__, cli, service


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
    assert run_cli(["test"], capsys) == (
        "No channels configured. Add one:  alle channels add nordvpn --country …"
    )


def test_json_read_commands(capsys, no_singbox):
    providers = json.loads(run_cli(["providers", "ls", "--json"], capsys))
    channels = json.loads(run_cli(["channels", "ls", "--json"], capsys))
    status = json.loads(run_cli(["status", "--json"], capsys))

    assert providers == {"providers": []}
    assert channels == {"providers": [], "channels": []}
    assert status["running"] is False
    assert status["state"] == "stopped"
    assert status["channels"] == []


def test_test_json_empty(capsys, no_singbox):
    data = json.loads(run_cli(["test", "--json"], capsys))
    assert data["probed"] is False
    assert data["speed"] is False
    assert data["channels"] == []


def test_version_command(capsys):
    assert run_cli(["version"], capsys) == __version__


def test_channel_commands_share_identity_columns(capsys, no_singbox):
    """Every channel-listing command exposes the same identity fields
    (provider, name, port, country, city), and the ones that always render a
    table lead with those five columns. `status` only renders its table while
    running, so it is checked via JSON only."""
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel(
        "nordvpn", "United States", "Seattle", {"private_key": "x", "peer": {}}
    )

    basics = ["provider", "name", "port", "country", "city"]
    for rows in (
        service.channel_list()["channels"],
        service.status_snapshot()["channels"],
        service.test()["channels"],
        service.metrics_snapshot()["channels"],
    ):
        assert rows
        assert set(basics) <= set(rows[0])
        assert rows[0]["port"].startswith(":")

    for cmd in (["channels", "ls"], ["test"], ["metrics"]):
        header = run_cli(cmd, capsys).splitlines()[0].split()
        assert header[:5] == ["PROVIDER", "NAME", "PORT", "COUNTRY", "CITY"], (
            cmd,
            header,
        )


def test_config_provider_lifecycle_keeps_cli_messages(
    capsys, no_background, no_singbox
):
    added = run_cli(["providers", "add", "protonvpn"], capsys)
    assert added.startswith("Added provider Proton VPN.")

    listed = run_cli(["providers", "ls"], capsys)
    assert (
        "Proton VPN" in listed and "0 .conf files" in listed
    )  # config providers show a count

    channels = run_cli(["channels", "ls"], capsys)
    assert channels.startswith(
        "No channels yet."
    )  # provider added, but nothing imported

    locations = run_cli(["locations", "protonvpn"], capsys)
    assert locations.startswith("Proton VPN: locations are not listed here.")

    removed = run_cli(["providers", "rm", "protonvpn", "-y"], capsys)
    assert removed == "Removed Proton VPN and its 0 channel(s)."


def test_providers_rm_accepts_multiple_and_dry_run(capsys, no_background):
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_provider("protonvpn")

    dry = run_cli(["providers", "rm", "nordvpn", "protonvpn", "--dry-run"], capsys)
    assert "Would remove NordVPN and its 0 channel(s)." in dry
    assert "Would remove Proton VPN and its 0 channel(s)." in dry
    assert store.has_provider("nordvpn")
    assert store.has_provider("protonvpn")

    removed = run_cli(["providers", "rm", "nordvpn", "protonvpn", "-y"], capsys)
    assert "Removed NordVPN and its 0 channel(s)." in removed
    assert "Removed Proton VPN and its 0 channel(s)." in removed
    assert "Removed 2 providers." in removed
    assert not service.Store.load().has_provider("nordvpn")
    assert not service.Store.load().has_provider("protonvpn")


# Fully synthetic: keys decode to 32 bytes but are made-up values, and the
# endpoint uses a TEST-NET-1 documentation address. Never real conf contents.
SAMPLE_CONF = """\
[Interface]
# Key for alle-test
PrivateKey = WEVHcHJpdmF0ZUtleUV4YW1wbGVWYWx1ZUFBQUFBQUE=
Address = 10.0.0.2/32
DNS = 10.0.0.1

[Peer]
PublicKey = c3ludGhldGljLWFsbGUtdGVzdC1wdWJsaWMta2V5LTA=
AllowedIPs = 0.0.0.0/0
Endpoint = 192.0.2.10:51820
"""


def test_config_import_requires_provider_added(capsys, no_background):
    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "add", "protonvpn", "--config", "/tmp/proton.conf"])
    assert (
        str(exc.value)
        == "Proton VPN is not added — run `alle providers add protonvpn` first."
    )


def test_config_import_missing_file(capsys, no_background):
    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["channels", "add", "protonvpn", "--config", "/tmp/does-not-exist.conf"]
        )
    assert str(exc.value) == "config file not found: /tmp/does-not-exist.conf"


def test_config_import_rejects_garbage(capsys, no_background, tmp_path):
    bad = tmp_path / "bad.conf"
    bad.write_text("not a wireguard config")
    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "add", "protonvpn", "--config", str(bad)])
    assert "is not a usable WireGuard .conf" in str(exc.value)


def test_config_import_stores_channel(capsys, no_background, tmp_path):
    conf = tmp_path / "wg-US-CA-842.conf"
    conf.write_text(SAMPLE_CONF)
    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()

    out = run_cli(["channels", "add", "protonvpn", "--config", str(conf)], capsys)
    assert out.startswith(
        "Imported channel wg_us_ca_842 under Proton VPN from wg-US-CA-842.conf"
    )

    ch = service.Store.load().get_channel("protonvpn", "wg_us_ca_842")
    assert ch is not None
    assert ch.wg["peer"]["endpoint_host"] == "192.0.2.10"
    assert ch.wg["peer"]["endpoint_port"] == 51820
    assert ch.wg["address"] == ["10.0.0.2/32"]
    # country/city are parsed from the file name's ISO codes (wg-US-CA-842), not guessed
    assert ch.country == "United States" and ch.city == "California"


def test_unparseable_config_shows_unknown(capsys, no_background, tmp_path):
    conf = tmp_path / "myserver.conf"  # no ISO codes in the name
    conf.write_text(SAMPLE_CONF)
    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()
    cli.main(["channels", "add", "protonvpn", "--config", str(conf)])
    capsys.readouterr()
    listed = run_cli(["channels", "ls"], capsys)
    assert (
        "(Unknown)" in listed
    )  # country and city both unresolved -> braced placeholder


def test_nordvpn_country_only_shows_any_city(capsys, no_background, tmp_path):
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "United States", "", {"private_key": "x", "peer": {}})
    listed = run_cli(["channels", "ls"], capsys)
    assert "(Any City)" in listed  # API channel, country but no city


def test_channels_ls_ids_and_refs(capsys, no_background):
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "Japan", "", {"private_key": "x", "peer": {}})
    store.add_channel(
        "nordvpn", "United States", "Seattle", {"private_key": "x", "peer": {}}
    )

    assert run_cli(["channels", "ls", "--ids"], capsys).splitlines() == [
        "japan_1",
        "united_states_seattle_1",
    ]
    assert run_cli(["channels", "ls", "--refs"], capsys).splitlines() == [
        "nordvpn/japan_1",
        "nordvpn/united_states_seattle_1",
    ]


def test_channels_rm_accepts_multiple_names(capsys, no_background):
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "Japan", "", {"private_key": "x", "peer": {}})
    store.add_channel(
        "nordvpn", "United States", "Seattle", {"private_key": "x", "peer": {}}
    )

    removed = run_cli(["channels", "rm", "japan_1", "united_states_seattle_1"], capsys)
    assert "Removed channel japan_1 from NordVPN." in removed
    assert "Removed channel united_states_seattle_1 from NordVPN." in removed
    assert "Removed 2 channels." in removed
    assert service.Store.load().provider_channels("nordvpn") == []


def test_channels_rm_supports_qualified_refs_for_duplicates(capsys, no_background):
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_provider("protonvpn")
    store.add_channel("nordvpn", "Japan", "", {"private_key": "x", "peer": {}})
    store.add_channel("protonvpn", "Japan", "", {"private_key": "x", "peer": {}})

    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "rm", "japan_1"])
    assert "exists under multiple providers" in str(exc.value)

    removed = run_cli(["channels", "rm", "protonvpn/japan_1"], capsys)
    assert removed == "Removed channel japan_1 from Proton VPN."
    assert service.Store.load().get_channel("nordvpn", "japan_1") is not None
    assert service.Store.load().get_channel("protonvpn", "japan_1") is None


def test_channels_rm_supports_globs_dry_run_and_scoped_all(capsys, no_background):
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel(
        "nordvpn", "United States", "Seattle", {"private_key": "x", "peer": {}}
    )
    store.add_channel(
        "nordvpn", "United States", "Chicago", {"private_key": "x", "peer": {}}
    )
    store.add_channel("nordvpn", "Japan", "", {"private_key": "x", "peer": {}})

    dry = run_cli(["channels", "rm", "united_states_*", "--dry-run"], capsys)
    assert "Would remove 2 channels." in dry
    assert len(service.Store.load().provider_channels("nordvpn")) == 3

    removed = run_cli(["channels", "rm", "united_states_*"], capsys)
    assert "Removed 2 channels." in removed
    assert service.Store.load().get_channel("nordvpn", "japan_1") is not None

    removed_all = run_cli(["channels", "rm", "--provider", "nordvpn", "--all"], capsys)
    assert removed_all == "Removed channel japan_1 from NordVPN."
    assert service.Store.load().provider_channels("nordvpn") == []


def test_channels_ls_is_a_flat_table_with_separator(capsys, no_background, tmp_path):
    conf = tmp_path / "wg-US-CA-842.conf"
    conf.write_text(SAMPLE_CONF)
    cli.main(["providers", "add", "protonvpn"])
    cli.main(["channels", "add", "protonvpn", "--config", str(conf)])
    capsys.readouterr()
    lines = run_cli(["channels", "ls"], capsys).splitlines()
    assert lines[0].split() == [
        "PROVIDER",
        "NAME",
        "PORT",
        "COUNTRY",
        "CITY",
    ]  # single header
    assert set(lines[1]) <= {"-", " "} and "-" in lines[1]  # dash separator
    assert lines[2].startswith("Proton VPN")  # flat row, brand + PROVIDER column


def test_config_import_id_comes_from_filename(capsys, no_background, tmp_path):
    conf = tmp_path / "de-berlin-server.conf"
    conf.write_text(SAMPLE_CONF)
    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()
    run_cli(["channels", "add", "protonvpn", "--config", str(conf)], capsys)
    assert service.Store.load().get_channel("protonvpn", "de_berlin_server") is not None


def test_reimporting_same_file_updates_in_place(capsys, no_background, tmp_path):
    conf = tmp_path / "wg-US-CA-842.conf"
    conf.write_text(SAMPLE_CONF)
    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()  # discard the provider-add output
    out1 = run_cli(["channels", "add", "protonvpn", "--config", str(conf)], capsys)
    assert out1.startswith("Imported channel wg_us_ca_842 ")
    imported = service.Store.load().get_channel("protonvpn", "wg_us_ca_842")
    assert imported is not None
    port1 = imported.port

    # regenerate the config (rotated key) and re-import the same file name
    conf.write_text(SAMPLE_CONF.replace("WEVH", "ZZZZ"))
    out2 = run_cli(["channels", "add", "protonvpn", "--config", str(conf)], capsys)
    assert out2.startswith("Updated channel wg_us_ca_842 ")

    channels = service.Store.load().provider_channels("protonvpn")
    assert len(channels) == 1  # updated in place — no wg_us_ca_842_2
    assert channels[0].port == port1  # local port stays stable
    assert channels[0].wg["private_key"].startswith("ZZZZ")  # rotated key applied


def test_config_and_location_flags_are_mutually_exclusive(
    capsys, no_background, tmp_path
):
    conf = tmp_path / "srv.conf"
    conf.write_text(SAMPLE_CONF)
    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()
    for extra in (["--country", "United States"], ["--city", "Los Angeles"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(["channels", "add", "protonvpn", "--config", str(conf), *extra])
        assert "--config cannot be combined with --country/--city" in str(exc.value)


def test_providers_ls_counts_config_files(capsys, no_background, tmp_path):
    conf = tmp_path / "wg-US-CA-842.conf"
    conf.write_text(SAMPLE_CONF)
    cli.main(["providers", "add", "protonvpn"])
    capsys.readouterr()
    assert "0 .conf files" in run_cli(["providers", "ls"], capsys)

    cli.main(["channels", "add", "protonvpn", "--config", str(conf)])
    capsys.readouterr()
    assert "1 .conf file" in run_cli(["providers", "ls"], capsys)  # singular, updates

    cli.main(["channels", "rm", "protonvpn", "--channel", "wg_us_ca_842"])
    capsys.readouterr()
    assert "0 .conf files" in run_cli(["providers", "ls"], capsys)  # updates back down


def test_config_flag_rejected_for_api_provider(capsys, no_background, tmp_path):
    conf = tmp_path / "srv.conf"
    conf.write_text(SAMPLE_CONF)
    cli.main(["providers", "add", "protonvpn"])  # ensure a config provider exists too
    capsys.readouterr()
    # add nordvpn provider without a credential prompt by going through the service store
    service.Store.load().add_provider("nordvpn")
    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "add", "nordvpn", "--config", str(conf)])
    assert "uses an API" in str(exc.value)
