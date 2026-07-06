"""Routing service operations and the restrict-only removal integrity."""

from __future__ import annotations

import pytest

from alle import cli, service
from alle.state import Store

WG = {
    "private_key": "PRIV=",
    "address": ["10.5.0.2/32"],
    "peer": {
        "public_key": "PUB=",
        "endpoint_host": "1.2.3.4",
        "endpoint_port": 51820,
        "preshared_key": None,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "keepalive": 25,
    },
}


@pytest.fixture(autouse=True)
def no_background(monkeypatch):
    monkeypatch.setattr(service.daemon, "ensure_running", lambda: None)


@pytest.fixture
def channel():
    store = Store.load()
    store.add_provider("nordvpn")
    return store.add_channel("nordvpn", "US", "", dict(WG))  # id: us_1


# ---- routes_add ------------------------------------------------------------------


def test_routes_add_returns_decorated_rule(channel):
    result = service.routes_add("domain_suffix", "Netflix.COM", "nordvpn/us_1")
    rule = result["rule"]
    assert rule["id"] == "r1"
    assert rule["match"] == "domain_suffix netflix.com"  # normalized
    assert rule["target"] == "nordvpn/us_1"
    assert result["shadowed_by"] is None


def test_routes_add_accepts_brand_name_in_target(channel):
    result = service.routes_add("domain", "a.com", "NordVPN/us_1")
    assert result["rule"]["target"] == "nordvpn/us_1"


def test_routes_add_rejects_unknown_provider_and_channel(channel):
    with pytest.raises(service.ServiceError, match="unknown provider 'nope'"):
        service.routes_add("domain", "a.com", "nope/us_1")
    with pytest.raises(service.ServiceError, match="no channel 'nordvpn/gone_1'"):
        service.routes_add("domain", "a.com", "nordvpn/gone_1")


def test_routes_add_rejects_bad_values(channel):
    with pytest.raises(service.ServiceError, match="not a valid domain"):
        service.routes_add("domain", "not a domain", "direct")
    with pytest.raises(service.ServiceError, match="not a valid IP or CIDR"):
        service.routes_add("ip_cidr", "999.9.9.9/40", "direct")
    with pytest.raises(service.ServiceError, match="not valid"):
        service.routes_add("domain", "a.com", "sideways")


def test_routes_add_warns_when_shadowed(channel):
    service.routes_add("domain_suffix", "google.com", "direct")
    result = service.routes_add("domain", "api.google.com", "nordvpn/us_1")
    assert result["shadowed_by"] == "r1"


# ---- routes_list / routes_remove / killswitch -------------------------------------


def test_routes_list_annotates_and_filters(channel):
    service.routes_add("domain_suffix", "google.com", "nordvpn/us_1")
    service.routes_add("domain", "api.google.com", "direct")  # shadowed by r1
    service.routes_add("ip_cidr", "10.0.0.0/8", "block")

    data = service.routes_list()
    assert [r["id"] for r in data["rules"]] == ["r1", "r2", "r3"]
    assert data["rules"][1]["shadowed_by"] == "r1"
    assert data["router"]["rule_count"] == 3
    assert data["router"]["unmatched"] == "direct"

    by_channel = service.routes_list(channel="us_1")  # bare id matches
    assert [r["id"] for r in by_channel["rules"]] == ["r1"]
    qualified = service.routes_list(channel="nordvpn/us_1")
    assert [r["id"] for r in qualified["rules"]] == ["r1"]


def test_routes_remove_reports_all_missing_ids(channel):
    service.routes_add("domain", "a.com", "direct")
    with pytest.raises(service.ServiceError, match="r7, r9"):
        service.routes_remove(["r1", "r7", "r9"])
    assert [r["id"] for r in Store.load().rules()] == ["r1"]  # nothing removed


def test_routes_remove_dry_run_then_real(channel):
    service.routes_add("domain", "a.com", "direct")
    dry = service.routes_remove(["r1"], dry_run=True)
    assert dry["dry_run"] is True
    assert [r["id"] for r in Store.load().rules()] == ["r1"]
    real = service.routes_remove(["r1"])
    assert real["dry_run"] is False
    assert Store.load().rules() == []


def test_killswitch_toggles_unmatched_behavior():
    assert service.routes_killswitch()["router"]["unmatched"] == "direct"
    on = service.routes_killswitch(True)
    assert on["router"]["killswitch"] is True and on["router"]["unmatched"] == "block"
    off = service.routes_killswitch(False)
    assert off["router"]["unmatched"] == "direct"


def test_status_snapshot_carries_router_info():
    router = service.status_snapshot()["router"]
    assert router["rule_count"] == 0
    assert router["unmatched"] == "direct"


# ---- restrict-only removal integrity ----------------------------------------------


def test_referenced_channel_removal_is_blocked_with_fix_commands(channel):
    service.routes_add("domain_suffix", "netflix.com", "nordvpn/us_1")
    service.routes_add("all", "", "nordvpn/us_1")

    with pytest.raises(service.ServiceError) as exc:
        service.channel_remove_many(["us_1"])
    msg = str(exc.value)
    assert "nordvpn/us_1" in msg
    assert "r1" in msg and "r2" in msg  # every blocker in one pass
    assert "alle routes rm r1 r2" in msg  # with the exact fix
    assert Store.load().get_channel("nordvpn", "us_1") is not None

    # dry-run reports the same conflict instead of pretending it would work
    with pytest.raises(service.ServiceError, match="routing rules"):
        service.channel_remove_many(["us_1"], dry_run=True)


def test_referenced_provider_removal_is_blocked(channel):
    service.routes_add("domain", "a.com", "nordvpn/us_1")
    with pytest.raises(service.ServiceError, match="alle routes rm r1"):
        service.provider_remove_many(["nordvpn"])
    assert Store.load().has_provider("nordvpn")


def test_unreferenced_removal_works_after_rules_are_gone(channel):
    service.routes_add("domain", "a.com", "nordvpn/us_1")
    service.routes_remove(["r1"])
    result = service.channel_remove_many(["us_1"])
    assert result["channels"][0]["channel"] == "us_1"


def test_direct_and_block_targets_never_block_removal(channel):
    service.routes_add("domain", "a.com", "direct")
    service.routes_add("domain", "b.com", "block")
    assert service.channel_remove_many(["us_1"])["channels"]


# ---- CLI round trip ----------------------------------------------------------------


def run_cli(args, capsys):
    cli.main(args)
    return capsys.readouterr().out.rstrip("\n")


def test_cli_routes_round_trip(channel, capsys):
    out = run_cli(
        ["routes", "add", "nordvpn/us_1", "--domain-suffix", "netflix.com"], capsys
    )
    assert "Added rule r1: domain_suffix netflix.com → nordvpn/us_1." in out

    out = run_cli(["routes", "add", "direct", "--domain", "api.netflix.com"], capsys)
    assert "WARNING: shadowed by earlier rule r1" in out

    out = run_cli(["routes", "ls"], capsys)
    assert "Router entrypoint" in out
    assert "shadowed by r1" in out

    out = run_cli(["routes", "killswitch", "on"], capsys)
    assert "unmatched router traffic is blocked" in out

    out = run_cli(["routes", "rm", "r1", "r2"], capsys)
    assert "Removed rule r1" in out and "Removed rule r2" in out

    out = run_cli(["routes", "ls"], capsys)
    assert "No routing rules" in out


def test_cli_blocked_channel_rm_shows_blockers(channel, capsys):
    run_cli(["routes", "add", "nordvpn/us_1", "--all"], capsys)
    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "rm", "us_1"])
    assert "alle routes rm r1" in str(exc.value)
