"""The rule-match tracer: input parsing, the walk, verdicts, and engine drift.

Pure logic — DNS is stubbed, geo data comes from encoder-built .srs files
(see test_srs), and nothing spawns sing-box.
"""

from __future__ import annotations


import pytest

from alle import geodata, routes, service, tracer
from alle.engine import Engine
from alle.state import Store
from conftest import wg_config
from test_srs import _default_rule, _srs_file

WG = wg_config("1.2.3.4")


def _v6_wg(host="9.9.9.9"):
    wg = wg_config(host)
    wg["address"] = ["10.2.0.2/32", "2a07:b944::2:2/128"]
    return wg


def _store(*specs, router=None):
    """specs: (provider, id, port, wg) tuples (optionally + enabled)."""
    data = {"version": 1, "providers": {}}
    if router is not None:
        data["router"] = router
    for spec in specs:
        provider, cid, port, wg = spec[:4]
        prov = data["providers"].setdefault(provider, {"channels": {}})
        prov["channels"][cid] = {
            "country": "",
            "city": "",
            "port": port,
            "wg": wg,
            "probe": {},
            "enabled": spec[4] if len(spec) > 4 else True,
        }
    return Store(data=data)


def _router(*rules, port=40000, killswitch=False, lan_direct=True, tun=False):
    numbered = [
        {
            "id": f"r{i + 1}",
            "type": t,
            "value": v,
            "target": target,
            "ruleset": f"rs{i + 1}",
            "ruleset_name": f"Set {i + 1}",
        }
        for i, (t, v, target) in enumerate(rules)
    ]
    return {
        "port": port,
        "killswitch": killswitch,
        "lan_direct": lan_direct,
        "tun": tun,
        "rules": numbered,
    }


@pytest.fixture
def no_dns(monkeypatch):
    """Trace domains without touching the network."""

    def fake(answers_by_domain):
        def _resolve(domain):
            a, aaaa = answers_by_domain.get(domain, ([], []))
            return {"a": list(a), "aaaa": list(aaaa), "error": None}

        monkeypatch.setattr(tracer, "_resolve", _resolve)

    return fake


# ---- input parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "domain", "ip"),
    [
        ("netflix.com", "netflix.com", None),
        ("NETFLIX.COM.", "netflix.com", None),
        ("https://user@Example.org:8443/path?q=1", "example.org", None),
        ("example.org:443", "example.org", None),
        # scheme-less host with a path — the host is extracted, path discarded
        ("netflix.com/some/page.html", "netflix.com", None),
        ("a.b.example.org/x/y", "a.b.example.org", None),
        ("example.org:8080/path", "example.org", None),
        ("8.8.8.8", None, "8.8.8.8"),
        ("192.168.1.1/admin/login.php", None, "192.168.1.1"),
        ("2001:db8::1", None, "2001:db8::1"),
        ("[2001:db8::1]/path", None, "2001:db8::1"),
        ("[2001:db8::1]:443", None, "2001:db8::1"),
    ],
)
def test_parse_destination_forms(value, domain, ip):
    assert tracer.parse_destination(value) == (domain, ip)


@pytest.mark.parametrize("value", ["", "   ", "not a domain", "http://", "singlelabel"])
def test_parse_destination_rejects_unusable(value):
    with pytest.raises(tracer.TraceError):
        tracer.parse_destination(value)


# ---- the walk: order and verdicts --------------------------------------------


