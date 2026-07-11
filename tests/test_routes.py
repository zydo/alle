"""Routing rule model: matcher validation, target parsing, and the shadow lint."""

from __future__ import annotations

import pytest

from alle import routes


# ---- matcher validation --------------------------------------------------------


def test_domains_are_normalized():
    assert (
        routes.normalize_value("domain_suffix", " API.Google.COM. ") == "api.google.com"
    )
    assert routes.normalize_value("domain_suffix", "Netflix.com") == "netflix.com"


def test_wildcard_domain_is_redundant_and_rejected():
    with pytest.raises(routes.RuleError, match="always match subdomains"):
        routes.normalize_value("domain_suffix", "*.google.com")


@pytest.mark.parametrize(
    "bad", ["", "xxx", "localhost", "no spaces.com", "-x.com", "a..b", "a b"]
)
def test_invalid_domains_are_rejected(bad):
    with pytest.raises(routes.RuleError, match="not a valid domain"):
        routes.normalize_value("domain_suffix", bad)


def test_cidr_values_are_canonicalized():
    assert routes.normalize_value("ip_cidr", "10.0.0.1/8") == "10.0.0.0/8"
    assert routes.normalize_value("ip_cidr", "203.0.113.7") == "203.0.113.7/32"
    assert routes.normalize_value("ip_cidr", "2001:db8::1") == "2001:db8::1/128"


def test_invalid_cidr_is_rejected():
    with pytest.raises(routes.RuleError, match="not a valid IP or CIDR"):
        routes.normalize_value("ip_cidr", "999.1.2.3/8")


def test_match_all_needs_no_value():
    assert routes.normalize_value("all", "") == ""


# ---- inferred matchers default to suffix (Phase 5.6) --------------------------


def test_inferred_domains_default_to_suffix_regardless_of_label_count():
    # The old two-label heuristic made a registrable domain like example.co.uk
    # (3 labels) an exact match — so its subdomains bypassed the rule. Every
    # inferred domain is now a suffix match.
    assert routes.infer_matcher("netflix.com") == ("domain_suffix", "netflix.com")
    assert routes.infer_matcher("example.co.uk") == ("domain_suffix", "example.co.uk")
    assert routes.infer_matcher("api.openai.com") == ("domain_suffix", "api.openai.com")
    assert routes.infer_matcher("a.b.c.d.example.com") == (
        "domain_suffix",
        "a.b.c.d.example.com",
    )


def test_legacy_domain_type_is_read_as_suffix():
    # The legacy exact "domain" type (old bundles, old API clients) is an
    # alias — alle has one domain semantic, the domain and its subdomains.
    assert routes.infer_matcher("api.openai.com", "domain") == (
        "domain_suffix",
        "api.openai.com",
    )
    assert routes.infer_matcher("netflix.com", "domain_suffix") == (
        "domain_suffix",
        "netflix.com",
    )


def test_inferred_cidr_and_all_still_classified():
    assert routes.infer_matcher("10.8.0.0/16") == ("ip_cidr", "10.8.0.0/16")
    assert routes.infer_matcher("all") == ("all", "")


# ---- target parsing ------------------------------------------------------------


def test_targets_parse_to_kinds():
    assert routes.parse_target("direct") == ("direct", None)
    assert routes.parse_target("block") == ("block", None)
    assert routes.parse_target("nordvpn/us_1") == ("channel", ("nordvpn", "us_1"))


@pytest.mark.parametrize("bad", ["", "nordvpn", "/us_1", "a/b/c", "nordvpn/"])
def test_invalid_targets_are_rejected(bad):
    with pytest.raises(routes.RuleError, match="not valid"):
        routes.parse_target(bad)


# ---- shadow lint ---------------------------------------------------------------


def _rule(rid, mtype, value):
    return {"id": rid, "type": mtype, "value": value, "target": "direct"}


def test_suffix_shadows_deeper_suffix():
    rules = [
        _rule("r1", "domain_suffix", "google.com"),
        _rule("r2", "domain_suffix", "api.google.com"),  # covered — dead code
        _rule("r3", "domain_suffix", "maps.google.com"),  # covered
        _rule("r4", "domain_suffix", "agoogle.com"),  # dot boundary: NOT covered
    ]
    assert routes.shadowed_by(rules) == {"r2": "r1", "r3": "r1"}


def test_duplicate_suffix_is_shadowed():
    rules = [
        _rule("r1", "domain_suffix", "google.com"),
        _rule("r2", "domain_suffix", "google.com"),  # duplicate — dead
    ]
    assert routes.shadowed_by(rules) == {"r2": "r1"}


def test_cidr_supernet_shadows_subnet_only_within_family():
    rules = [
        _rule("r1", "ip_cidr", "10.0.0.0/8"),
        _rule("r2", "ip_cidr", "10.1.0.0/16"),  # subnet — dead
        _rule("r3", "ip_cidr", "192.168.0.0/16"),  # disjoint
        _rule("r4", "ip_cidr", "2001:db8::/32"),  # other family
    ]
    assert routes.shadowed_by(rules) == {"r2": "r1"}


def test_match_all_shadows_everything_after_it():
    rules = [
        _rule("r1", "domain_suffix", "a.com"),
        _rule("r2", "all", ""),
        _rule("r3", "domain_suffix", "b.com"),
        _rule("r4", "all", ""),
    ]
    assert routes.shadowed_by(rules) == {"r3": "r2", "r4": "r2"}


def test_cross_family_rules_never_shadow():
    rules = [
        _rule("r1", "domain_suffix", "google.com"),
        _rule("r2", "ip_cidr", "8.8.8.0/24"),
    ]
    assert routes.shadowed_by(rules) == {}


def test_earliest_covering_rule_is_reported():
    rules = [
        _rule("r1", "domain_suffix", "com"),
        _rule("r2", "domain_suffix", "google.com"),
        _rule("r3", "domain_suffix", "api.google.com"),
    ]
    assert routes.shadowed_by(rules) == {"r2": "r1", "r3": "r1"}


# ---- built-in LAN-direct shadowing (Phase 5.6) --------------------------------


def test_lan_direct_shadows_a_private_cidr_user_rule():
    # A user rule targeting a private range the built-in LAN-direct block
    # already sends direct can never match while lan_direct is on.
    assert routes.shadowed_by_lan_direct(_rule("r1", "ip_cidr", "10.8.0.0/16"))
    assert routes.shadowed_by_lan_direct(_rule("r2", "ip_cidr", "192.168.1.0/24"))
    assert routes.shadowed_by_lan_direct(_rule("r3", "ip_cidr", "172.16.5.0/28"))


def test_lan_direct_does_not_shadow_a_public_cidr_or_a_domain_rule():
    assert not routes.shadowed_by_lan_direct(_rule("r1", "ip_cidr", "8.8.8.0/24"))
    assert not routes.shadowed_by_lan_direct(
        _rule("r2", "domain_suffix", "netflix.com")
    )
    assert not routes.shadowed_by_lan_direct(_rule("r3", "domain_suffix", "10.0.0.1"))
    # a CIDR wider than the LAN range (not a subnet) is not shadowed
    assert not routes.shadowed_by_lan_direct(_rule("r4", "ip_cidr", "10.0.0.0/7"))


def test_lan_direct_shadow_marker_renders_with_a_human_label():
    assert (
        routes.shadow_label(routes.LAN_DIRECT_SHADOW) == "the built-in LAN-direct rule"
    )
    assert routes.shadow_label("r3") == "earlier rule r3"
