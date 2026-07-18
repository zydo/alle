"""Focused service orchestration tests with external collaborators stubbed."""

from __future__ import annotations

import pytest

from alle import service
from alle.providers import ProviderError


WG = {"private_key": "x", "peer": {}}


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
            "has_token": True,
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


def test_test_channel_filter_skips_nonmatching_channels():
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.add_channel("nordvpn", "United States", "", dict(WG))

    result = service.test(channel="japan_1")

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


def test_locations_list_uses_valid_stale_cache_when_background_refresh_fails(
    monkeypatch,
):
    monkeypatch.setattr(service.locations, "needs_refresh", lambda state, p: True)
    monkeypatch.setattr(
        service.locations,
        "update",
        lambda state, providers: (_ for _ in ()).throw(ProviderError("offline")),
    )
    monkeypatch.setattr(
        service.locations, "load", lambda state, provider: {"Japan": ["Tokyo"]}
    )

    result = service.locations_list("nordvpn")

    assert result["stale"] is True
    assert "using stale cache" in result["warning"]
    assert result["countries"] == [{"country": "Japan", "cities": ["Tokyo"]}]


def test_locations_forced_refresh_reports_failure_and_preserves_cache(monkeypatch):
    monkeypatch.setattr(
        service.locations,
        "update",
        lambda state, providers: (_ for _ in ()).throw(ProviderError("offline")),
    )

    with pytest.raises(ProviderError, match="offline"):
        service.locations_list("nordvpn", refresh=True)


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
    # isolate from the host's actual login-service plist: restart() branches on
    # daemonctl.is_installed(), which otherwise reflects the host machine.
    monkeypatch.setattr(service.daemonctl, "is_installed", lambda: False)

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


def test_restart_uses_atomic_manager_restart_when_supervised(monkeypatch):
    """With a login service installed, restart is one supervisor operation —
    never stop+start, whose failure window leaves the daemon down."""
    events = []

    class Runner:
        def is_running(self):
            return True

        def stop(self):
            events.append("singbox-stop")

    monkeypatch.setattr(service.singbox, "Runner", Runner)
    monkeypatch.setattr(service.daemonctl, "is_installed", lambda: True)
    monkeypatch.setattr(
        service.daemonctl,
        "restart_service",
        lambda: events.append("manager-restart") or True,
    )
    monkeypatch.setattr(
        service.daemon, "ensure_running", lambda: events.append("ensure")
    )
    monkeypatch.setattr(
        service.daemon, "stop", lambda: events.append("daemon-stop") or True
    )

    assert service.restart() == {"reconnect_cleared": 0}
    assert events == ["singbox-stop", "manager-restart"]


def test_web_ui_restart_schedules_external_lifecycle(monkeypatch):
    events = []
    store = service.Store.load()
    store.add_provider("nordvpn")
    channel = store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.set_reconnect("nordvpn", channel.id, {"attempts": 1})
    monkeypatch.setattr(service.daemon, "in_daemon_process", lambda: True)
    monkeypatch.setattr(
        service.daemon, "schedule_lifecycle", lambda action: events.append(action)
    )
    monkeypatch.setattr(
        service.daemon, "ensure_running", lambda: events.append("ensure")
    )

    assert service.restart() == {"reconnect_cleared": 1, "restarting": True}
    assert events == ["restart"]


def test_web_ui_stop_schedules_external_lifecycle(monkeypatch):
    events = []
    monkeypatch.setattr(service.daemon, "in_daemon_process", lambda: True)
    monkeypatch.setattr(
        service.daemon, "schedule_lifecycle", lambda action: events.append(action)
    )
    monkeypatch.setattr(service, "_stop_all", lambda: events.append("stop-now") or True)

    assert service.stop() == {"was_running": True, "stopping": True}
    assert events == ["stop"]