def test_geoip_match_names_the_resolved_domain_inline(no_dns, geo_file):
    # a geoip verdict is decided by the resolved IP — the reason names the
    # domain that resolved to it inline; the joined reason carries that to the
    # CLI and REST API verbatim
    geo_file("geoip", "us", _default_rule(cidrs=["172.66.0.0/16"]))
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(("geoip", "us", "nordvpn/us_1")),
    )
    no_dns({"x.com": (["172.66.0.227"], [])})
    r = tracer.trace(store, "x.com")
    assert r["verdict"] == "channel"
    assert "(x.com resolved to)" in r["reason"]
    assert any("(x.com resolved to)" in p["text"] for p in r["reason_parts"])
    # a literal-IP input did no DNS — no parenthetical (nothing resolved)
    r2 = tracer.trace(store, "172.66.0.227")
    assert r2["verdict"] == "channel"
    assert "resolved to" not in r2["reason"]
    # a non-geoip verdict never mentions resolution (the match is the domain)
    store2 = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(("domain_suffix", "example.com", "nordvpn/us_1")),
    )
    no_dns({"www.example.com": (["172.66.0.227"], [])})
    r3 = tracer.trace(store2, "www.example.com")
    assert r3["verdict"] == "channel"
    assert "resolved to" not in r3["reason"]


def test_reason_segments_bold_matcher_and_target(no_dns, geo_file):
    # the matcher token and the channel target are the emphasized (bold) parts;
    # the joined plain text still reads as a sentence for the CLI/JSON form
    geo_file("geosite", "netflix", _default_rule(suffix=["netflix.com"]))
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(
            ("geosite", "netflix", "nordvpn/us_1"),
            ("ip_cidr", "198.51.100.0/24", "nordvpn/us_1"),
            ("domain_suffix", "example.com", "direct"),
            ("all", "", "nordvpn/us_1"),
        ),
    )
    no_dns({"www.netflix.com": (["44.242.60.85"], [])})
    r = tracer.trace(store, "www.netflix.com")
    parts = r["reason_parts"]
    bold = [p["text"] for p in parts if p.get("bold")]
    assert bold == ["geosite:netflix", "nordvpn/us_1"]
    assert r["reason"] == "".join(p["text"] for p in parts)
    # an ip_cidr token is the CIDR itself; the all token is "all traffic"
    no_dns({"ip.example": (["198.51.100.9"], [])})
    r2 = tracer.trace(store, "ip.example")
    assert [p["text"] for p in r2["reason_parts"] if p.get("bold")] == [
        "198.51.100.0/24",
        "nordvpn/us_1",
    ]
    no_dns({"example.com": ([], [])})
    r3 = tracer.trace(store, "example.com")
    assert [p["text"] for p in r3["reason_parts"] if p.get("bold")] == ["example.com"]
    no_dns({"other.example": ([], [])})
    r4 = tracer.trace(store, "other.example")
    assert [p["text"] for p in r4["reason_parts"] if p.get("bold")] == [
        "all traffic",
        "nordvpn/us_1",
    ]


def test_first_match_wins_and_walk_shows_misses(no_dns):
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(
            ("domain_suffix", "api.example.com", "direct"),
            ("domain_suffix", "example.com", "nordvpn/us_1"),
            ("all", "", "block"),
        ),
    )
    no_dns({"www.example.com": (["93.184.216.34"], [])})
    result = tracer.trace(store, "www.example.com")
    assert result["verdict"] == "channel"
    assert result["exit"] == "nordvpn/us_1"
    assert result["matched_rule"]["id"] == "r2"
    walk = {w["rule"]: w for w in result["walked"]}
    assert walk["r1 api.example.com"]["matched"] is False
    assert walk["r2 example.com"]["matched"] is True
    # evaluation stopped at the winner — r3 was never reached
    assert not any(w["rule"].startswith("r3") for w in result["walked"])


def test_direct_and_block_verdicts(no_dns):
    store = _store(
        router=_router(
            ("domain_suffix", "bank.example", "direct"),
            ("domain_suffix", "ads.example", "block"),
        )
    )
    no_dns({})
    assert tracer.trace(store, "x.bank.example")["verdict"] == "direct"
    blocked = tracer.trace(store, "ads.example")
    assert blocked["verdict"] == "block"
    assert blocked["exit"] is None


