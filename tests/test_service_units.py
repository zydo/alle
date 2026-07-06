"""Focused service orchestration tests with external collaborators stubbed."""

from __future__ import annotations

import pytest

from alle import service
from alle.providers import ProviderError


WG = {"private_key": "x", "peer": {}}


@pytest.fixture(autouse=True)
def no_background(monkeypatch):
    monkeypatch.setattr(service.daemon, "ensure_running", lambda: None)


def test_resolve_provider_rejects_unknown():
    with pytest.raises(service.ServiceError) as exc:
        service.resolve_provider("bogus")

    assert "unknown provider 'bogus'" in str(exc.value)


def test_provider_add_token_validates_stores_and_lists_masked_secret(monkeypatch):
    validated = []
    monkeypatch.setattr(
        service, "validate_provider_credentials", lambda p, c: validated.append((p, c))
    )

    result = service.provider_add_token("nordvpn", {"token": "abcdef123456"})

    assert result["functional"] is True
    assert validated == [("nordvpn", {"token": "abcdef123456"})]
    assert service.provider_list()["providers"] == [
        {
            "provider": "nordvpn",
            "display_name": "NordVPN",
            "kind": "token",
            "credential": "******3456",
            "channel_count": 0,
        }
    ]


def test_provider_add_config_returns_help():
    result = service.provider_add_config("protonvpn")

    assert result["provider"] == "protonvpn"
    assert result["display_name"] == "Proton VPN"
    assert "WireGuard" in result["config_help"]


def test_validate_provider_credentials_delegates_to_provider(monkeypatch):
    called = []
    monkeypatch.setitem(
        service.PROVIDERS,
        "fakevpn",
        {"derive_key": lambda creds: called.append(creds) or "key"},
    )

    service.validate_provider_credentials("fakevpn", {"token": "secret"})

    assert called == [{"token": "secret"}]


def test_channel_add_validation_errors_and_provider_error(monkeypatch):
    with pytest.raises(service.ServiceError) as exc:
        service.channel_add("nordvpn", "Japan", None)
    assert "run `alle providers add nordvpn` first" in str(exc.value)

    store = service.Store.load()
    store.add_provider("protonvpn")
    with pytest.raises(service.ServiceError) as exc:
        service.channel_add("protonvpn", "Japan", None)
    assert "imported from a WireGuard .conf" in str(exc.value)

    store.add_provider("nordvpn")
    with pytest.raises(service.ServiceError) as exc:
        service.channel_add("nordvpn", None, None)
    assert "usage: alle channels add nordvpn" in str(exc.value)

    def reject_location(provider, country, city):
        raise ProviderError("country 'Atlantis' is not a nordvpn location.")

    monkeypatch.setattr(service, "provider_wg", reject_location)
    with pytest.raises(service.ServiceError) as exc:
        service.channel_add("nordvpn", "Atlantis", None)
    assert "See available locations: alle locations nordvpn" in str(exc.value)


def test_channel_add_rejects_nonfunctional_token_provider(monkeypatch):
    store = service.Store.load()
    store.add_provider("fakevpn")
    monkeypatch.setattr(service, "kind", lambda provider: "token")
    monkeypatch.setattr(service, "is_functional", lambda provider: False)

    with pytest.raises(service.ServiceError) as exc:
        service.channel_add("fakevpn", "Japan", None)

    assert "adding channels under fakevpn isn't implemented yet" in str(exc.value)


def test_channel_add_success_uses_provider_wg(monkeypatch):
    store = service.Store.load()
    store.add_provider("nordvpn")
    calls = []

    def fake_provider_wg(provider, country, city):
        calls.append((provider, country, city))
        return dict(WG)

    monkeypatch.setattr(service, "provider_wg", fake_provider_wg)

    result = service.channel_add("nordvpn", "Japan", None)

    assert calls == [("nordvpn", "Japan", "")]
    assert result["channel"].id == "japan_1"


def test_channel_add_config_read_error(monkeypatch, tmp_path):
    store = service.Store.load()
    store.add_provider("protonvpn")
    conf = tmp_path / "wg-US-CA-842.conf"
    conf.write_text("[Interface]\nPrivateKey = x\n")

    class UnreadablePath:
        name = "wg-US-CA-842.conf"
        stem = "wg-US-CA-842"

        def __init__(self, value):
            self.value = value

        def expanduser(self):
            return self

        def is_file(self):
            return True

        def read_text(self):
            raise OSError("denied")

    monkeypatch.setattr(service, "Path", UnreadablePath)

    with pytest.raises(service.ServiceError) as exc:
        service.channel_add("protonvpn", None, None, str(conf))

    assert "could not read" in str(exc.value)