def test_channel_set_label_sets_clears_and_resolves():
    store = service.Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))  # id: us_1

    result = service.channel_set_label(ch.id, "  Streaming US  ")
    assert result["label"] == "Streaming US"  # stripped
    assert result["cleared"] is False
    ch = service.Store.load().get_channel("nordvpn", ch.id)
    assert ch is not None
    assert ch.label == "Streaming US"

    # qualified ref also works, and empty clears back to the id
    cleared = service.channel_set_label(f"nordvpn/{ch.id}", "")
    assert cleared["cleared"] is True
    ch = service.Store.load().get_channel("nordvpn", ch.id)
    assert ch is not None
    assert ch.label == ""


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


# ---- token replacement (Phase 5.5) ----------------------------------------


def _proton_conf(endpoint: str = "1.2.3.4:51820") -> str:
    import base64

    key = base64.b64encode(b"k" * 32).decode()
    pub = base64.b64encode(b"p" * 32).decode()
    return (
        f"[Interface]\nPrivateKey = {key}\nAddress = 10.0.0.2/32\n"
        f"[Peer]\nPublicKey = {pub}\nEndpoint = {endpoint}\n"
    )


def test_provider_update_token_replaces_and_reresolves(monkeypatch):
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.add_channel("nordvpn", "United States", "", dict(WG))
    service.credentials.set_("nordvpn", {"token": "old-token"})

    resolved_with = {}

    def fake_resolver(provider, creds):
        resolved_with["creds"] = creds
        return lambda country, city: {"private_key": "fresh", "peer": {"loc": country}}

    monkeypatch.setattr(service, "provider_resolver", fake_resolver)

    result = service.provider_update_token("nordvpn", {"token": "new-token"})

    assert result["updated"] is True
    assert resolved_with["creds"] == {"token": "new-token"}
    assert service.credentials.get("nordvpn") == {"token": "new-token"}
    assert sorted(result["channels"]["resolved"]) == ["japan_1", "united_states_1"]
    assert result["channels"]["failed"] == []
    # every channel got the freshly-resolved params
    reloaded = service.Store.load()
    ch = reloaded.get_channel("nordvpn", "japan_1")
    assert ch is not None
    assert ch.wg["private_key"] == "fresh"


def test_provider_update_token_same_token_is_noop(monkeypatch):
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "Japan", "", {"private_key": "keep", "peer": {}})
    service.credentials.set_("nordvpn", {"token": "same-token"})

    def must_not_resolve(provider, creds):
        raise AssertionError("re-resolve must not run for an identical token")

    monkeypatch.setattr(service, "provider_resolver", must_not_resolve)

    # A surrounding-whitespace variant is still "the same" (set_ strips).
    result = service.provider_update_token("nordvpn", {"token": "  same-token  "})

    assert result["updated"] is False
    assert result["unchanged"] is True
    assert result["channels"] == {"resolved": [], "failed": []}
    # the channel's params were left untouched
    ch = service.Store.load().get_channel("nordvpn", "japan_1")
    assert ch is not None
    assert ch.wg["private_key"] == "keep"


def test_provider_update_token_bad_token_keeps_old(monkeypatch):
    def reject(provider, creds):
        raise ProviderError("token rejected")

    monkeypatch.setattr(service, "validate_provider_credentials", reject)
    store = service.Store.load()
    store.add_provider("nordvpn")
    service.credentials.set_("nordvpn", {"token": "good-token"})

    with pytest.raises(ProviderError):
        service.provider_update_token("nordvpn", {"token": "bad-token"})

    assert service.credentials.get("nordvpn") == {"token": "good-token"}  # unchanged


def test_provider_update_token_per_channel_failure_keeps_old_wg(monkeypatch):
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "Japan", "", {"private_key": "keep", "peer": {}})
    service.credentials.set_("nordvpn", {"token": "t"})

    def failing_resolver(provider, creds):
        def resolve(country, city):
            raise ProviderError("no server")

        return resolve

    monkeypatch.setattr(service, "provider_resolver", failing_resolver)

    result = service.provider_update_token("nordvpn", {"token": "t2"})

    assert result["channels"] == {"resolved": [], "failed": ["japan_1"]}
    # the channel kept its old params (auto-reconnect will refresh it)
    ch = service.Store.load().get_channel("nordvpn", "japan_1")
    assert ch is not None
    assert ch.wg["private_key"] == "keep"