def test_lan_direct_wins_before_user_rules():
    store = _store(router=_router(("all", "", "block")))
    result = tracer.trace(store, "192.168.1.10")
    assert result["verdict"] == "lan_direct"
    assert result["exit"] == "direct"
    assert result["matched_rule"] == {"builtin": routes.LAN_DIRECT_SHADOW}


def test_lan_direct_off_falls_through_to_rules():
    store = _store(router=_router(("all", "", "block"), lan_direct=False))
    result = tracer.trace(store, "192.168.1.10")
    assert result["verdict"] == "block"
    assert not any("LAN-direct" in w["rule"] for w in result["walked"])


def test_killswitch_verdict(no_dns):
    store = _store(router=_router(killswitch=True))
    no_dns({})
    result = tracer.trace(store, "unmatched.example")
    assert result["verdict"] == "killswitch"
    assert result["exit"] is None


def test_fall_through_direct(no_dns):
    store = _store(router=_router())
    no_dns({})
    result = tracer.trace(store, "unmatched.example")
    assert result["verdict"] == "direct"
    assert result["exit"] == "direct"
    assert result["matched_rule"] is None
    assert "kill-switch is off" in result["reason"]


def test_unusable_channel_target_blocks_fail_closed(no_dns):
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG), False),  # disabled
        router=_router(("domain_suffix", "example.com", "nordvpn/us_1")),
    )
    no_dns({})
    result = tracer.trace(store, "example.com")
    assert result["verdict"] == "block"
    assert "fail closed" in result["reason"]


# ---- geo rules ---------------------------------------------------------------


@pytest.fixture
def geo_file(tmp_path, monkeypatch):
    """Point one cached geo category at an encoder-built .srs file."""

    def install(kind, name, rule_bytes):
        path = _srs_file(tmp_path, rule_bytes)
        real = geodata.cached_path

        def fake(store, k, n):
            if (k, n) == (kind, name):
                return path
            return real(store, k, n)

        monkeypatch.setattr(geodata, "cached_path", fake)
        return path

    return install


def test_geosite_rule_matches_via_cached_srs(no_dns, geo_file):
    geo_file("geosite", "netflix", _default_rule(suffix=["netflix.com"]))
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(("geosite", "netflix", "nordvpn/us_1")),
    )
    no_dns({"www.netflix.com": (["44.242.60.85"], [])})
    result = tracer.trace(store, "www.netflix.com")
    assert result["verdict"] == "channel"
    assert result["matched_rule"]["id"] == "r1"
    assert result["geo_problems"] == {}
    no_dns({"example.org": (["93.184.216.34"], [])})
    assert tracer.trace(store, "example.org")["verdict"] == "direct"


def test_geoip_rule_matches_resolved_ip(no_dns, geo_file):
    geo_file("geoip", "jp", _default_rule(cidrs=["203.0.113.0/24"]))
    store = _store(router=_router(("geoip", "jp", "direct")))
    no_dns({"jp.example": (["203.0.113.9"], [])})
    result = tracer.trace(store, "jp.example")
    assert result["verdict"] == "direct"
    assert "203.0.113.9" in result["reason"]


def test_uncached_geo_category_blocks_all_and_is_disclosed(no_dns):
    store = _store(router=_router(("geosite", "netflix", "direct")))
    no_dns({"anything.example": (["93.184.216.34"], [])})
    result = tracer.trace(store, "anything.example")
    # the engine compiles a matcher-less reject: everything is blocked, not
    # just the category's traffic — the trace must say so honestly
    assert result["verdict"] == "block"
    assert "all routed traffic is blocked" in result["reason"]
    assert result["geo_problems"] == {
        "geosite:netflix": "not cached (or failed its digest check)"
    }


# ---- IPv6 handling -----------------------------------------------------------


def test_v6_literal_rejected_under_tun_without_v6_channels():
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(("all", "", "nordvpn/us_1"), tun=True),
    )
    result = tracer.trace(store, "2001:db8::1")
    assert result["verdict"] == "reject_v6"
    assert "no enabled channel carries IPv6" in result["reason"]


