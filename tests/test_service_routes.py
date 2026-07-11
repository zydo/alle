"""Routing service operations and the restrict-only removal integrity."""

from __future__ import annotations

import pytest

from alle import cli, service
from alle.state import Store
from conftest import wg_config

WG = wg_config("1.2.3.4")


@pytest.fixture(autouse=True)
def no_background(monkeypatch):
    monkeypatch.setattr(service.daemon, "ensure_running", lambda: None)


@pytest.fixture
def channel():
    store = Store.load()
    store.add_provider("nordvpn")
    return store.add_channel("nordvpn", "US", "", dict(WG))  # id: us_1


def _add(mtype, value, target):
    """Create a singleton ruleset (replaces the removed service.routes_add shim)."""
    return service.routes_ruleset_create(
        target, target, [{"type": mtype, "value": value}]
    )


# ---- rulesets ------------------------------------------------------------------


def test_ruleset_create_returns_decorated_block(channel):
    result = service.routes_ruleset_create(
        "Streaming",
        "nordvpn/us_1",
        [{"value": "Netflix.COM"}, {"value": "api.netflix.com"}],
    )
    rs = result["ruleset"]
    assert rs["id"] == "rs1"
    assert rs["name"] == "Streaming"
    assert rs["target"] == "nordvpn/us_1"
    assert [r["id"] for r in rs["rules"]] == ["r1", "r2"]
    assert [r["match"] for r in rs["rules"]] == [
        "domain_suffix netflix.com",
        # default is suffix for every inferred domain (the two-label heuristic
        # that made api.netflix.com exact is gone); an explicit type opts into exact
        "domain_suffix api.netflix.com",
    ]


def test_ruleset_create_accepts_brand_name_and_rejects_bad_targets(channel):
    result = service.routes_ruleset_create("API", "NordVPN/us_1", [{"value": "a.com"}])
    assert result["ruleset"]["target"] == "nordvpn/us_1"
    with pytest.raises(service.ServiceError, match="unknown provider 'nope'"):
        service.routes_ruleset_create("Bad", "nope/us_1", [{"value": "a.com"}])
    with pytest.raises(service.ServiceError, match="no channel 'nordvpn/gone_1'"):
        service.routes_ruleset_create("Bad", "nordvpn/gone_1", [{"value": "a.com"}])


def test_ruleset_create_rejects_bad_values(channel):
    with pytest.raises(service.ServiceError, match="not a valid domain"):
        service.routes_ruleset_create("Bad", "direct", [{"value": "not a domain"}])
    with pytest.raises(service.ServiceError, match="not a valid IP or CIDR"):
        service.routes_ruleset_create(
            "Bad", "direct", [{"type": "ip_cidr", "value": "999.9.9.9/40"}]
        )
    with pytest.raises(service.ServiceError, match="not valid"):
        service.routes_ruleset_create("Bad", "sideways", [{"value": "a.com"}])


def test_ruleset_add_inherits_block_priority_and_reports_later_shadow(channel):
    service.routes_ruleset_create("US", "nordvpn/us_1", [{"value": "google.com"}])
    service.routes_ruleset_create("Direct", "direct", [{"value": "api.google.com"}])
    result = service.routes_ruleset_add("rs1", [{"value": "api.google.com"}])
    assert [r["id"] for r in result["ruleset"]["rules"]] == ["r1", "r3"]
    data = service.routes_list()
    later = data["rulesets"][1]["rules"][0]
    assert later["id"] == "r2" and later["shadowed_by"] == "r1"


def test_ruleset_update_keeps_id_position_name_and_matchers(channel):
    a = service.routes_ruleset_create("A", "nordvpn/us_1", [{"value": "a.com"}])
    service.routes_ruleset_create("B", "direct", [{"value": "b.com"}])
    rsid = a["ruleset"]["id"]

    result = service.routes_ruleset_update(
        rsid, "Renamed", "nordvpn/us_1", [{"value": "x.com"}, {"value": "10.0.0.0/8"}]
    )
    rs = result["ruleset"]
    assert rs["id"] == rsid  # id + position preserved
    assert rs["name"] == "Renamed"
    assert rs["target"] == "nordvpn/us_1"
    assert [r["match"] for r in rs["rules"]] == [
        "domain_suffix x.com",
        "ip_cidr 10.0.0.0/8",
    ]
    assert [r["id"] for r in service.routes_list()["rulesets"]] == [rsid, "rs2"]