def test_metrics_snapshot_filter_skips_nonmatching_channels():
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.add_channel("nordvpn", "United States", "", dict(WG))

    result = service.metrics_snapshot("japan_1")

    assert [row["name"] for row in result["channels"]] == ["japan_1"]


def test_locations_list_refresh_country_and_full_list(monkeypatch):
    calls = []
    monkeypatch.setattr(service.locations, "needs_refresh", lambda state, p: True)
    monkeypatch.setattr(
        service.locations, "update", lambda state, providers: calls.append(providers)
    )
    monkeypatch.setattr(
        service.locations,
        "load",
        lambda state, provider: {"Japan": ["Tokyo"], "United States": ["Seattle"]},
    )

    country = service.locations_list("nordvpn", country="japan")
    full = service.locations_list("nordvpn", refresh=True)

    assert country["matched"] is True
    assert country["country"] == "Japan"
    assert country["cities"] == ["Tokyo"]
    assert full["country_count"] == 2
    assert full["city_count"] == 2
    assert calls == [["nordvpn"], ["nordvpn"]]


def test_status_snapshot_covers_probe_and_reconnect_states(monkeypatch):
    class Runner:
        def is_running(self):
            return True

    monkeypatch.setattr(service.singbox, "Runner", Runner)
    store = service.Store.load()
    store.add_provider("nordvpn")
    active = store.add_channel("nordvpn", "Active", "", dict(WG))
    failed = store.add_channel("nordvpn", "Failed", "", dict(WG))
    retrying = store.add_channel("nordvpn", "Retrying", "", dict(WG))
    given_up = store.add_channel("nordvpn", "Given Up", "", dict(WG))
    store.add_channel("nordvpn", "Pending", "", dict(WG))
    store.set_probe(
        "nordvpn", active.id, {"ok": True, "latency_ms": 12, "ip": "1.2.3.4"}
    )
    store.set_probe("nordvpn", failed.id, {"ok": False, "error": "timeout"})
    store.set_reconnect("nordvpn", retrying.id, {"attempts": 2})
    store.set_reconnect("nordvpn", given_up.id, {"failed": True})

    states = {
        row["name"]: row["state"] for row in service.status_snapshot()["channels"]
    }

    assert states == {
        "active_1": "Active",
        "failed_1": "Timeout",
        "given_up_1": "Reconnect failed",
        "pending_1": "Pending",
        "retrying_1": "Reconnecting (2)",
    }


def test_start_stop_restart_and_logs_tail(monkeypatch):
    events = []

    class Runner:
        def is_running(self):
            return True

        def stop(self):
            events.append("singbox-stop")

    store = service.Store.load()
    store.add_provider("nordvpn")
    channel = store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.set_reconnect("nordvpn", channel.id, {"attempts": 1})
    monkeypatch.setattr(service.singbox, "Runner", Runner)
    monkeypatch.setattr(
        service.daemon, "ensure_running", lambda: events.append("ensure")
    )
    monkeypatch.setattr(
        service.daemon, "stop", lambda: events.append("daemon-stop") or True
    )

    assert service.start() == {"has_channels": True}
    assert service.stop() == {"was_running": True}
    assert service.restart() == {"reconnect_cleared": 1}
    assert "restart (cleared reconnect state for 1 channel(s))" in service.logs_tail()
    assert events == [
        "ensure",
        "daemon-stop",
        "singbox-stop",
        "daemon-stop",
        "singbox-stop",
        "ensure",
    ]


def test_channel_set_label_sets_clears_and_resolves():
    store = service.Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))  # id: us_1

    result = service.channel_set_label(ch.id, "  Streaming US  ")
    assert result["label"] == "Streaming US"  # stripped
    assert result["cleared"] is False
    assert service.Store.load().get_channel("nordvpn", ch.id).label == "Streaming US"

    # qualified ref also works, and empty clears back to the id
    cleared = service.channel_set_label(f"nordvpn/{ch.id}", "")
    assert cleared["cleared"] is True
    assert service.Store.load().get_channel("nordvpn", ch.id).label == ""


def test_channel_set_label_rejects_glob_and_unknown():
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG))
    with pytest.raises(service.ServiceError, match="glob cannot be used"):
        service.channel_set_label("us_*", "x")
    with pytest.raises(service.ServiceError, match="no channel"):
        service.channel_set_label("nope_1", "x")


def test_channel_add_stores_label(monkeypatch):
    store = service.Store.load()
    store.add_provider("nordvpn")
    monkeypatch.setattr(service, "provider_wg", lambda p, c, city: dict(WG))
    result = service.channel_add("nordvpn", "US", None, label="  My US  ")
    assert result["channel"].label == "My US"  # stripped and stored
