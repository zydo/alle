"""Routing rule model: matcher validation, target parsing, and the shadow lint."""

from __future__ import annotations

import pytest

from alle import routes


# ---- matcher validation --------------------------------------------------------


def test_domains_are_normalized():
    assert routes.normalize_value("domain", " API.Google.COM. ") == "api.google.com"
    assert routes.normalize_value("domain_suffix", "Netflix.com") == "netflix.com"


def test_wildcard_domain_is_redirected_to_suffix():
    with pytest.raises(routes.RuleError, match="--domain-suffix google.com"):
        routes.normalize_value("domain", "*.google.com")


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


def test_suffix_shadows_exact_and_deeper_suffix():
    rules = [
        _rule("r1", "domain_suffix", "google.com"),
        _rule("r2", "domain", "api.google.com"),  # covered — dead code
        _rule("r3", "domain_suffix", "maps.google.com"),  # covered
        _rule("r4", "domain", "agoogle.com"),  # dot boundary: NOT covered
    ]
    assert routes.shadowed_by(rules) == {"r2": "r1", "r3": "r1"}


def test_exact_does_not_shadow_the_suffix():
    rules = [
        _rule("r1", "domain", "google.com"),  # exact first
        _rule("r2", "domain_suffix", "google.com"),  # still matches subdomains
        _rule("r3", "domain", "google.com"),  # duplicate exact — dead
    ]
    assert routes.shadowed_by(rules) == {"r3": "r1"}


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
        _rule("r1", "domain", "a.com"),
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
        _rule("r3", "domain", "api.google.com"),
    ]
    assert routes.shadowed_by(rules) == {"r2": "r1", "r3": "r1"}