def test_ruleset_update_retargets_and_validates(channel):
    a = service.routes_ruleset_create("A", "nordvpn/us_1", [{"value": "a.com"}])
    rsid = a["ruleset"]["id"]
    retargeted = service.routes_ruleset_update(
        rsid, "A", "direct", [{"value": "a.com"}]
    )
    assert retargeted["ruleset"]["target"] == "direct"
    with pytest.raises(service.ServiceError, match="name cannot be empty"):
        service.routes_ruleset_update(rsid, "  ", "direct", [{"value": "a.com"}])
    with pytest.raises(service.ServiceError, match="not a valid domain"):
        service.routes_ruleset_update(rsid, "A", "direct", [{"value": "xxx"}])
    with pytest.raises(service.ServiceError, match="at least one matcher"):
        service.routes_ruleset_update(rsid, "A", "direct", [])


# ---- routes_list / routes_remove / killswitch -------------------------------------


def test_routes_list_annotates_and_filters(channel):
    service.routes_ruleset_create("US", "nordvpn/us_1", [{"value": "google.com"}])
    service.routes_ruleset_create("Direct", "direct", [{"value": "api.google.com"}])
    service.routes_ruleset_create(
        "Block local", "block", [{"type": "ip_cidr", "value": "10.0.0.0/8"}]
    )

    data = service.routes_list()
    assert [r["id"] for r in data["rules"]] == ["r1", "r2", "r3"]
    assert [rs["id"] for rs in data["rulesets"]] == ["rs1", "rs2", "rs3"]
    assert data["rules"][1]["shadowed_by"] == "r1"
    assert data["rulesets"][1]["shadowed_count"] == 1
    assert data["router"]["rule_count"] == 3
    assert data["router"]["unmatched"] == "direct"

    by_channel = service.routes_list(channel="us_1")  # bare id matches
    assert [r["id"] for r in by_channel["rules"]] == ["r1"]
    qualified = service.routes_list(channel="nordvpn/us_1")
    assert [r["id"] for r in qualified["rules"]] == ["r1"]


def test_routes_remove_reports_all_missing_ids(channel):
    _add("domain_suffix", "a.com", "direct")
    with pytest.raises(service.ServiceError, match="r7, r9"):
        service.routes_remove(["r1", "r7", "r9"])
    assert [r["id"] for r in Store.load().rules()] == ["r1"]  # nothing removed


def test_routes_remove_dry_run_then_real(channel):
    _add("domain_suffix", "a.com", "direct")
    dry = service.routes_remove(["r1"], dry_run=True)
    assert dry["dry_run"] is True
    assert [r["id"] for r in Store.load().rules()] == ["r1"]
    real = service.routes_remove(["r1"])
    assert real["dry_run"] is False
    assert Store.load().rules() == []


def test_routes_reorder_persists_and_recomputes_shadow_lint(channel):
    service.routes_ruleset_create("API", "nordvpn/us_1", [{"value": "api.google.com"}])
    service.routes_ruleset_create("Google", "direct", [{"value": "google.com"}])
    service.routes_ruleset_create(
        "Block local", "block", [{"type": "ip_cidr", "value": "10.0.0.0/8"}]
    )

    result = service.routes_reorder(["rs2", "rs1", "rs3"])

    assert result["changed"] is True
    assert [rs["id"] for rs in result["rulesets"]] == ["rs2", "rs1", "rs3"]
    assert result["rulesets"][1]["rules"][0]["shadowed_by"] == "r2"
    assert [rs["id"] for rs in service.routes_list()["rulesets"]] == [
        "rs2",
        "rs1",
        "rs3",
    ]


def test_routes_reorder_rejects_invalid_permutations_without_mutating(channel):
    service.routes_ruleset_create("A", "direct", [{"value": "a.com"}])
    service.routes_ruleset_create("B", "direct", [{"value": "b.com"}])
    service.routes_ruleset_create("C", "direct", [{"value": "c.com"}])

    cases = [
        (["rs1", "rs1", "rs3"], "duplicate ruleset"),
        (["rs1", "rs2", "rs9"], "unknown ruleset"),
        (["rs1", "rs2"], "missing ruleset"),
    ]
    for ids, msg in cases:
        with pytest.raises(service.ServiceError, match=msg):
            service.routes_reorder(ids)
        assert [rs["id"] for rs in Store.load().rulesets()] == ["rs1", "rs2", "rs3"]