def test_v6_literal_follows_rules_without_tun():
    # explicit-proxy mode compiles no v6 rules at all
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(("all", "", "nordvpn/us_1"), tun=False),
    )
    result = tracer.trace(store, "2001:db8::1")
    assert result["verdict"] == "channel"


def test_v6_guard_rejects_v6_flow_into_v4_only_channel(no_dns):
    store = _store(
        ("protonvpn", "wg_jp_351", 9100, _v6_wg()),
        ("nordvpn", "us_1", 9200, dict(WG)),
        router=_router(("all", "", "nordvpn/us_1"), tun=True),
    )
    no_dns({"v6only.example": ([], ["2001:db8::5"])})
    result = tracer.trace(store, "v6only.example")
    assert result["verdict"] == "reject_v6"
    assert "IPv4-only" in result["reason"]
    assert any(w["rule"].startswith("built-in: IPv6 guard") for w in result["walked"])


def test_v6_flow_rides_a_v6_capable_channel(no_dns):
    store = _store(
        ("protonvpn", "wg_jp_351", 9100, _v6_wg()),
        router=_router(("all", "", "protonvpn/wg_jp_351"), tun=True),
    )
    no_dns({"v6only.example": ([], ["2001:db8::5"])})
    result = tracer.trace(store, "v6only.example")
    assert result["verdict"] == "channel"
    assert result["exit"] == "protonvpn/wg_jp_351"


def test_unmatched_v6_hits_trailing_catchall(no_dns):
    store = _store(
        ("protonvpn", "wg_jp_351", 9100, _v6_wg()),
        router=_router(tun=True),
    )
    no_dns({"v6only.example": ([], ["2001:db8::5"])})
    result = tracer.trace(store, "v6only.example")
    assert result["verdict"] == "reject_v6"
    assert "matched no rule" in result["reason"]


def test_aaaa_suppressed_forces_v4_flow(no_dns):
    # dual-stack domain, tun on, v4-only fleet: alle's DNS answers ipv4_only,
    # so the flow is v4 and the AAAA suppression is disclosed
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(("all", "", "nordvpn/us_1"), tun=True),
    )
    no_dns({"dual.example": (["93.184.216.34"], ["2001:db8::6"])})
    result = tracer.trace(store, "dual.example")
    assert result["verdict"] == "channel"
    assert result["flow_family"] == 4
    assert result["dns"]["aaaa_suppressed"] is True
    assert "2001:db8::6" in result["resolved_ips"]  # still disclosed


def test_dns_failure_still_evaluates_domain_rules(monkeypatch):
    store = _store(router=_router(("domain_suffix", "example.com", "direct")))
    monkeypatch.setattr(
        tracer,
        "_resolve",
        lambda domain: {"a": [], "aaaa": [], "error": "timed out"},
    )
    result = tracer.trace(store, "www.example.com")
    assert result["verdict"] == "direct"
    assert result["dns"]["error"] == "timed out"
    assert result["resolved_ips"] == []


# ---- drift guard against the engine's compiled table -------------------------


def _compiled_kinds(config) -> list[str]:
    """Classify the shared-table entries of a compiled config, in order."""
    kinds = []
    for rule in config["route"]["rules"]:
        if set(rule) == {"inbound", "outbound"} and rule["inbound"][0].startswith(
            "in-"
        ):
            continue  # per-channel pin, not part of the shared table
        if rule.get("action") == "sniff":
            kinds.append("sniff")
        elif rule.get("port"):
            kinds.append("lan_ports")
        elif rule.get("protocol") == "dns":
            kinds.append("dns_hijack")
        elif rule.get("ip_cidr") == list(routes.LAN_DIRECT_CIDRS):
            kinds.append("lan_cidrs")
        elif rule.get("ip_cidr") == ["::/0"]:
            kinds.append("v6_reject")
        elif rule.get("ip_version") == 6:
            kinds.append("v6_guard")
        elif set(rule) == {"inbound", "action"} and rule["action"] == "reject":
            kinds.append("killswitch")
        else:
            kinds.append("user")
    return kinds


