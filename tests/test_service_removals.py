"""Focused service-layer tests for destructive removal planning."""

from __future__ import annotations

import pytest

from alle import service


WG = {"private_key": "x", "peer": {}}


def test_provider_remove_many_preflights_before_mutating():
    store = service.Store.load()
    store.add_provider("nordvpn")

    with pytest.raises(service.ServiceError) as exc:
        service.provider_remove_many(["nordvpn", "protonvpn"])

    assert "Proton VPN is not added" in str(exc.value)
    assert service.Store.load().has_provider("nordvpn")


def test_provider_remove_many_requires_at_least_one_provider():
    with pytest.raises(service.ServiceError) as exc:
        service.provider_remove_many([])

    assert "at least one provider is required" in str(exc.value)


def test_provider_remove_many_dedupes_and_dry_run_does_not_mutate():
    store = service.Store.load()
    store.add_provider("nordvpn")

    result = service.provider_remove_many(["nordvpn", "nordvpn"], dry_run=True)

    assert result == {
        "providers": [
            {
                "provider": "nordvpn",
                "display_name": "NordVPN",
                "channels_removed": 0,
            }
        ],
        "dry_run": True,
    }
    assert service.Store.load().has_provider("nordvpn")


def test_channel_remove_many_plain_name_requires_disambiguation():
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_provider("protonvpn")
    store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.add_channel("protonvpn", "Japan", "", dict(WG))

    with pytest.raises(service.ServiceError) as exc:
        service.channel_remove_many(["wg_jp_1"])

    assert "exists under multiple providers" in str(exc.value)
    assert service.Store.load().get_channel("nordvpn", "wg_jp_1") is not None
    assert service.Store.load().get_channel("protonvpn", "wg_jp_1") is not None


def test_channel_remove_many_supports_qualified_glob_and_dedupes():
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_provider("protonvpn")
    store.add_channel("nordvpn", "United States", "Seattle", dict(WG))
    store.add_channel("nordvpn", "United States", "Chicago", dict(WG))
    store.add_channel("protonvpn", "United States", "Seattle", dict(WG))

    result = service.channel_remove_many(["nordvpn/wg_us_*", "nordvpn/wg_us_seattle_1"])

    assert [item["ref"] for item in result["channels"]] == [
        "nordvpn/wg_us_chicago_1",
        "nordvpn/wg_us_seattle_1",
    ]
    assert service.Store.load().provider_channels("nordvpn") == []
    assert service.Store.load().get_channel("protonvpn", "wg_us_seattle_1") is not None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "at least one channel name is required"),
        ({"channel_ids": ["missing"]}, "no channel named 'missing'"),
        (
            {"channel_ids": ["bad/wg_jp_1"]},
            "unknown provider 'bad'",
        ),
        (
            {"channel_ids": ["wg_jp_1"], "provider": "nordvpn"},
            "NordVPN is not added",
        ),
        (
            {"channel_ids": ["wg_jp_1"], "provider": "protonvpn"},
            "Proton VPN is not added",
        ),
        (
            {"channel_ids": ["wg_jp_1"], "all_": True},
            "--all cannot be combined with channel names",
        ),
        (
            {"channel_ids": [], "all_": True},
            "--all for channels requires --provider",
        ),
    ],
)
def test_channel_remove_many_validation_errors(kwargs, message):
    with pytest.raises(service.ServiceError) as exc:
        service.channel_remove_many(**({"channel_ids": []} | kwargs))

    assert message in str(exc.value)


def test_channel_remove_many_scoped_missing_channel():
    store = service.Store.load()
    store.add_provider("nordvpn")

    with pytest.raises(service.ServiceError) as exc:
        service.channel_remove_many(["missing"], provider="nordvpn")

    assert "no channel 'missing' under NordVPN" in str(exc.value)


def test_channel_remove_many_scoped_all_requires_existing_channels():
    store = service.Store.load()
    store.add_provider("nordvpn")

    with pytest.raises(service.ServiceError) as exc:
        service.channel_remove_many([], provider="nordvpn", all_=True)

    assert "no channels under NordVPN" in str(exc.value)
