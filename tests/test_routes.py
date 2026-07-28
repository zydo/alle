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


# ---- the indexed lint agrees with the pairwise definition ---------------------
#
# `shadowed_by` looks its answers up instead of searching for them. `covers` is
# still the definition of the relation, so the reference below — the obvious
# quadratic reading of it — is what the index has to reproduce, rule for rule.


def reference_shadowed_by(rules: list[dict]) -> dict[str, str]:
    """The pairwise definition, kept in the tests as the thing to agree with."""
    out: dict[str, str] = {}
    for i, rule in enumerate(rules):
        for earlier in rules[:i]:
            if routes.covers(earlier, rule):
                out[rule["id"]] = earlier["id"]
                break
    return out


def assert_agrees(rules: list[dict]) -> dict[str, str]:
    """Both implementations must return the same mapping — the *identity* of the
    covering rule, not merely which rules ended up shadowed."""
    optimized = routes.shadowed_by(rules)
    assert optimized == reference_shadowed_by(rules)
    return optimized


# A value pool per matcher type, chosen so random tables actually produce
# coverage: nested domains, supernets/subnets in both families, repeated geo
# categories, and matcher types the lint cannot reason about.
_VALUE_POOL: dict[str, tuple[str, ...]] = {
    "all": ("",),
    "domain_suffix": (
        "com",
        "google.com",
        "api.google.com",
        "deep.api.google.com",
        "agoogle.com",
        "example.co.uk",
        "co.uk",
        "otherexample.co.uk",
    ),
    "ip_cidr": (
        "0.0.0.0/0",
        "10.0.0.0/8",
        "10.1.0.0/16",
        "10.1.2.0/24",
        "10.1.2.3/32",
        "192.168.0.0/16",
        "::/0",
        "2001:db8::/32",
        "2001:db8:1::/48",
        "2001:db8:1::1/128",
    ),
    "geosite": ("netflix", "category-ads-all"),
    "geoip": ("us", "cn"),
    # Not a matcher alle knows: it must stay undecidable, covered only by `all`
    # and covering nothing — including another rule of the same unknown type.
    "port": ("443", "443"),
}


# `all` is deliberately rare. Weighted evenly it lands early in most tables and
# shadows the whole tail, which agrees trivially and leaves the domain and CIDR
# indexes — the parts with real containment logic — barely exercised.
_TYPE_WEIGHTS: dict[str, int] = {
    "domain_suffix": 12,
    "ip_cidr": 12,
    "geosite": 4,
    "geoip": 4,
    "port": 3,
    "all": 1,
}


# Half the values are drawn fresh rather than from the pool. Pool-only tables
# get almost everything shadowed at size 200, which stops testing the direction
# that matters just as much: rules the lint must leave alone.
_FRESH: dict[str, str] = {
    "domain_suffix": "host{i}.example{i}.test",
    "ip_cidr": "10.{a}.{b}.0/24",
    "geosite": "category-{i}",
    "geoip": "cc{i}",
    "port": "{i}",
    "all": "",
}


def _value(rng, mtype: str, i: int) -> str:
    if mtype != "all" and rng.random() < 0.5:
        return _FRESH[mtype].format(i=i, a=i // 256, b=i % 256)
    return rng.choice(_VALUE_POOL[mtype])


def _random_table(rng, size: int, *, duplicate_ids: bool = False) -> list[dict]:
    types = list(_TYPE_WEIGHTS)
    weights = list(_TYPE_WEIGHTS.values())
    rules = []
    for i in range(size):
        mtype = rng.choices(types, weights=weights)[0]
        rules.append(
            {
                "id": f"r{rng.randint(1, 3)}" if duplicate_ids else f"r{i + 1}",
                "type": mtype,
                "value": _value(rng, mtype, i),
                "target": "direct",
            }
        )
    return rules


@pytest.mark.parametrize("size", [1, 2, 7, 40, 200])
def test_indexed_lint_agrees_with_the_pairwise_definition(size):
    import random

    for seed in range(25):
        rng = random.Random((size << 8) + seed)
        assert_agrees(_random_table(rng, size))


def test_indexed_lint_agrees_when_rule_ids_repeat():
    """Schema-valid tables can repeat ids (a ruleset's rules share one id
    space); the last write must win in both implementations alike."""
    import random

    for seed in range(25):
        rng = random.Random(seed)
        assert_agrees(_random_table(rng, 30, duplicate_ids=True))


def test_a_leading_catch_all_shadows_the_entire_tail():
    rules = [_rule("r0", "all", "")] + [
        _rule(f"r{i}", "domain_suffix", f"host{i}.example.com") for i in range(1, 20)
    ]
    shadows = assert_agrees(rules)
    assert set(shadows) == {f"r{i}" for i in range(1, 20)}
    assert set(shadows.values()) == {"r0"}


def test_nested_supernets_report_the_outermost_rule():
    rules = [
        _rule("r1", "ip_cidr", "10.0.0.0/8"),
        _rule("r2", "ip_cidr", "10.1.0.0/16"),
        _rule("r3", "ip_cidr", "10.1.2.0/24"),
        _rule("r4", "ip_cidr", "2001:db8::/32"),
        _rule("r5", "ip_cidr", "2001:db8:1::/48"),
    ]
    # /8 covers both v4 subnets — the earliest, not the tightest, is reported
    assert assert_agrees(rules) == {"r2": "r1", "r3": "r1", "r5": "r4"}


def test_a_zero_length_prefix_covers_its_whole_family_only():
    rules = [
        _rule("r1", "ip_cidr", "0.0.0.0/0"),
        _rule("r2", "ip_cidr", "10.0.0.0/8"),
        _rule("r3", "ip_cidr", "2001:db8::/32"),  # other family: untouched
    ]
    assert assert_agrees(rules) == {"r2": "r1"}


def test_repeated_geo_categories_shadow_only_their_own_kind():
    rules = [
        _rule("r1", "geosite", "netflix"),
        _rule("r2", "geoip", "netflix"),  # same value, different kind
        _rule("r3", "geosite", "netflix"),  # duplicate category — dead
        _rule("r4", "geosite", "category-ads-all"),
    ]
    assert assert_agrees(rules) == {"r3": "r1"}


def test_unknown_matcher_types_never_shadow_each_other():
    rules = [
        _rule("r1", "port", "443"),
        _rule("r2", "port", "443"),  # identical, but undecidable: not shadowed
        _rule("r3", "domain_suffix", "example.com"),
    ]
    assert assert_agrees(rules) == {}


def test_an_unparseable_cidr_is_left_undecided_rather_than_raising():
    """The one deliberate difference from `reference_shadowed_by`, which raises
    here (`covers` parses inside the comparison). A stored value the lint cannot
    parse now covers nothing and is covered by nothing, so a table containing
    one still renders instead of failing every route surface that lints it.

    Unreachable through the API — `normalize_value` rejects such a value — but
    state.json is a file on disk.
    """
    rules = [
        _rule("r1", "ip_cidr", "not-a-network"),
        _rule("r2", "ip_cidr", "not-a-network"),
        _rule("r3", "ip_cidr", "10.0.0.0/8"),
        _rule("r4", "ip_cidr", "10.1.0.0/16"),
    ]
    assert routes.shadowed_by(rules) == {"r4": "r3"}


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
