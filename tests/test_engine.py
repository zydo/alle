"""Engine: config assembly, reconcile, and heartbeat probing — with the sing-box
process stubbed out (pure logic, no downloads, no tunnels)."""

from __future__ import annotations

from typing import cast

import pytest

from alle import applog, routes, singbox
from alle.engine import Engine
from alle.state import Store
from conftest import wg_config

WG = wg_config("1.2.3.4")


def _store(*specs, router=None):
    """specs: (provider, id, port, country, city, wg) tuples."""
    data = {"version": 1, "providers": {}}
    if router is not None:
        data["router"] = router
    for provider, cid, port, country, city, wg in specs:
        prov = data["providers"].setdefault(provider, {"channels": {}})
        prov["channels"][cid] = {
            "country": country,
            "city": city,
            "port": port,
            "wg": wg,
            "probe": {},
        }
    return Store(data=data)


def _router(*rules, port=40000, killswitch=False, lan_direct=True):
    numbered = [
        {"id": f"r{i + 1}", "type": t, "value": v, "target": target}
        for i, (t, v, target) in enumerate(rules)
    ]
    return {
        "port": port,
        "killswitch": killswitch,
        "lan_direct": lan_direct,
        "rules": numbered,
    }


def test_each_channel_becomes_an_inbound_and_endpoint():
    store = _store(
        ("nordvpn", "us_1", 8888, "US", "", dict(WG)),
        ("nordvpn", "uk_1", 8889, "UK", "", dict(WG)),
    )
    config, errors = Engine(store)._build_config()
    assert errors == {}
    assert [i["tag"] for i in config["inbounds"]] == [
        "in-nordvpn-uk_1",
        "in-nordvpn-us_1",
    ]
    assert [e["tag"] for e in config["endpoints"]] == [
        "out-nordvpn-uk_1",
        "out-nordvpn-us_1",
    ]
    assert {"inbound": ["in-nordvpn-us_1"], "outbound": "out-nordvpn-us_1"} in config[
        "route"
    ]["rules"]
    assert config["route"]["final"] == "direct"


