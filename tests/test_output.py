"""Human-readable CLI output helpers."""

from __future__ import annotations

import time

from alle import output


def test_json_text_serializes_objects_by_dict():
    class Thing:
        def __init__(self):
            self.value = 3

    assert '"value": 3' in output.json_text({"thing": Thing()})


def test_providers_list_renders_config_singular_and_token_fallback():
    text = output.providers_list(
        {
            "providers": [
                {
                    "display_name": "Proton VPN",
                    "kind": "config",
                    "channel_count": 1,
                },
                {"display_name": "NordVPN", "kind": "token", "credential": ""},
            ]
        }
    )

    assert "1 .conf file" in text
    assert "(no credential)" in text


def test_locations_output_covers_unavailable_country_and_full_list():
    unavailable = output.locations(
        {"available": False, "display_name": "Proton VPN", "help": "Import .conf"}
    )
    assert unavailable.startswith("Proton VPN: locations are not listed here.")

    missing = output.locations(
        {
            "available": True,
            "provider": "nordvpn",
            "display_name": "NordVPN",
            "country": "Atlantis",
            "matched": False,
        }
    )
    assert "not a NordVPN country" in missing

    full = output.locations(
        {
            "available": True,
            "provider": "nordvpn",
            "country_count": 1,
            "city_count": 2,
            "countries": [{"country": "US", "cities": ["Seattle", "Austin"]}],
        }
    )
    assert "nordvpn: 1 countries, 2 cities" in full
    assert "Seattle" in full


def test_status_inactive_with_channels_and_active_empty():
    inactive = output.status(
        {
            "running": False,
            "channels": [{"name": "us_1"}],
            "channel_count": 1,
            "provider_count": 1,
        }
    )
    assert "Alle - Inactive" in inactive
    assert "run `alle start`" in inactive

    active = output.status({"running": True, "channels": []})
    assert "Alle - Active" in active
    assert "no channels yet" in active


def test_metrics_filter_empty_and_age_units(monkeypatch):
    assert (
        output.metrics({"channels": [], "filter": "missing"})
        == "No channel named 'missing'. See: alle channels ls"
    )

    now = 10_000
    monkeypatch.setattr(time, "time", lambda: now)
    text = output.metrics(
        {
            "channels": [
                {
                    "provider": "nordvpn",
                    "name": "us_1",
                    "port": ":53124",
                    "country": "US",
                    "city": "(Any City)",
                    "sent": 1536,
                    "received": 1024**2,
                    "total": 1024**3,
                    "updated_at": now - 3600,
                }
            ]
        }
    )
    assert "1.5 KB" in text
    assert "1.0 MB" in text
    assert "1.0 GB" in text
    assert "1h ago" in text


def test_test_result_speed_and_failure_state_cells():
    text = output.test_result(
        {
            "channels": [
                {
                    "provider": "nordvpn",
                    "name": "us_1",
                    "port": ":53124",
                    "country": "US",
                    "city": "(Any City)",
                    "healthy": False,
                    "error": "timeout",
                    "latency_ms": None,
                    "ip": None,
                    "speed_result": {},
                },
                {
                    "provider": "nordvpn",
                    "name": "jp_1",
                    "port": ":53125",
                    "country": "Japan",
                    "city": "(Any City)",
                    "healthy": True,
                    "error": None,
                    "latency_ms": 12,
                    "ip": "1.2.3.4",
                    "speed_result": {
                        "download_bps": 150_000_000,
                        "upload_bps": 20_000_000,
                    },
                },
            ],
            "speed": True,
        }
    )

    assert "Timeout" in text
    assert "150.0 Mbps" in text
    assert "20.0 Mbps" in text
