"""The container gateway profile (ALLE_GATEWAY=1): fail-closed declaration at
start, data-plane readiness gating, and the kill-switch recovery diagnostic.

Everything here is opt-in via the env knob — the first tests pin that a host
(no knob) sees no gateway behavior at all.
"""

from __future__ import annotations

import pytest

from alle import reconnect, service
from alle.state import Store
from conftest import wg_config

WG = wg_config("1.2.3.4")


@pytest.fixture(autouse=True)
def reset_gateway_profile(monkeypatch):
    monkeypatch.delenv("ALLE_GATEWAY", raising=False)


@pytest.fixture
def privileged(monkeypatch):
    """All three gateway privilege preconditions granted (and tun allowed)."""
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(service, "_dev_net_tun_exists", lambda: True)
    monkeypatch.setattr(service, "_has_net_admin_capability", lambda: True)
    monkeypatch.setattr(service, "_singbox_has_net_admin", lambda: True)
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: None)


def test_profile_is_inert_by_default():
    assert service.gateway_profile_active() is False
    result = service.health()
    assert "gateway" not in result  # a host health never grows gateway checks


def test_gateway_init_fails_closed_listing_every_missing_privilege(monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 501)
    monkeypatch.setattr(service, "_dev_net_tun_exists", lambda: False)
    monkeypatch.setattr(service, "_has_net_admin_capability", lambda: False)
    with pytest.raises(service.ServiceError) as e:
        service.gateway_init()
    msg = str(e.value)
    assert "ALLE_RUN_AS_ROOT" in msg
    assert "/dev/net/tun" in msg
    assert "NET_ADMIN" in msg
    router = Store.load().router  # blocked before any state was touched
    assert router["tun"] is False and router["killswitch"] is False


def test_gateway_init_declares_killswitch_and_tun(privileged):
    result = service.gateway_init()
    assert result["router"]["tun"] is True
    router = Store.load().router
    assert router["tun"] is True and router["killswitch"] is True
    # idempotent across container restarts
    service.gateway_init()
    router = Store.load().router
    assert router["tun"] is True and router["killswitch"] is True


def test_gateway_init_overrides_an_adhoc_killswitch_off(privileged):
    store = Store.load()
    store.set_killswitch(False)
    service.gateway_init()
    assert Store.load().router["killswitch"] is True


def test_gateway_init_supersedes_a_pending_tun_trial(privileged, monkeypatch):
    monkeypatch.setattr(service, "_spawn_tun_watchdog", lambda secs, nonce: None)
    service.tun_trial_arm(60)
    assert service._tun_trial_read() is not None
    service.gateway_init()  # a declared profile is not a trial
    assert service._tun_trial_read() is None


# ---- readiness: the data-plane contract ---------------------------------------


@pytest.fixture
def gateway_env(monkeypatch, privileged):
    monkeypatch.setenv("ALLE_GATEWAY", "1")
    # daemon + sing-box process liveness held green so the tests isolate the
    # gateway conditions
    monkeypatch.setattr(service.daemon, "running_pid", lambda: 4242)
    monkeypatch.setattr(
        service.daemon,
        "daemon_info",
        lambda: {"runtime": {"singbox": "ok", "detail": ""}},
    )
    monkeypatch.setattr(service.singbox.Runner, "is_running", lambda self: True)
    monkeypatch.setattr(service.singbox.Runner, "control_alive", lambda self: True)
    monkeypatch.setattr(service, "_tun_interface_present", lambda: True)


def seed_viable_channel():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    store.set_probe("nordvpn", ch.id, {"ok": True, "latency_ms": 10})
    return ch


def test_gateway_readiness_red_until_every_condition_holds(gateway_env):
    # nothing declared yet: policy + viability both red, and health.ok is red
    # even though daemon/sing-box are up — a PID is not a gateway
    result = service.health()
    assert result["daemon"] and result["singbox"]
    assert result["ok"] is False
    assert "declared_policy" in result["gateway"]["failing"]
    assert "viable_channel" in result["gateway"]["failing"]

    service.gateway_init()
    result = service.health()
    assert result["ok"] is False  # still no viable channel
    assert result["gateway"]["failing"] == ["viable_channel"]

    seed_viable_channel()
    result = service.health()
    assert result["ok"] is True
    assert result["gateway"] == {"ok": True, "failing": []}


def test_gateway_readiness_red_when_the_interface_is_gone(gateway_env, monkeypatch):
    service.gateway_init()
    seed_viable_channel()
    monkeypatch.setattr(service, "_tun_interface_present", lambda: False)
    result = service.health()
    assert result["ok"] is False
    assert result["gateway"]["failing"] == ["tun_interface"]


def test_gateway_readiness_red_when_control_is_dead(gateway_env, monkeypatch):
    service.gateway_init()
    seed_viable_channel()
    monkeypatch.setattr(service.singbox.Runner, "control_alive", lambda self: False)
    result = service.health()
    assert result["ok"] is False
    assert result["gateway"]["failing"] == ["singbox_control"]


def test_gateway_readiness_red_until_desired_generation_is_accepted(
    gateway_env, monkeypatch
):
    service.gateway_init()
    seed_viable_channel()
    monkeypatch.setattr(
        service.daemon,
        "daemon_info",
        lambda: {"runtime": {"singbox": "config_rejected", "detail": "bad route"}},
    )
    result = service.health()
    assert result["ok"] is False
    assert result["gateway"]["failing"] == ["runtime_generation"]


def test_gateway_readiness_red_when_privileges_were_dropped(gateway_env, monkeypatch):
    service.gateway_init()
    seed_viable_channel()
    monkeypatch.setattr(service, "_has_net_admin_capability", lambda: False)
    result = service.health()
    assert result["ok"] is False
    assert result["gateway"]["failing"] == ["privileges"]


def test_gateway_readiness_red_when_only_disabled_channels_pass(gateway_env):
    service.gateway_init()
    ch = seed_viable_channel()
    Store.load().set_channels_enabled([("nordvpn", ch.id)], False)
    result = service.health()
    assert "viable_channel" in result["gateway"]["failing"]


def test_cli_health_shows_gateway_state(gateway_env, capsys):
    from alle import cli

    service.gateway_init()
    with pytest.raises(SystemExit):
        cli.main(["health"])
    out = capsys.readouterr().out
    assert "gateway=NOT-READY:viable_channel" in out

    seed_viable_channel()
    cli.main(["health"])
    out = capsys.readouterr().out
    assert "gateway=ready" in out


# ---- fail-closed control-plane recovery diagnostic -----------------------------


def test_killswitch_diagnostic_only_under_tun_plus_killswitch():
    store = Store.load()
    assert reconnect._killswitch_diagnostic(store) == ""
    store.set_killswitch(True)
    assert reconnect._killswitch_diagnostic(Store.load()) == ""  # no tun: proxy mode
    Store.load().set_tun(True)
    note = reconnect._killswitch_diagnostic(Store.load())
    assert "fail-closed by design" in note
    assert "alle routes killswitch off" in note
