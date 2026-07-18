"""Routing service operations and the restrict-only removal integrity."""

from __future__ import annotations

import pytest

from alle import cli, service
from alle.state import Store
from conftest import wg_config

WG = wg_config("1.2.3.4")


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
    assert router["tun"] is False


# ---- TUN mode ----------------------------------------------------------------


def test_tun_mode_reports_without_touching_state():
    report = service.tun_mode()
    assert report["changed"] is False
    assert report["router"]["tun"] is False


def test_tun_on_requires_root(monkeypatch):
    monkeypatch.setattr("alle.helper.reachable", lambda: False)
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: None)
    monkeypatch.setattr("os.geteuid", lambda: 501)
    with pytest.raises(service.ServiceError, match="privileged helper"):
        service.tun_mode(True)
    assert Store.load().router["tun"] is False  # gate fails before state moves
    # the hint is platform-specific: on macOS it leads with installing the
    # helper (the shipped path, not the stale "helper is planned" framing) —
    # pin the platform so the assertion holds on Linux CI too
    monkeypatch.setattr(service.sys, "platform", "darwin")
    with pytest.raises(service.ServiceError, match="sudo alle helper install"):
        service.tun_mode(True)
    # …and on Linux it leads with the setcap grant
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "_singbox_has_net_admin", lambda: False)
    with pytest.raises(service.ServiceError, match="setcap cap_net_admin"):
        service.tun_mode(True)


def test_tun_on_requires_a_root_daemon_when_one_is_running(monkeypatch):
    monkeypatch.setattr("alle.helper.reachable", lambda: False)
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: {"pid": 4242})
    monkeypatch.setattr(service, "_process_uid", lambda pid: 501)
    with pytest.raises(service.ServiceError, match="privileged helper"):
        service.tun_mode(True)


def test_tun_on_allowed_when_singbox_has_net_admin(monkeypatch):
    # The Linux setcap path: a capability on the binary means no root is
    # needed anywhere, even with an unprivileged daemon running.
    monkeypatch.setattr("alle.helper.reachable", lambda: False)
    monkeypatch.setattr(service, "_singbox_has_net_admin", lambda: True)
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: {"pid": 4242})
    monkeypatch.setattr(service, "_process_uid", lambda pid: 501)
    monkeypatch.setattr("os.geteuid", lambda: 501)
    on = service.tun_mode(True)
    assert on["changed"] is True and on["router"]["tun"] is True


def test_tun_on_allowed_when_live_process_has_net_admin(monkeypatch):
    # The live process's CapEff is authoritative: a running sing-box that
    # already holds CAP_NET_ADMIN passes the gate even when the binary check
    # cannot confirm the capability (e.g. the binary was since replaced).
    monkeypatch.setattr(service, "_running_singbox_has_net_admin", lambda: True)
    monkeypatch.setattr(service, "_singbox_has_net_admin", lambda: False)
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: {"pid": 4242})
    monkeypatch.setattr(service, "_process_uid", lambda pid: 501)
    monkeypatch.setattr("os.geteuid", lambda: 501)
    on = service.tun_mode(True)
    assert on["changed"] is True and on["router"]["tun"] is True


def test_capeff_net_admin_bit_parsing():
    # CAP_NET_ADMIN is bit 12: 0x1000 has it, 0x0800 does not.
    has = "Name:\tsing-box\nCapEff:\t0000000000003000\n"
    lacks = "Name:\tsing-box\nCapEff:\t0000000000000800\n"
    assert service._capeff_has_net_admin(has) is True
    assert service._capeff_has_net_admin(lacks) is False
    assert service._capeff_has_net_admin("no such line") is False
    assert service._capeff_has_net_admin("CapEff:\tnot-hex\n") is False


def test_tun_root_error_mentions_setcap_on_linux(monkeypatch):
    monkeypatch.setattr("alle.helper.reachable", lambda: False)
    monkeypatch.setattr(service, "_singbox_has_net_admin", lambda: False)
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: None)
    monkeypatch.setattr("os.geteuid", lambda: 501)
    monkeypatch.setattr(service.sys, "platform", "linux")
    with pytest.raises(service.ServiceError, match="setcap cap_net_admin"):
        service.tun_mode(True)