def _walk_kinds(result) -> list[str]:
    kinds = []
    for w in result["walked"]:
        label = w["rule"]
        if label == "built-in: sniff":
            kinds.append("sniff")
        elif label.startswith("built-in: LAN-direct UDP"):
            kinds.append("lan_ports")
        elif label.startswith("built-in: DNS hijack"):
            kinds.append("dns_hijack")
        elif label.startswith("built-in: LAN-direct CIDRs"):
            kinds.append("lan_cidrs")
        elif label.startswith("built-in: IPv6 reject") or label.startswith(
            "built-in: unmatched IPv6"
        ):
            kinds.append("v6_reject")
        elif label.startswith("built-in: IPv6 guard"):
            kinds.append("v6_guard")
        elif label.startswith("built-in: kill-switch"):
            kinds.append("killswitch")
        else:
            kinds.append("user")
    return kinds


@pytest.mark.parametrize("tun", [False, True])
@pytest.mark.parametrize("killswitch", [False, True])
def test_walk_mirrors_compiled_table_order(no_dns, tun, killswitch):
    """The tracer's walk must visit the same logical table the engine
    compiles, in the same order — this is the drift guard between the two."""
    store = _store(
        ("nordvpn", "us_1", 9000, dict(WG)),
        router=_router(
            ("domain_suffix", "a.example", "nordvpn/us_1"),
            ("ip_cidr", "198.51.100.0/24", "direct"),
            ("domain_suffix", "b.example", "block"),
            tun=tun,
            killswitch=killswitch,
        ),
    )
    config, errors = Engine(store)._build_config()
    assert errors == {}
    no_dns({})  # no answers: nothing matches, the walk visits every entry
    result = tracer.trace(store, "unmatched.example")
    walk = _walk_kinds(result)
    if killswitch:
        assert result["verdict"] == "killswitch"
    else:
        # route.final: direct is not a rule row — append it conceptually
        assert result["verdict"] == "direct"
    assert walk == _compiled_kinds(config)


# ---- DNS wire format ---------------------------------------------------------


def test_dns_answer_parsing_with_compression_and_cname():
    query = tracer._dns_query("example.com", 1)
    answer = (
        query[:2]
        + b"\x81\x80\x00\x01\x00\x02\x00\x00\x00\x00"
        + query[12:]  # question section echoed back
        # CNAME record (type 5) — skipped
        + b"\xc0\x0c\x00\x05\x00\x01\x00\x00\x00\x3c\x00\x02\xc0\x0c"
        # A record via compression pointer
        + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22"
    )
    assert tracer._dns_answers(answer, query, 1) == ["93.184.216.34"]


def test_dns_answer_rejects_mismatched_id_and_rcode():
    query = tracer._dns_query("example.com", 1)
    with pytest.raises(ValueError, match="mismatched"):
        tracer._dns_answers(b"\x00\x00" + query[2:], query, 1)
    nx = bytearray(query)
    nx[2:4] = b"\x81\x83"  # NXDOMAIN
    with pytest.raises(ValueError, match="rcode"):
        tracer._dns_answers(bytes(nx), query, 1)


# ---- the service seam --------------------------------------------------------


def test_service_wraps_trace_errors():
    with pytest.raises(service.ServiceError, match="not a valid domain"):
        service.routes_trace("not a domain")


def test_service_returns_trace_result(no_dns, monkeypatch):
    monkeypatch.setattr(
        tracer,
        "_resolve",
        lambda domain: {"a": [], "aaaa": [], "error": None},
    )
    result = service.routes_trace("example.com")
    assert result["input"] == "example.com"
    assert result["verdict"] in {
        "channel",
        "direct",
        "block",
        "lan_direct",
        "reject_v6",
        "killswitch",
    }