def test_provider_update_token_rolls_back_credential_when_commit_fails(monkeypatch):
    """The credential and its re-resolved channels commit together: if the
    state write (the commit point) fails, the journalled old token comes back."""
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)
    store = service.Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "Japan", "", {"private_key": "keep", "peer": {}})
    service.credentials.set_("nordvpn", {"token": "old"})
    monkeypatch.setattr(
        service,
        "provider_resolver",
        lambda p, c: lambda country, city="": {"private_key": "fresh", "peer": {}},
    )

    def boom(self, provider, wg_by_cid):
        raise RuntimeError("disk full")

    monkeypatch.setattr(service.Store, "update_channels_wg", boom)

    with pytest.raises(RuntimeError, match="disk full"):
        service.provider_update_token("nordvpn", {"token": "new"})

    # nothing half-applied: old token restored, old channel params intact
    assert service.credentials.get("nordvpn") == {"token": "old"}
    ch = service.Store.load().get_channel("nordvpn", "japan_1")
    assert ch is not None and ch.wg["private_key"] == "keep"


def test_provider_remove_rolls_back_credential_when_state_removal_fails(monkeypatch):
    store = service.Store.load()
    store.add_provider("nordvpn")
    service.credentials.set_("nordvpn", {"token": "keep"})

    def boom(self, providers):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(service.Store, "remove_providers", boom)

    with pytest.raises(RuntimeError, match="state write failed"):
        service.provider_remove_many(["nordvpn"])

    # the already-removed credential was rolled back with the failed removal
    assert service.credentials.get("nordvpn") == {"token": "keep"}
    assert service.Store.load().has_provider("nordvpn")


def test_provider_add_token_rolls_back_credential_when_state_add_fails(monkeypatch):
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)

    def boom(self, provider):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(service.Store, "add_provider", boom)

    with pytest.raises(RuntimeError, match="state write failed"):
        service.provider_add_token("nordvpn", {"token": "t"})

    assert service.credentials.get("nordvpn") is None  # no orphan credential


def test_provider_update_token_rejects_config_provider():
    store = service.Store.load()
    store.add_provider("protonvpn")
    with pytest.raises(service.ServiceError, match="no token to replace"):
        service.provider_update_token("protonvpn", {"token": "x"})


def test_provider_update_token_requires_added_provider():
    with pytest.raises(service.ServiceError, match="is not added"):
        service.provider_update_token("nordvpn", {"token": "x"})


def test_provider_add_or_update_token_dispatches(monkeypatch):
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)
    monkeypatch.setattr(
        service, "provider_resolver", lambda p, c: lambda a, b: dict(WG)
    )

    first = service.provider_add_or_update_token("nordvpn", {"token": "one"})
    assert first["updated"] is False  # fresh add
    second = service.provider_add_or_update_token("nordvpn", {"token": "two"})
    assert second["updated"] is True  # now a replace
    assert service.credentials.get("nordvpn") == {"token": "two"}


# ---- duplicate .conf detection --------------------------------------------


def test_reimport_identical_conf_reports_unchanged(tmp_path):
    store = service.Store.load()
    store.add_provider("protonvpn")
    conf = tmp_path / "wg-US-CA-9.conf"
    conf.write_text(_proton_conf())

    first = service.channel_add("protonvpn", None, None, str(conf))
    assert first["updated"] is False and first.get("unchanged") is False

    again = service.channel_add("protonvpn", None, None, str(conf))
    assert again["unchanged"] is True
    assert again["updated"] is False


def test_reimport_changed_conf_reports_updated(tmp_path):
    store = service.Store.load()
    store.add_provider("protonvpn")
    conf = tmp_path / "wg-US-CA-9.conf"
    conf.write_text(_proton_conf())
    service.channel_add("protonvpn", None, None, str(conf))

    conf.write_text(_proton_conf("9.9.9.9:51820"))
    changed = service.channel_add("protonvpn", None, None, str(conf))
    assert changed["unchanged"] is False
    assert changed["updated"] is True