def test_tun_toggles_when_privileged(monkeypatch):
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: None)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    on = service.tun_mode(True)
    assert on["changed"] is True and on["router"]["tun"] is True
    # disabling never needs privileges — it must always be possible
    monkeypatch.setattr("os.geteuid", lambda: 501)
    off = service.tun_mode(False)
    assert off["changed"] is True and off["router"]["tun"] is False


# ---- TUN trial (the iptables-apply pattern) -----------------------------------


@pytest.fixture
def rooted(monkeypatch):
    """Pretend to be root with no daemon, and capture the watchdog spawn."""
    spawned = []
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: None)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(
        service,
        "_spawn_tun_watchdog",
        lambda secs, nonce: spawned.append((secs, nonce)),
    )
    return spawned


def test_tun_trial_arms_watchdog_before_activation(rooted):
    result = service.tun_trial_arm(60)
    assert result["router"]["tun"] is True
    marker = service._tun_trial_read()
    assert marker and rooted == [(60, marker["nonce"])]
    # the pending trial is visible in the plain report
    assert service.tun_mode()["trial"]["nonce"] == marker["nonce"]


def test_tun_trial_expire_reverts_only_the_matching_trial(rooted):
    service.tun_trial_arm(60)
    marker = service._tun_trial_read()
    assert marker is not None
    nonce = marker["nonce"]
    assert service.tun_trial_expire("stale-nonce") is False  # superseded watchdog
    assert Store.load().router["tun"] is True
    assert service.tun_trial_expire(nonce) is True
    assert Store.load().router["tun"] is False
    assert service._tun_trial_read() is None
    assert service.tun_trial_expire(nonce) is False  # already handled


def test_tun_trial_confirm_keeps_tun_on(rooted):
    service.tun_trial_arm(60)
    marker = service._tun_trial_read()
    assert marker is not None
    nonce = marker["nonce"]
    assert service.tun_trial_confirm()["confirmed"] is True
    assert service.tun_trial_expire(nonce) is False  # watchdog finds no marker
    assert Store.load().router["tun"] is True
    with pytest.raises(service.ServiceError, match="no TUN trial is pending"):
        service.tun_trial_confirm()


def test_explicit_toggle_supersedes_a_pending_trial(rooted):
    service.tun_trial_arm(60)
    marker = service._tun_trial_read()
    assert marker is not None
    nonce = marker["nonce"]
    service.tun_mode(False)  # a human decision clears the marker
    assert service._tun_trial_read() is None
    assert service.tun_trial_expire(nonce) is False


def test_tun_trial_window_is_bounded(rooted):
    with pytest.raises(service.ServiceError, match="5 to 3600"):
        service.tun_trial_arm(2)
    with pytest.raises(service.ServiceError, match="5 to 3600"):
        service.tun_trial_arm(9999)
    assert Store.load().router["tun"] is False  # nothing armed, nothing flipped


def test_tun_trial_marker_is_durable_and_private(rooted):
    import stat

    service.tun_trial_arm(60)
    path = service._tun_trial_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    marker = service._tun_trial_read()
    assert marker is not None and marker["nonce"] and marker["deadline"] > 0


def test_tun_trial_read_rejects_invalid_markers(rooted):
    path = service._tun_trial_path()
    for bad in (
        '"a string"',
        '{"nonce": "", "deadline": 99}',
        '{"nonce": "x"}',
        '{"nonce": "x", "deadline": "soon"}',
        "not json",
    ):
        path.write_text(bad)
        assert service._tun_trial_read() is None


def test_tun_trial_expire_after_confirm_never_reverts(rooted):
    # the serialized transition order: confirm consumed the marker, so the
    # expiry that raced it acts on nothing — a reported confirmation stays
    service.tun_trial_arm(60)
    marker = service._tun_trial_read()
    assert marker is not None
    assert service.tun_trial_confirm()["confirmed"] is True
    assert service.tun_trial_expire(marker["nonce"]) is False
    assert Store.load().router["tun"] is True
    # ...and after an expiry wins, a confirm reports no pending trial
    service.tun_trial_arm(60)
    marker = service._tun_trial_read()
    assert service.tun_trial_expire(marker["nonce"]) is True
    with pytest.raises(service.ServiceError, match="no TUN trial is pending"):
        service.tun_trial_confirm()


