"""Human-readable CLI output helpers."""

from __future__ import annotations


from alle import output


def test_json_text_serializes_objects_by_dict():
    class Thing:
        def __init__(self):
            self.value = 3

    assert '"value": 3' in output.json_text({"thing": Thing()})


def test_json_text_falls_back_to_string():
    class SlotThing:
        __slots__ = ()

        def __str__(self):
            return "slotty"

    assert output.json_text({"thing": SlotThing()}) == '{\n  "thing": "slotty"\n}'


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


def test_locations_output_country_match():
    text = output.locations(
        {
            "available": True,
            "provider": "nordvpn",
            "display_name": "NordVPN",
            "country": "Japan",
            "matched": True,
            "cities": ["Tokyo", "Osaka"],
        }
    )

    assert text.splitlines() == [
        "nordvpn cities in Japan (2):",
        "  Tokyo",
        "  Osaka",
    ]


def test_status_inactive_with_channels_and_active_empty():
    inactive = output.status(
        {
            "running": False,
            "channels": [{"name": "wg_us_1"}],
            "channel_count": 1,
            "provider_count": 1,
        }
    )
    assert "Alle - Inactive" in inactive
    assert "run `alle start`" in inactive

    active = output.status({"running": True, "channels": []})
    assert "Alle - Active" in active
    assert "no channels yet" in active


def test_status_surfaces_a_degraded_singbox_runtime():
    snapshot = {
        "running": True,
        "channels": [],
        "daemon": {"runtime": {"singbox": "crash_looping", "detail": "3 exits"}},
    }
    text = output.status(snapshot)
    assert "⚠ sing-box crash-looping: 3 exits" in text
    # a healthy runtime adds no warning line
    snapshot["daemon"]["runtime"] = {"singbox": "ok", "detail": ""}
    assert "sing-box" not in output.status(snapshot)


def test_status_active_is_a_summary_without_a_table():
    # status is the system view: per-provider channel counts, never the
    # per-channel table (that is `alle test`) — so no LABEL header, no AGO.
    text = output.status(
        {
            "running": True,
            "channels": [
                {"provider": "nordvpn", "name": "wg_jp_1"},
                {"provider": "nordvpn", "name": "wg_us_1"},
                {"provider": "protonvpn", "name": "wg_us_ca_842"},
            ],
            "channel_count": 3,
            "provider_count": 2,
        }
    )

    assert "Channels  NordVPN: 2 channels, Proton VPN: 1 channel" in text
    assert "(details: alle channels ls)" in text
    assert "LABEL" not in text and "ago" not in text


def test_status_router_line_points_at_routes_ls():
    text = output.status(
        {
            "running": True,
            "channels": [],
            "router": {
                "port": 54585,
                "rule_count": 8,
                "killswitch": False,
                "unmatched": "direct",
                "lan_direct": True,
            },
        }
    )
    assert (
        "Router    127.0.0.1:54585 — 8 rule(s), LAN bypasses VPN, "
        "unmatched → direct  (details: alle routes ls)" in text
    )


def test_test_result_empty_filter_and_default_failure_state():
    assert (
        output.test_result({"channels": [], "filter": "missing"})
        == "No channel named 'missing'. See: alle channels ls"
    )

    text = output.test_result(
        {
            "channels": [
                {
                    "provider": "nordvpn",
                    "name": "failed_1",
                    "port": ":53124",
                    "country": "US",
                    "city": "(Any City)",
                    "healthy": False,
                    "error": "",
                    "latency_ms": None,
                    "ip": None,
                }
            ],
            "speed": False,
        }
    )

    assert "Failed" in text


def test_test_result_speed_and_failure_state_cells():
    text = output.test_result(
        {
            "channels": [
                {
                    "provider": "nordvpn",
                    "name": "wg_us_1",
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
                    "sent": 1536,
                    "received": 1024**3,
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
    # traffic totals ride along on every test row (SENT/RECV columns)
    assert "SENT" in text and "RECV" in text
    assert "1.5 KB" in text and "1.0 GB" in text


def test_router_mode_always_shows_the_lan_state():
    # LAN handling sits at priority zero — as consequential as unmatched
    # handling, so it shows in both states, not only when disabled.
    empty = {"rule_count": 0, "killswitch": False, "lan_direct": True}
    assert output._router_mode(empty) == "pass-through (no rules)"
    on = {
        "rule_count": 8,
        "killswitch": False,
        "unmatched": "direct",
        "lan_direct": True,
    }
    assert output._router_mode(on) == "8 rule(s), LAN bypasses VPN, unmatched → direct"
    off = {
        "rule_count": 2,
        "killswitch": True,
        "unmatched": "block",
        "lan_direct": False,
    }
    assert (
        output._router_mode(off)
        == "2 rule(s), LAN follows rules, unmatched → block — kill-switch ON"
    )
    # pre-toggle JSON carries no lan_direct key: the default is on
    legacy = {"rule_count": 1, "killswitch": False, "unmatched": "direct"}
    assert "LAN bypasses VPN" in output._router_mode(legacy)


# ---- terminal sanitization: labels can't smuggle ANSI/control sequences -------


def test_sanitize_text_strips_ansi_and_control_characters():
    from alle import output

    s = output.sanitize_text("ok\x1b[31mred\x1b[0m \x07bell \x9b2Jcsi")
    assert "\x1b" not in s and "\x07" not in s and "\x9b" not in s
    assert "red" in s and "bell" in s  # visible text survives


def test_sanitize_text_keeps_plain_unicode():
    from alle import output

    assert output.sanitize_text("Zürich ✓ 東京") == "Zürich ✓ 東京"


def test_channel_table_sanitizes_hostile_labels():
    from alle import output

    data = {
        "providers": {"nordvpn": {}},
        "channels": [
            {
                "provider": "nordvpn",
                "name": "us1",
                "label": "evil\x1b[2J\x1b[1;1Hwipe",
                "country": "US",
                "city": "NYC\x07",
                "port": ":1080",
                "enabled": True,
            }
        ],
    }
    text = output.channels_list(data)
    assert "\x1b" not in text and "\x07" not in text
    assert "wipe" in text  # content kept, escapes gone