def test_inbound_and_endpoint_shape():
    config, _ = Engine(
        _store(("nordvpn", "us_1", 9000, "US", "", dict(WG)))
    )._build_config()
    inb = config["inbounds"][0]
    assert inb == {
        "type": "mixed",
        "tag": "in-nordvpn-us_1",
        "listen": "127.0.0.1",
        "listen_port": 9000,
    }
    ep = config["endpoints"][0]
    assert ep["type"] == "wireguard" and ep["system"] is False
    assert ep["private_key"] == "PRIV=" and ep["address"] == ["10.5.0.2/32"]
    peer = ep["peers"][0]
    assert peer["address"] == "1.2.3.4" and peer["port"] == 51820
    assert peer["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert "pre_shared_key" not in peer  # absent when the conf had none


def test_preshared_key_passed_through_when_present():
    wg = dict(WG)
    wg["peer"] = {**WG["peer"], "preshared_key": "PSK="}
    config, _ = Engine(_store(("nordvpn", "us_1", 9000, "US", "", wg)))._build_config()
    assert config["endpoints"][0]["peers"][0]["pre_shared_key"] == "PSK="


def test_config_authenticates_the_clash_api():
    config, _ = Engine(_store())._build_config()
    api = singbox.clash_api()
    assert config["experimental"]["clash_api"] == {
        "external_controller": api["address"],
        "secret": api["secret"],
    }


def test_router_entrypoint_compiles_rules_in_order():
    store = _store(
        ("nordvpn", "us_1", 8888, "US", "", dict(WG)),
        router=_router(
            ("domain_suffix", "api.google.com", "nordvpn/us_1"),
            ("domain_suffix", "netflix.com", "direct"),
            ("ip_cidr", "10.0.0.0/8", "block"),
            ("all", "", "nordvpn/us_1"),
        ),
    )
    config, errors = Engine(store)._build_config()
    assert errors == {}
    router_in = next(i for i in config["inbounds"] if i["tag"] == "in-router")
    assert router_in == {
        "type": "mixed",
        "tag": "in-router",
        "listen": "127.0.0.1",
        "listen_port": 40000,
    }
    rules = config["route"]["rules"]
    assert rules[0] == {"inbound": ["in-nordvpn-us_1"], "outbound": "out-nordvpn-us_1"}
    assert rules[1] == {"inbound": ["in-router"], "action": "sniff"}
    assert rules[2] == {
        "inbound": ["in-router"],
        "network": ["udp"],
        "port": list(routes.LAN_DIRECT_UDP_PORTS),
        "outbound": "direct",
    }  # the port half of the built-in LAN block (unicast DHCP/SSDP/mDNS)
    assert rules[3] == {
        "inbound": ["in-router"],
        "ip_cidr": list(routes.LAN_DIRECT_CIDRS),
        "outbound": "direct",
    }  # the built-in LAN block precedes every user rule
    assert rules[4] == {
        "inbound": ["in-router"],
        "domain_suffix": ["api.google.com"],
        "outbound": "out-nordvpn-us_1",
    }
    assert rules[5] == {
        "inbound": ["in-router"],
        "domain_suffix": ["netflix.com"],
        "outbound": "direct",
    }
    assert rules[6] == {
        "inbound": ["in-router"],
        "ip_cidr": ["10.0.0.0/8"],
        "action": "reject",
    }
    assert rules[7] == {"inbound": ["in-router"], "outbound": "out-nordvpn-us_1"}
    assert config["route"]["final"] == "direct"  # unmatched passes through


def test_killswitch_appends_a_trailing_reject():
    store = _store(router=_router(killswitch=True))
    config, _ = Engine(store)._build_config()
    assert config["route"]["rules"][-1] == {
        "inbound": ["in-router"],
        "action": "reject",
    }


def test_lan_direct_off_omits_the_builtin_block():
    store = _store(router=_router(("all", "", "direct"), lan_direct=False))
    config, _ = Engine(store)._build_config()
    assert all(
        r.get("ip_cidr") != list(routes.LAN_DIRECT_CIDRS)
        for r in config["route"]["rules"]
    )
    # the port half rides the same toggle — off means off for both
    assert all("port" not in r for r in config["route"]["rules"])


def test_lan_direct_defaults_on_when_key_is_absent():
    # a state.json written before the toggle existed has no lan_direct key
    router = _router()
    del router["lan_direct"]
    config, _ = Engine(_store(router=router))._build_config()
    assert {
        "inbound": ["in-router"],
        "ip_cidr": list(routes.LAN_DIRECT_CIDRS),
        "outbound": "direct",
    } in config["route"]["rules"]
    assert {
        "inbound": ["in-router"],
        "network": ["udp"],
        "port": list(routes.LAN_DIRECT_UDP_PORTS),
        "outbound": "direct",
    } in config["route"]["rules"]


def test_tun_inbound_and_dns_shape():
    import sys as _sys

    router = _router()
    router["tun"] = True
    config, errors = Engine(_store(router=router))._build_config()
    assert errors == {}
    tun = next(i for i in config["inbounds"] if i["tag"] == "in-tun")
    expected = {
        "type": "tun",
        "tag": "in-tun",
        "interface_name": Engine._tun_interface_name(),
        # the v6 address is the leak fix: auto_route seizes the v6 default
        # route so IPv6 is captured (then rejected) instead of bypassing
        "address": ["172.19.0.1/30", "fdfe:dcba:9876::1/126"],
        "mtu": 1400,
        "auto_route": True,
        "strict_route": True,
        "stack": "system",
    }
    if _sys.platform == "linux":  # upstream-recommended, Linux-only field
        expected["auto_redirect"] = True
    assert tun == expected
    assert config["dns"] == {
        "servers": [
            # no detour: dialed directly — never via a channel, never a LAN
            # resolver (an explicit direct detour is a sing-box runtime error)
            {"type": "udp", "tag": "dns-remote", "server": "1.1.1.1"}
        ],
        "strategy": "ipv4_only",
    }
    assert config["route"]["default_domain_resolver"] == "dns-remote"
    assert config["route"]["auto_detect_interface"] is True


def test_tun_off_leaves_the_explicit_proxy_config_untouched():
    config, _ = Engine(_store(router=_router()))._build_config()
    assert all(i["type"] != "tun" for i in config["inbounds"])
    assert "dns" not in config
    assert "default_domain_resolver" not in config["route"]
    assert "auto_detect_interface" not in config["route"]


def test_tun_joins_the_same_rule_table_without_duplicating_it():
    router = _router(
        ("domain_suffix", "netflix.com", "nordvpn/us_1"),
        killswitch=True,
    )
    router["tun"] = True
    store = _store(("nordvpn", "us_1", 8888, "US", "", dict(WG)), router=router)
    config, errors = Engine(store)._build_config()
    assert errors == {}
    rules = config["route"]["rules"]
    both = ["in-router", "in-tun"]
    # the per-channel exact rule stays pinned to its own inbound (never demoted)
    assert rules[0] == {"inbound": ["in-nordvpn-us_1"], "outbound": "out-nordvpn-us_1"}
    assert rules[1] == {"inbound": both, "action": "sniff"}
    # the port half of LAN-direct precedes the DNS hijack: unicast mDNS is
    # wire-format DNS, so a later position would let the hijack swallow it
    # into a resolver that cannot answer .local
    assert rules[2] == {
        "inbound": both,
        "network": ["udp"],
        "port": list(routes.LAN_DIRECT_UDP_PORTS),
        "outbound": "direct",
    }
    # DNS hijack is tun-only and precedes the CIDR LAN-direct block, so a
    # port-53 query to a LAN resolver is answered by alle, not leaked
    assert rules[3] == {
        "inbound": ["in-tun"],
        "protocol": "dns",
        "action": "hijack-dns",
    }
    assert rules[4] == {
        "inbound": both,
        "ip_cidr": list(routes.LAN_DIRECT_CIDRS),
        "outbound": "direct",
    }
    # IPv6 leak fix: captured v6 is rejected (IPv4-only providers can't carry
    # it) — after LAN-direct so local v6 stays reachable, before user rules so
    # a catch-all can't steer v6 into an IPv4-only channel
    assert rules[5] == {
        "inbound": ["in-tun"],
        "ip_cidr": ["::/0"],
        "action": "reject",
    }
    assert rules[6] == {
        "inbound": both,
        "domain_suffix": ["netflix.com"],
        "outbound": "out-nordvpn-us_1",
    }
    assert rules[7] == {"inbound": both, "action": "reject"}  # system-wide killswitch
    assert len(rules) == 8  # one shared table — no second rule set


def test_tun_without_router_port_still_gets_the_rule_table():
    router = _router(("domain_suffix", "a.com", "direct"), port=0)
    router["tun"] = True
    config, errors = Engine(_store(router=router))._build_config()
    assert errors == {}
    assert [i["tag"] for i in config["inbounds"]] == ["in-tun"]
    rules = config["route"]["rules"]
    assert {"inbound": ["in-tun"], "action": "sniff"} in rules
    assert {
        "inbound": ["in-tun"],
        "domain_suffix": ["a.com"],
        "outbound": "direct",
    } in rules


def test_tun_interface_name_is_platform_aware(monkeypatch):
    import alle.engine as engine_mod

    monkeypatch.setattr(engine_mod.sys, "platform", "darwin")
    assert Engine._tun_interface_name() == "utun225"  # Darwin only accepts utunN
    monkeypatch.setattr(engine_mod.sys, "platform", "linux")
    assert Engine._tun_interface_name() == "alle-tun"


def test_tun_auto_redirect_is_linux_only(monkeypatch):
    import alle.engine as engine_mod

    engine = Engine(_store())
    monkeypatch.setattr(engine_mod.sys, "platform", "linux")
    assert engine._tun_inbound()["auto_redirect"] is True
    monkeypatch.setattr(engine_mod.sys, "platform", "darwin")
    assert "auto_redirect" not in engine._tun_inbound()


def test_unallocated_router_port_means_no_router_inbound():
    store = _store(
        ("nordvpn", "us_1", 8888, "US", "", dict(WG)), router=_router(port=0)
    )
    config, errors = Engine(store)._build_config()
    assert errors == {}
    assert [i["tag"] for i in config["inbounds"]] == ["in-nordvpn-us_1"]


def test_dangling_rule_reference_fails_closed_and_is_reported():
    store = _store(
        ("nordvpn", "us_1", 8888, "US", "", dict(WG)),
        router=_router(
            ("domain_suffix", "a.com", "nordvpn/gone_1"),  # no such channel
            ("domain_suffix", "b.com", "nordvpn/us_1"),
        ),
    )
    config, errors = Engine(store)._build_config()
    assert "rule r1" in errors and "nordvpn/gone_1" in errors["rule r1"]
    # the dangling rule blocks its traffic instead of leaking it via final:direct
    assert {
        "inbound": ["in-router"],
        "domain_suffix": ["a.com"],
        "action": "reject",
    } in config["route"]["rules"]
    assert {
        "inbound": ["in-router"],
        "domain_suffix": ["b.com"],
        "outbound": "out-nordvpn-us_1",
    } in config["route"]["rules"]  # healthy rule survives untouched


def test_rule_targeting_missing_provider_fails_closed():
    store = _store(
        ("nordvpn", "us_1", 8888, "US", "", dict(WG)),
        router=_router(("domain_suffix", "a.com", "protonvpn/nl_1")),  # provider gone
    )
    config, errors = Engine(store)._build_config()
    assert "rule r1" in errors and "protonvpn/nl_1" in errors["rule r1"]
    assert {
        "inbound": ["in-router"],
        "domain_suffix": ["a.com"],
        "action": "reject",
    } in config["route"]["rules"]


def test_rule_targeting_unbuildable_channel_fails_closed():
    store = _store(
        ("nordvpn", "us_1", 8888, "US", "", {}),  # malformed WireGuard data
        router=_router(("domain_suffix", "a.com", "nordvpn/us_1")),
    )
    config, errors = Engine(store)._build_config()
    assert "nordvpn/us_1" in errors and "rule r1" in errors
    # no dangling outbound reference or half-built channel in the config …
    assert config["endpoints"] == []
    # … and the rule's traffic is blocked rather than routed direct
    assert {
        "inbound": ["in-router"],
        "domain_suffix": ["a.com"],
        "action": "reject",
    } in config["route"]["rules"]


def test_catchall_ruleset_with_invalid_target_blocks_everything():
    # a ruleset whose only target is invalid: its catch-all row must not let
    # the whole router entrypoint fall through to direct
    store = _store(router=_router(("all", "", "nordvpn/gone_1")))
    config, errors = Engine(store)._build_config()
    assert "rule r1" in errors
    assert {"inbound": ["in-router"], "action": "reject"} in config["route"]["rules"]


def test_malformed_channel_omitted_and_reported():
    store = _store(
        ("nordvpn", "us_1", 8888, "US", "", {})
    )  # no usable WireGuard config
    config, errors = Engine(store)._build_config()
    assert config["inbounds"] == []
    assert "nordvpn/us_1" in errors and "no usable" in errors["nordvpn/us_1"]


class _FakeRunner:
    def __init__(self):
        self.applied = []
        self._running = False

    def apply(self, config):
        self.applied.append(config)
        self._running = bool(config.get("inbounds"))
        return singbox.ApplyResult(singbox.ApplyOutcome.APPLIED)

    def is_running(self):
        return self._running


def test_reconcile_pushes_config():
    eng = Engine(_store(("nordvpn", "us_1", 8888, "US", "", dict(WG))))
    runner = _FakeRunner()
    eng.runner = cast(singbox.Runner, runner)
    assert eng.reconcile() == {}
    assert runner.applied
    assert [i["tag"] for i in runner.applied[0]["inbounds"]] == ["in-nordvpn-us_1"]


def test_reconcile_raises_on_a_rejected_generation():
    class _RejectRunner:
        def apply(self, config):
            return singbox.ApplyResult(
                singbox.ApplyOutcome.REJECTED, "unknown field frobnicate"
            )

    eng = Engine(Store.load())
    eng.runner = cast(singbox.Runner, _RejectRunner())
    with pytest.raises(singbox.ConfigRejectedError, match="frobnicate"):
        eng.reconcile()


def test_reconcile_raises_on_unrecoverable_runtime_failure():
    class _DyingRunner:
        def apply(self, config):
            return singbox.ApplyResult(
                singbox.ApplyOutcome.RUNTIME_FAILED, "exited immediately (code 1)"
            )

    eng = Engine(Store.load())
    eng.runner = cast(singbox.Runner, _DyingRunner())
    # no address-in-use ports to recover — the failure propagates for the
    # daemon's timer retry
    with pytest.raises(singbox.SingBoxRuntimeError, match="exited immediately"):
        eng.reconcile()


class _PortStealRunner:
    """Fails the first apply with sing-box's address-in-use error, then works."""

    def __init__(self, stolen_port):
        self.stolen_port = stolen_port
        self.applied = []

    def apply(self, config):
        self.applied.append(config)
        if len(self.applied) == 1:
            return singbox.ApplyResult(
                singbox.ApplyOutcome.RUNTIME_FAILED,
                "sing-box exited immediately (code 1); last log lines:\n"
                "FATAL[0000] start service: start inbound/mixed[in-x]: listen tcp "
                f"127.0.0.1:{self.stolen_port}: bind: address already in use",
            )
        return singbox.ApplyResult(singbox.ApplyOutcome.APPLIED)

    def is_running(self):
        return True


def test_reconcile_reallocates_a_stolen_channel_port():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    eng = Engine(Store.load())
    runner = _PortStealRunner(ch.port)
    eng.runner = cast(singbox.Runner, runner)

    assert eng.reconcile() == {}
    assert len(runner.applied) == 2  # failed once, retried after reallocation
    moved = Store.load().get_channel("nordvpn", ch.id)
    assert moved is not None and moved.port != ch.port
    assert runner.applied[1]["inbounds"][0]["listen_port"] == moved.port


def test_reconcile_regenerates_a_stolen_clash_api_port():
    before = singbox.clash_api()
    stolen = int(before["address"].rsplit(":", 1)[1])
    eng = Engine(Store.load())
    runner = _PortStealRunner(stolen)
    eng.runner = cast(singbox.Runner, runner)

    eng.reconcile()
    after = singbox.clash_api()
    assert after["secret"] != before["secret"]  # endpoint was regenerated
    assert runner.applied[1]["experimental"]["clash_api"]["secret"] == after["secret"]


def test_reconcile_propagates_non_port_failures():
    class _BrokenRunner:
        def apply(self, config):
            raise singbox.SingBoxError("could not download sing-box: offline")

    eng = Engine(Store.load())
    eng.runner = cast(singbox.Runner, _BrokenRunner())
    with pytest.raises(singbox.SingBoxError, match="offline"):
        eng.reconcile()


def test_probe_all_logs_channel_details(monkeypatch):
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    eng = Engine(store)
    runner = _FakeRunner()
    runner._running = True
    eng.runner = cast(singbox.Runner, runner)
    monkeypatch.setattr(
        "alle.engine.probe.probe_channel",
        lambda port: {
            "ok": True,
            "at": 1,
            "latency_ms": 12.3,
            "ip": "1.2.3.4",
            "error": None,
        },
    )
    eng.probe_all()
    log = applog.tail()
    assert f"nordvpn/{ch.id} ok 12.3ms ip=1.2.3.4" in log
    assert "1 healthy" in log


def test_probe_all_records_stopped_when_not_running():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    eng = Engine(store)
    eng.runner = cast(singbox.Runner, _FakeRunner())  # not running
    out = eng.probe_all()
    assert out[f"nordvpn/{ch.id}"]["error"] == "stopped"
    # persisted to state
    persisted = Store.load().get_channel("nordvpn", ch.id)
    assert persisted is not None
    assert persisted.probe["error"] == "stopped"


def test_probe_all_runs_channels_concurrently(monkeypatch):
    """Probes run on a capped pool, so the wall-clock cost of N channels is
    the slowest single channel, not the sum — and a stuck channel can't stall
    the whole pass past PROBE_PASS_DEADLINE."""
    import threading

    store = Store.load()
    store.add_provider("nordvpn")
    for name in ("us", "uk", "jp"):
        store.add_channel("nordvpn", name, "", dict(WG))

    eng = Engine(store)
    runner = _FakeRunner()
    runner._running = True
    eng.runner = cast(singbox.Runner, runner)

    active = {"n": 0, "peak": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)

    def slow_probe(port):
        with lock:
            active["n"] += 1
            active["peak"] = max(active["peak"], active["n"])
        barrier.wait()  # all three must reach here concurrently to pass
        return {"ok": True, "at": 1, "latency_ms": 5.0, "ip": "1.2.3.4", "error": None}

    monkeypatch.setattr("alle.engine.probe.probe_channel", slow_probe)
    eng.probe_all()
    assert active["peak"] == 3  # all three probed in parallel, not serially
    # every channel got a result persisted
    for ch in Store.load().provider_channels("nordvpn"):
        assert ch.probe.get("ok") is True


def test_probe_result_is_discarded_after_channel_identity_changes(monkeypatch):
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    eng = Engine(store)
    runner = _FakeRunner()
    runner._running = True
    eng.runner = cast(singbox.Runner, runner)

    def probe_then_disable(_port):
        Store.load().set_channels_enabled([("nordvpn", ch.id)], False)
        return {"ok": False, "at": 1, "error": "old tunnel failed"}

    monkeypatch.setattr("alle.engine.probe.probe_channel", probe_then_disable)
    eng.probe_all([ch])
    current = Store.load().get_channel("nordvpn", ch.id)
    assert current is not None and current.enabled is False
    assert current.probe == {}