# ---- TUN trial recovery (power loss / container recreation) --------------------


def test_tun_trial_recover_reverts_an_expired_trial(rooted):
    import time

    service.tun_trial_arm(60)
    assert Store.load().router["tun"] is True
    # simulate a restart after the deadline: the watchdog process is gone
    service._tun_trial_write("orphan", int(time.time()) - 5)
    result = service.tun_trial_recover()
    assert result == {"action": "reverted_expired"}
    assert Store.load().router["tun"] is False
    assert service._tun_trial_read() is None


def test_tun_trial_recover_rearms_a_live_trial(rooted):
    import time

    service.tun_trial_arm(60)
    marker = service._tun_trial_read()
    rooted.clear()  # forget the original arm's watchdog
    remaining = marker["deadline"] - int(time.time())
    result = service.tun_trial_recover()
    assert result is not None and result["action"] == "rearmed"
    assert 0 < result["remaining"] <= remaining
    # a fresh watchdog for the remaining window, same nonce (duplicates of
    # the dead one would be harmless under nonce serialization anyway)
    assert rooted == [(result["remaining"], marker["nonce"])]
    assert Store.load().router["tun"] is True  # a live trial keeps tun on


def test_tun_trial_recover_fails_closed_on_unreadable_marker(rooted):
    service.tun_trial_arm(60)
    service._tun_trial_path().write_text("{corrupt")
    result = service.tun_trial_recover()
    assert result == {"action": "reverted_invalid"}
    assert Store.load().router["tun"] is False  # unknown trial -> off
    assert not service._tun_trial_path().exists()


def test_tun_trial_recover_is_a_noop_without_a_marker(rooted):
    assert service.tun_trial_recover() is None
    # a confirmed (marker-less) tun stays on across restarts
    service.tun_trial_arm(60)
    service.tun_trial_confirm()
    assert service.tun_trial_recover() is None
    assert Store.load().router["tun"] is True


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


def test_cli_tun_round_trip(monkeypatch, capsys):
    out = run_cli(["tun"], capsys)
    assert "TUN mode off" in out and "alle tun on" in out

    monkeypatch.setattr(service.daemon, "daemon_info", lambda: None)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    out = run_cli(["tun", "on"], capsys)
    assert "TUN mode ON" in out and "no kill-switch" in out

    # the kill-switch scope framing shifts to system-wide while tun is on
    out = run_cli(["routes", "killswitch", "on"], capsys)
    assert "Applies system-wide" in out
    out = run_cli(["tun"], capsys)
    assert "kill-switch is system-wide" in out

    out = run_cli(["tun", "off"], capsys)
    assert "TUN mode off" in out


def test_cli_tun_trial_round_trip(monkeypatch, capsys):
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: None)
    monkeypatch.setattr("os.geteuid", lambda: 0)
    monkeypatch.setattr(service, "_spawn_tun_watchdog", lambda secs, nonce: None)

    out = run_cli(["tun", "on", "--trial", "60"], capsys)
    assert "trial window: 60s" in out and "alle tun confirm" in out

    out = run_cli(["tun"], capsys)
    assert "TUN trial pending" in out and "TUN mode ON" in out

    out = run_cli(["tun", "confirm"], capsys)
    assert "stays on" in out
    assert Store.load().router["tun"] is True

    with pytest.raises(SystemExit):
        cli.main(["tun", "off", "--trial", "60"])  # --trial only applies to on


def test_cli_blocked_channel_rm_shows_blockers(channel, capsys):
    run_cli(
        ["routes", "ruleset", "create", "Default", "--via", "nordvpn/us_1", "--all"],
        capsys,
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "rm", "us_1"])
    assert "alle routes rm r1" in str(exc.value)