def test_routes_reorder_noop_is_reported(channel):
    service.routes_ruleset_create("A", "direct", [{"value": "a.com"}])
    result = service.routes_reorder(["rs1"])
    assert result["changed"] is False
    assert [rs["id"] for rs in result["rulesets"]] == ["rs1"]


def test_killswitch_toggles_unmatched_behavior():
    assert service.routes_killswitch()["router"]["unmatched"] == "direct"
    on = service.routes_killswitch(True)
    assert on["router"]["killswitch"] is True and on["router"]["unmatched"] == "block"
    off = service.routes_killswitch(False)
    assert off["router"]["unmatched"] == "direct"


def test_lan_direct_defaults_on_and_toggles():
    from alle import routes as routes_mod

    report = service.routes_lan_direct()
    assert report["changed"] is False
    assert report["router"]["lan_direct"] is True
    assert report["cidrs"] == list(routes_mod.LAN_DIRECT_CIDRS)
    off = service.routes_lan_direct(False)
    assert off["changed"] is True and off["router"]["lan_direct"] is False
    assert service.routes_lan_direct(True)["router"]["lan_direct"] is True


def test_status_snapshot_carries_router_info():
    router = service.status_snapshot()["router"]
    assert router["rule_count"] == 0
    assert router["unmatched"] == "direct"
    assert router["lan_direct"] is True


# ---- restrict-only removal integrity ----------------------------------------------


def test_referenced_channel_removal_is_blocked_with_fix_commands(channel):
    _add("domain_suffix", "netflix.com", "nordvpn/us_1")
    _add("all", "", "nordvpn/us_1")

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
    _add("domain_suffix", "a.com", "nordvpn/us_1")
    with pytest.raises(service.ServiceError, match="alle routes rm r1"):
        service.provider_remove_many(["nordvpn"])
    assert Store.load().has_provider("nordvpn")


def test_unreferenced_removal_works_after_rules_are_gone(channel):
    _add("domain_suffix", "a.com", "nordvpn/us_1")
    service.routes_remove(["r1"])
    result = service.channel_remove_many(["us_1"])
    assert result["channels"][0]["channel"] == "us_1"


def test_direct_and_block_targets_never_block_removal(channel):
    _add("domain_suffix", "a.com", "direct")
    _add("domain_suffix", "b.com", "block")
    assert service.channel_remove_many(["us_1"])["channels"]


# ---- CLI round trip ----------------------------------------------------------------


def run_cli(args, capsys):
    cli.main(args)
    return capsys.readouterr().out.rstrip("\n")


def test_cli_routes_round_trip(channel, capsys):
    out = run_cli(
        [
            "routes",
            "ruleset",
            "create",
            "Streaming",
            "--via",
            "nordvpn/us_1",
            "--domain",
            "netflix.com",
        ],
        capsys,
    )
    assert "Added ruleset rs1 'Streaming': 1 matcher(s) → nordvpn/us_1." in out

    out = run_cli(
        [
            "routes",
            "ruleset",
            "create",
            "Direct",
            "--via",
            "direct",
            "--domain",
            "api.netflix.com",
        ],
        capsys,
    )
    assert "shadowed by earlier rule r1" in out

    out = run_cli(["routes", "reorder", "rs2", "rs1"], capsys)
    assert "Reordered 2 rulesets" in out
    assert "WARNING:" not in out

    out = run_cli(["routes", "killswitch", "on"], capsys)
    assert "unmatched router traffic is blocked" in out

    out = run_cli(["routes", "lan"], capsys)
    assert "LAN direct ON" in out
    out = run_cli(["routes", "lan", "off"], capsys)
    assert "LAN direct off" in out
    out = run_cli(["routes", "lan", "on", "-v"], capsys)
    assert "LAN direct ON" in out and "10.0.0.0/8" in out

    out = run_cli(["routes", "rm", "r1", "r2"], capsys)
    assert "Removed matcher r1" in out and "Removed matcher r2" in out

    out = run_cli(["routes", "ls"], capsys)
    assert "No routing rulesets" in out


def test_cli_blocked_channel_rm_shows_blockers(channel, capsys):
    run_cli(
        ["routes", "ruleset", "create", "Default", "--via", "nordvpn/us_1", "--all"],
        capsys,
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "rm", "us_1"])
    assert "alle routes rm r1" in str(exc.value)
