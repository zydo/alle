"""The declarative setup bundle: export shape, all-or-nothing validation,
import (merge) / restore (replace) semantics, the CLI verbs, and the API
endpoints."""

from __future__ import annotations

import base64
import json
import stat
import urllib.error
import urllib.request
from threading import Thread

import pytest
import yaml

from alle import bundle, cli, credentials, service
from alle.providers import ProviderError
from alle.state import Store
from alle.webui import server

KEY_A = base64.b64encode(bytes([1] * 32)).decode()
KEY_B = base64.b64encode(bytes([2] * 32)).decode()
KEY_C = base64.b64encode(bytes([3] * 32)).decode()


@pytest.fixture(autouse=True)
def no_background(monkeypatch):
    monkeypatch.setattr(service.daemon, "ensure_running", lambda: None)


def wg(host="1.2.3.4", private_key=KEY_A, preshared=None):
    return {
        "private_key": private_key,
        "address": ["10.5.0.2/32"],
        "peer": {
            "public_key": KEY_B,
            "endpoint_host": host,
            "endpoint_port": 51820,
            "preshared_key": preshared,
            "allowed_ips": ["0.0.0.0/0", "::/0"],
            "keepalive": 25,
        },
    }


def _factory(host):
    """A fake ``provider_resolver`` whose channels land on ``host``."""

    def factory(provider, creds):
        return lambda country, city="": wg(host)

    return factory


def _factory_down(provider, creds):
    """A fake ``provider_resolver`` for an unreachable provider API."""
    raise ProviderError("api down")


def _factory_forbidden(provider, creds):
    raise AssertionError("must not touch the provider API")


def seed():
    """A setup with both archetypes, a credential, rulesets, and probe noise."""
    store = Store.load()
    store.add_provider("nordvpn")
    credentials.set_("nordvpn", {"token": "tok-123"})
    ch = store.add_channel("nordvpn", "United States", "", wg(), label="US East")
    store.set_probe("nordvpn", ch.id, {"ok": True, "latency_ms": 12})
    store.set_reconnect("nordvpn", ch.id, {"state": "watch"})
    store.add_provider("protonvpn")
    store.upsert_channel("protonvpn", "proton_us_1", "", "", wg("5.6.7.8", KEY_C))
    store.create_ruleset(
        "Streaming", f"nordvpn/{ch.id}", [("domain_suffix", "netflix.com")]
    )
    store.create_ruleset("Fallback", "direct", [("all", "")])
    store.set_killswitch(True)
    return Store.load(), ch


def channel(store: Store, provider: str, cid: str):
    """The channel, asserted present — keeps attribute access type-safe."""
    ch = store.get_channel(provider, cid)
    assert ch is not None
    return ch


# ---- export ---------------------------------------------------------------------


def test_export_is_setup_only_and_explicit(monkeypatch):
    _, ch = seed()
    data = bundle.export_bundle()

    assert data["kind"] == "alle-bundle" and data["bundle_version"] == 1
    channel = data["providers"]["nordvpn"]["channels"][ch.id]
    # no port/probe/reconnect; enabled is explicit (unstated means keep-as-is
    # on a merge, so a faithful backup must always state it)
    assert set(channel) == {"country", "city", "label", "wg", "enabled"}
    assert channel["enabled"] is True
    assert channel["label"] == "US East"
    assert data["providers"]["nordvpn"]["credential"] == {"token": "tok-123"}
    assert "credential" not in data["providers"]["protonvpn"]

    router = data["router"]
    assert router["killswitch"] is True
    assert router["lan_direct"] is True  # explicit even though state uses a default
    assert [rs["name"] for rs in router["rulesets"]] == ["Streaming", "Fallback"]
    for rs in router["rulesets"]:  # ids are allocation artifacts — never exported
        assert "id" not in rs
        for m in rs["matchers"]:
            assert set(m) <= {"type", "value"}
    assert router["rulesets"][1]["matchers"] == [{"type": "all"}]


def test_export_roundtrips_through_yaml():
    seed()
    data = bundle.export_bundle()
    assert bundle.loads(bundle.dumps(data)) == data


# ---- validation -----------------------------------------------------------------


def test_validation_reports_every_problem_and_changes_nothing():
    seed()
    before = Store.load().data
    bad = """
kind: alle-bundle
bundle_version: 1
providers:
  nosuch: {}
  nordvpn:
    credential: {token: ""}
    channels:
      Bad-Id: {}
      ok_1:
        country: US
        wg: {private_key: short, address: [], peer: {public_key: bad}}
router:
  killswitch: "yes"
  rulesets:
    - name: ""
      target: nordvpn/ghost_9
      matchers: []
"""
    with pytest.raises(bundle.BundleError) as e:
        bundle.apply_import(bad)
    paths = [p for p, _ in e.value.entries]
    assert "providers.nosuch" in paths
    assert "providers.nordvpn.credential" in paths
    assert "providers.nordvpn.channels.Bad-Id" in paths
    assert "providers.nordvpn.channels.ok_1.wg.private_key" in paths
    assert "router.killswitch" in paths
    assert "router.rulesets[0].target" in paths
    assert "router.rulesets[0].matchers" in paths
    assert Store.load().data == before
    assert credentials.get("nordvpn") == {"token": "tok-123"}


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("just: yaml", "not an alle bundle"),
        ("kind: alle-bundle", "bundle_version"),
        ("kind: alle-bundle\nbundle_version: 99", "newer than this alle"),
        ("- a\n- b", "root is not a mapping"),
        ("kind: [unclosed", "not valid YAML"),
    ],
)
def test_header_and_shape_rejections(text, needle):
    with pytest.raises(bundle.BundleError) as e:
        bundle.apply_import(text)
    assert needle in str(e.value)


def test_config_channel_requires_wg_snapshot():
    text = """
kind: alle-bundle
bundle_version: 1
providers:
  protonvpn:
    channels:
      proton_us_1: {country: ""}
"""
    with pytest.raises(bundle.BundleError) as e:
        bundle.apply_import(text)
    assert "wg is required" in str(e.value)


def test_restore_target_must_exist_in_bundle_but_import_may_use_existing():
    _, ch = seed()
    text = f"""
kind: alle-bundle
bundle_version: 1
router:
  rulesets:
    - name: Work
      target: nordvpn/{ch.id}
      matchers: [api.example.com]
"""
    # import: the target lives in the existing setup — fine
    summary = bundle.apply_import(text)
    assert summary["rulesets_added"] == ["Work"]
    # restore: the bundle must be self-contained
    with pytest.raises(bundle.BundleError) as e:
        bundle.apply_restore(text)
    assert f"no channel 'nordvpn/{ch.id}' to route to" in str(e.value)


# ---- restore --------------------------------------------------------------------


def test_restore_roundtrip_keeps_ports_and_replaces_setup():
    store, ch = seed()
    old_port = channel(store, "nordvpn", ch.id).port
    text = bundle.dumps(bundle.export_bundle())

    summary = bundle.apply_restore(text)

    after = Store.load()
    restored = channel(after, "nordvpn", ch.id)
    assert restored.port == old_port  # same identity on the same machine
    assert restored.label == "US East"
    assert restored.probe == {} and restored.reconnect == {}  # runtime state reset
    assert [b["name"] for b in after.rulesets()] == ["Streaming", "Fallback"]
    assert after.router["killswitch"] is True
    assert credentials.get("nordvpn") == {"token": "tok-123"}
    assert summary["removed"] == {"providers": [], "channels": []}


def test_restore_removes_everything_not_in_the_bundle():
    store, ch = seed()
    text = bundle.dumps(bundle.export_bundle())
    # extra state the bundle does not contain
    store.add_provider("protonvpn")
    extra, _ = store.upsert_channel("protonvpn", "proton_extra", "", "", wg("9.9.9.9"))
    credentials.set_("protonvpn", {"token": "should-vanish"})

    summary = bundle.apply_restore(text)

    after = Store.load()
    assert after.get_channel("protonvpn", "proton_extra") is None
    assert credentials.get("protonvpn") is None
    assert "protonvpn/proton_extra" in summary["removed"]["channels"]
    assert after.get_channel("nordvpn", ch.id) is not None


def test_restore_prunes_metrics_of_channels_dropped_from_retained_providers():
    from alle import metrics

    store, ch = seed()
    text = bundle.dumps(bundle.export_bundle())  # does NOT contain japan_1
    extra = store.add_channel("nordvpn", "Japan", "", wg("8.8.8.8"))
    metrics.add_delta("nordvpn", ch.id, 10, 10)
    metrics.add_delta("nordvpn", extra.id, 20, 20)

    bundle.apply_restore(text)

    # nordvpn is retained but japan_1 was dropped: its totals go, ch's stay
    assert ("nordvpn", extra.id) not in metrics.totals()
    assert ("nordvpn", ch.id) in metrics.totals()
    # …and a late daemon sample cannot resurrect the deleted row
    metrics.add_delta("nordvpn", extra.id, 99, 99)
    assert ("nordvpn", extra.id) not in metrics.totals()


def test_restore_allocates_fresh_ports_for_new_identities(monkeypatch):
    seed()
    data = bundle.export_bundle()
    chans = data["providers"]["nordvpn"]["channels"]
    chans["brand_new_1"] = dict(chans[next(iter(chans))])  # same wg, new identity
    monkeypatch.setattr(bundle, "provider_resolver", _factory_down)
    summary = bundle.apply_restore(bundle.dumps(data))
    after = Store.load()
    ports = [c.port for c in after.channels()]
    assert all(p > 0 for p in ports)
    assert len(set(ports)) == len(ports)
    # the new identity tried a fresh resolve, failed, and used the snapshot
    assert summary["wg_fallback"] == ["nordvpn/brand_new_1"]


# ---- import ---------------------------------------------------------------------


def test_import_upserts_by_provider_and_id(monkeypatch):
    store, ch = seed()
    old_port = channel(store, "protonvpn", "proton_us_1").port
    data = bundle.export_bundle()
    # config channel: the snapshot IS the config — an edited one applies in place
    proton = data["providers"]["protonvpn"]["channels"]["proton_us_1"]
    proton["wg"]["peer"]["endpoint_host"] = "9.9.9.9"
    # token channel: a new identity resolves fresh via the token
    nord = data["providers"]["nordvpn"]["channels"]
    nord["fresh_1"] = {"country": "Sweden", "city": ""}
    del data["router"]["rulesets"]  # channel-only merge

    monkeypatch.setattr(bundle, "provider_resolver", _factory("8.8.8.8"))
    summary = bundle.apply_import(bundle.dumps(data))

    after = Store.load()
    updated = channel(after, "protonvpn", "proton_us_1")
    assert updated.wg["peer"]["endpoint_host"] == "9.9.9.9"
    assert updated.port == old_port  # update in place, port kept
    fresh = channel(after, "nordvpn", "fresh_1")
    assert fresh.country == "Sweden"
    assert fresh.wg["peer"]["endpoint_host"] == "8.8.8.8"
    assert summary["channels"]["updated"] == ["protonvpn/proton_us_1"]
    assert summary["channels"]["created"] == ["nordvpn/fresh_1"]
    assert summary["channels"]["unchanged"] == [f"nordvpn/{ch.id}"]
    assert summary["wg_resolved"] == ["nordvpn/fresh_1"]
    # entries not in the bundle are kept (nothing was removed)
    assert after.get_channel("protonvpn", "proton_us_1") is not None


def test_import_appends_rulesets_at_the_bottom():
    seed()
    text = bundle.dumps(bundle.export_bundle())
    summary = bundle.apply_import(text)
    assert summary["rulesets_added"] == ["Streaming", "Fallback"]
    names = [b["name"] for b in Store.load().rulesets()]
    assert names == ["Streaming", "Fallback", "Streaming", "Fallback"]


def test_import_reports_credential_replacement():
    seed()
    text = bundle.dumps(bundle.export_bundle())
    summary = bundle.apply_import(text)  # identical credential — not a replacement
    assert summary["credentials"] == {"added": [], "replaced": []}

    credentials.set_("nordvpn", {"token": "rotated-since-backup"})
    summary = bundle.apply_import(text)
    assert summary["credentials"]["replaced"] == ["nordvpn"]
    assert credentials.get("nordvpn") == {"token": "tok-123"}


def test_import_commits_state_in_one_transaction(monkeypatch):
    from contextlib import contextmanager

    from alle import state

    seed()
    data = bundle.export_bundle()
    data["providers"]["nordvpn"]["channels"]["fresh_1"] = {
        "country": "Sweden",
        "city": "",
        "wg": wg("7.7.7.7"),
    }
    monkeypatch.setattr(bundle, "provider_resolver", _factory("8.8.8.8"))

    opened = []
    real = state.transaction

    @contextmanager
    def counting():
        opened.append(1)
        with real() as raw:
            yield raw

    monkeypatch.setattr(state, "transaction", counting)
    bundle.apply_import(bundle.dumps(data))

    # providers, channels, rulesets, and toggles all land in ONE state
    # transaction — the merge's commit point, never a mutation sequence.
    assert len(opened) == 1


def test_import_failure_before_commit_leaves_setup_untouched(monkeypatch):
    seed()
    data = bundle.export_bundle()
    data["providers"]["nordvpn"]["credential"] = {"token": "tok-456"}
    before_state = Store.load().data

    def boom(self, providers, rulesets, killswitch, lan_direct):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(Store, "merge_setup", boom)

    with pytest.raises(RuntimeError, match="state write failed"):
        bundle.apply_import(bundle.dumps(data))

    # the already-written credential was rolled back with the failed merge
    assert credentials.get("nordvpn") == {"token": "tok-123"}
    assert Store.load().data == before_state


def test_restore_failure_before_commit_leaves_credentials_untouched(monkeypatch):
    seed()
    data = bundle.export_bundle()
    data["providers"]["nordvpn"]["credential"] = {"token": "tok-456"}

    def boom(self, providers, rulesets, killswitch, lan_direct):
        raise RuntimeError("state write failed")

    monkeypatch.setattr(Store, "restore_setup", boom)

    with pytest.raises(RuntimeError, match="state write failed"):
        bundle.apply_restore(bundle.dumps(data))

    assert credentials.get("nordvpn") == {"token": "tok-123"}
    assert Store.load().provider_names() == ["nordvpn", "protonvpn"]


def test_import_into_empty_state_creates_providers(monkeypatch):
    seed()
    text = bundle.dumps(bundle.export_bundle())
    bundle.apply_restore(
        "kind: alle-bundle\nbundle_version: 1\n"
    )  # wipe to nothing first
    assert Store.load().provider_names() == []

    monkeypatch.setattr(bundle, "provider_resolver", _factory_down)
    summary = bundle.apply_import(text)
    assert sorted(summary["providers_added"]) == ["nordvpn", "protonvpn"]
    assert summary["credentials"]["added"] == ["nordvpn"]
    assert len(summary["channels"]["created"]) == 2
    # new token identity + unreachable API -> restored from the snapshot
    assert summary["wg_fallback"] == ["nordvpn/united_states_1"]
    assert Store.load().router["killswitch"] is True


# ---- token wg is derived state ----------------------------------------------------


def test_fresh_resolve_wins_over_snapshot_and_derives_once(monkeypatch):
    store, ch = seed()
    data = bundle.export_bundle()
    data["providers"]["nordvpn"]["channels"]["sweden_1"] = {
        "country": "Sweden",
        "city": "",
        "wg": wg("2.2.2.2"),
    }
    text = bundle.dumps(data)
    bundle.apply_restore("kind: alle-bundle\nbundle_version: 1\n")  # a new machine

    factories = []

    def factory(provider, creds):
        factories.append((provider, creds))
        return lambda country, city="": wg("7.7.7.7")

    monkeypatch.setattr(bundle, "provider_resolver", factory)
    summary = bundle.apply_import(text)

    # both nord channels resolved fresh; the account key was derived once
    assert factories == [("nordvpn", {"token": "tok-123"})]
    after = Store.load()
    assert channel(after, "nordvpn", ch.id).wg["peer"]["endpoint_host"] == "7.7.7.7"
    assert (
        channel(after, "nordvpn", "sweden_1").wg["peer"]["endpoint_host"] == "7.7.7.7"
    )
    assert summary["wg_resolved"] == [f"nordvpn/{ch.id}", "nordvpn/sweden_1"]
    assert summary["wg_fallback"] == []
    # the config channel is applied exactly as exported — never resolved
    assert (
        channel(after, "protonvpn", "proton_us_1").wg["peer"]["endpoint_host"]
        == "5.6.7.8"
    )


def test_existing_channel_same_location_keeps_live_params_without_api(monkeypatch):
    _, ch = seed()
    data = bundle.export_bundle()
    # a stale/foreign snapshot for the same identity and location
    data["providers"]["nordvpn"]["channels"][ch.id]["wg"] = wg("9.9.9.9")

    monkeypatch.setattr(bundle, "provider_resolver", _factory_forbidden)
    summary = bundle.apply_import(bundle.dumps(data))

    live = channel(Store.load(), "nordvpn", ch.id)
    assert live.wg["peer"]["endpoint_host"] == "1.2.3.4"  # live params kept
    assert f"nordvpn/{ch.id}" in summary["channels"]["unchanged"]
    assert summary["wg_resolved"] == [] and summary["wg_fallback"] == []


def test_token_resolve_failure_falls_back_to_snapshot(monkeypatch):
    seed()
    text = bundle.dumps(bundle.export_bundle())
    bundle.apply_restore("kind: alle-bundle\nbundle_version: 1\n")  # a new machine

    monkeypatch.setattr(bundle, "provider_resolver", _factory_down)
    summary = bundle.apply_restore(text)

    nord = channel(Store.load(), "nordvpn", "united_states_1")
    assert nord.wg["peer"]["endpoint_host"] == "1.2.3.4"  # the snapshot
    assert summary["wg_fallback"] == ["nordvpn/united_states_1"]
    assert summary["wg_resolved"] == []


def test_token_snapshot_with_credential_falls_back_when_api_down(monkeypatch):
    # token present (required) but the API is unreachable → keep the snapshot
    data = {
        "kind": "alle-bundle",
        "bundle_version": 1,
        "providers": {
            "nordvpn": {
                "credential": {"token": "tok-123"},
                "channels": {"sweden_1": {"country": "Sweden", "wg": wg("3.3.3.3")}},
            }
        },
        "router": {"killswitch": False, "lan_direct": True},
    }
    monkeypatch.setattr(bundle, "provider_resolver", _factory_down)
    summary = bundle.apply_restore(bundle.dumps(data))
    assert summary["wg_fallback"] == ["nordvpn/sweden_1"]
    ch = channel(Store.load(), "nordvpn", "sweden_1")
    assert ch.wg["peer"]["endpoint_host"] == "3.3.3.3"


# ---- wg-less token channels (hand-authored) --------------------------------------


HANDWRITTEN = """
kind: alle-bundle
bundle_version: 1
providers:
  nordvpn:
    credential: {token: tok-999}
    channels:
      sweden_1: {country: Sweden}
router:
  rulesets:
    - name: Streaming
      target: nordvpn/sweden_1
      matchers: [netflix.com, 10.8.0.0/16, api.example.com]
"""


def test_wgless_channel_resolves_via_token_and_matchers_infer(monkeypatch):
    calls = []

    def factory(provider, creds):
        def resolve(country, city=""):
            calls.append((provider, creds, country, city))
            return wg("7.7.7.7")

        return resolve

    monkeypatch.setattr(bundle, "provider_resolver", factory)
    summary = bundle.apply_import(HANDWRITTEN)

    assert calls == [("nordvpn", {"token": "tok-999"}, "Sweden", "")]
    assert summary["channels"]["created"] == ["nordvpn/sweden_1"]
    assert summary["wg_resolved"] == ["nordvpn/sweden_1"]
    after = Store.load()
    assert (
        channel(after, "nordvpn", "sweden_1").wg["peer"]["endpoint_host"] == "7.7.7.7"
    )
    types = [(r["type"], r["value"]) for r in after.rules()]
    assert types == [
        ("domain_suffix", "netflix.com"),
        ("ip_cidr", "10.8.0.0/16"),
        # inferred domains default to suffix now (was: exact for ≥3 labels)
        ("domain_suffix", "api.example.com"),
    ]


def test_legacy_explicit_domain_type_imports_as_suffix(monkeypatch):
    # Old exported bundles may carry the removed exact type as
    # {type: domain, …}: it imports as domain_suffix (one domain semantic).
    legacy = HANDWRITTEN.replace(
        "matchers: [netflix.com, 10.8.0.0/16, api.example.com]",
        "matchers: [{type: domain, value: api.example.com}]",
    )

    def factory(provider, creds):
        return lambda country, city="": wg("7.7.7.7")

    monkeypatch.setattr(bundle, "provider_resolver", factory)
    bundle.apply_import(legacy)
    types = [(r["type"], r["value"]) for r in Store.load().rules()]
    assert types == [("domain_suffix", "api.example.com")]


def test_wgless_channel_keeps_existing_params_when_location_unchanged(monkeypatch):
    store = Store.load()
    store.add_provider("nordvpn")
    credentials.set_("nordvpn", {"token": "tok-999"})
    store.upsert_channel("nordvpn", "sweden_1", "Sweden", "", wg("6.6.6.6"))

    monkeypatch.setattr(bundle, "provider_resolver", _factory_forbidden)
    summary = bundle.apply_import(HANDWRITTEN)
    assert summary["channels"]["unchanged"] == ["nordvpn/sweden_1"]
    assert (
        channel(Store.load(), "nordvpn", "sweden_1").wg["peer"]["endpoint_host"]
        == "6.6.6.6"
    )


def test_token_provider_without_a_token_is_rejected():
    text = """
kind: alle-bundle
bundle_version: 1
providers:
  nordvpn:
    channels:
      sweden_1: {country: Sweden}
"""
    with pytest.raises(bundle.BundleError) as e:
        bundle.apply_import(text)
    assert "a non-empty token is required for NordVPN" in str(e.value)


def test_resolve_failure_without_snapshot_mutates_nothing(monkeypatch):
    # wg-less channels have no fallback: a failed resolve rejects the whole apply
    monkeypatch.setattr(bundle, "provider_resolver", _factory_down)
    before = Store.load().data
    with pytest.raises(bundle.BundleError) as e:
        bundle.apply_import(HANDWRITTEN)
    assert "could not resolve a server: api down" in str(e.value)
    assert Store.load().data == before
    assert credentials.get("nordvpn") is None  # bundle credential not persisted


# ---- dedicated validation (validate_file / alle validate) ------------------------

VALID_LOCATIONS = {"united states": {"new york", "los angeles"}, "sweden": set()}


def _fake_locations(monkeypatch, countries=None):
    """Patch the service location lookup so validation never hits the network."""
    data = countries if countries is not None else VALID_LOCATIONS

    def factory():
        return (lambda provider: data if provider == "nordvpn" else None), []

    monkeypatch.setattr(service, "_bundle_location_lookup", factory)


def test_validate_file_accepts_a_good_bundle():
    seed()
    parsed = bundle.validate_file(bundle.dumps(bundle.export_bundle()))
    assert set(parsed["providers"]) == {"nordvpn", "protonvpn"}


def test_validate_reports_every_problem_with_line_numbers():
    text = (
        "kind: alle-bundle\n"  # 1
        "bundle_version: 1\n"  # 2
        "providers:\n"  # 3
        "  nordvpn:\n"  # 4
        "    credential: {token: t}\n"  # 5
        "    channels:\n"  # 6
        "      us_1:\n"  # 7
        "        country: United States\n"  # 8
        "        wg:\n"  # 9
        "          private_key: short\n"  # 10
        "          address: []\n"  # 11
        "          peer: {public_key: bad}\n"  # 12
        "router:\n"  # 13
        "  killswitch: true\n"  # 14
    )
    with pytest.raises(bundle.BundleError) as e:
        bundle.validate_file(text)
    paths = [p for p, _ in e.value.entries]
    assert "providers.nordvpn.channels.us_1.wg.private_key" in paths
    assert "router.lan_direct" in paths  # strict: must be explicit
    # the private_key error points at the exact line it sits on
    assert e.value.line_index["providers.nordvpn.channels.us_1.wg.private_key"] == 10
    assert str(e.value).count("\n  line ") >= 4  # every row carries a line no.


def test_duplicate_channel_id_is_flagged():
    text = (
        "kind: alle-bundle\n"
        "bundle_version: 1\n"
        "providers:\n"
        "  nordvpn:\n"
        "    credential: {token: t}\n"
        "    channels:\n"
        "      us_1: {country: United States}\n"
        "      us_1: {country: Sweden}\n"
        "router:\n"
        "  killswitch: false\n"
        "  lan_direct: true\n"
    )
    with pytest.raises(bundle.BundleError) as e:
        bundle.validate_file(text)
    assert any("duplicate channel id 'us_1'" in r for _, r in e.value.entries)
    assert e.value.line_index["providers.nordvpn.channels.us_1"] == 8  # the 2nd one


def test_router_toggles_required_when_strict_but_not_on_merge():
    text = (
        "kind: alle-bundle\n"
        "bundle_version: 1\n"
        "router:\n"
        "  rulesets:\n"
        "    - name: Direct all\n"
        "      target: direct\n"
        "      matchers: [all]\n"
    )
    with pytest.raises(bundle.BundleError) as e:
        bundle.validate_file(text)
    paths = [p for p, _ in e.value.entries]
    assert "router.killswitch" in paths and "router.lan_direct" in paths
    # a merge tolerates a partial router (only add rulesets)
    assert bundle.apply_import(text)["rulesets_added"] == ["Direct all"]


def test_country_and_city_checked_against_the_provider_list():
    text = (
        "kind: alle-bundle\n"
        "bundle_version: 1\n"
        "providers:\n"
        "  nordvpn:\n"
        "    credential: {token: t}\n"
        "    channels:\n"
        "      a_1: {country: Atlantis}\n"
        "      b_1: {country: United States, city: Nowhere}\n"
        "router:\n"
        "  killswitch: false\n"
        "  lan_direct: true\n"
    )

    def lookup(provider):
        return VALID_LOCATIONS if provider == "nordvpn" else None

    with pytest.raises(bundle.BundleError) as e:
        bundle.validate_file(text, location_lookup=lookup)
    reasons = " | ".join(r for _, r in e.value.entries)
    assert "not a known NordVPN country" in reasons
    assert "not a known NordVPN city" in reasons


def test_unknown_matcher_type_is_rejected():
    text = (
        "kind: alle-bundle\n"
        "bundle_version: 1\n"
        "router:\n"
        "  killswitch: false\n"
        "  lan_direct: true\n"
        "  rulesets:\n"
        "    - name: Bad\n"
        "      target: direct\n"
        "      matchers:\n"
        "        - {type: regex, value: foo}\n"
    )
    with pytest.raises(bundle.BundleError) as e:
        bundle.validate_file(text)
    assert "unknown matcher type" in str(e.value)


def test_cli_validate_ok(capsys, tmp_path, monkeypatch):
    seed()
    _fake_locations(monkeypatch)
    f = tmp_path / "b.yaml"
    run_cli(["export", "--out", str(f)], capsys)
    out = run_cli(["validate", str(f)], capsys)
    assert "is a valid bundle" in out


def test_cli_validate_reports_errors():
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("kind: alle-bundle\nbundle_version: 1\nproviders:\n  nosuch: {}\n")
        path = fh.name
    with pytest.raises(SystemExit) as e:
        cli.main(["validate", path])
    assert "unknown provider" in str(e.value.code)


# ---- CLI ------------------------------------------------------------------------


def run_cli(args, capsys):
    cli.main(args)
    return capsys.readouterr().out


def test_cli_export_writes_0600_with_default_name(capsys, tmp_path, monkeypatch):
    seed()
    monkeypatch.chdir(tmp_path)
    out = run_cli(["export"], capsys)
    files = list(tmp_path.glob("alle-backup-*.yaml"))
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert "keep it private" in out
    assert "tok-123" in files[0].read_text()


def test_cli_export_stdout(capsys):
    seed()
    out = run_cli(["export", "--out", "-"], capsys)
    assert "kind: alle-bundle" in out and "tok-123" in out


def test_cli_import_prints_summary(capsys, tmp_path):
    seed()
    f = tmp_path / "b.yaml"
    run_cli(["export", "--out", str(f)], capsys)
    out = run_cli(["import", str(f)], capsys)
    assert "+ ruleset 'Streaming'" in out
    assert "2 channel(s) already up to date" in out


def test_cli_replace_requires_yes_when_not_a_tty(capsys, tmp_path):
    seed()
    f = tmp_path / "b.yaml"
    run_cli(["export", "--out", str(f)], capsys)
    with pytest.raises(SystemExit) as e:
        cli.main(["import", "--replace", str(f)])
    assert "--yes" in str(e.value.code)
    out = run_cli(["import", "--replace", "--yes", str(f)], capsys)
    assert "Replaced the setup" in out


def test_cli_import_missing_file_fails_cleanly():
    with pytest.raises(SystemExit) as e:
        cli.main(["import", "/nonexistent/b.yaml"])
    assert "could not read" in str(e.value.code)


# ---- API ------------------------------------------------------------------------


@pytest.fixture
def live():
    seed()
    httpd = server.build_server()
    Thread(target=httpd.serve_forever, daemon=True).start()
    api = server.control_api()
    try:
        yield f"http://{api['address']}", api["secret"], api["address"]
    finally:
        httpd.shutdown()


def _req(url, *, method="GET", headers=None, data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(r) as resp:  # noqa: S310 (loopback test)
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_api_export_downloads_yaml(live):
    base, secret, _ = live
    status, body, headers = _req(
        base + "/api/v1/export", headers={"Authorization": f"Bearer {secret}"}
    )
    assert status == 200
    assert "attachment" in headers.get("Content-Disposition", "")
    assert "alle-backup-" in headers.get("Content-Disposition", "")
    data = yaml.safe_load(body)
    assert data["kind"] == "alle-bundle" and "redacted" not in data


def test_api_export_requires_auth(live):
    base, _, _ = live
    status, _, _ = _req(base + "/api/v1/export")
    assert status == 401


def test_api_import_merge_and_replace(live):
    base, secret, address = live
    headers = {
        "Authorization": f"Bearer {secret}",
        "Origin": f"http://{address}",
        "Content-Type": "application/json",
    }
    _, text, _ = _req(
        base + "/api/v1/export", headers={"Authorization": f"Bearer {secret}"}
    )

    # default import = merge
    status, body, _ = _req(
        base + "/api/v1/import",
        method="POST",
        headers=headers,
        data={"text": text.decode()},
    )
    assert status == 200
    summary = json.loads(body)
    assert summary["mode"] == "import"
    assert summary["rulesets_added"] == ["Streaming", "Fallback"]

    # import with replace = whole-setup replace (returns the "restore" summary)
    status, body, _ = _req(
        base + "/api/v1/import",
        method="POST",
        headers=headers,
        data={"text": text.decode(), "replace": True},
    )
    assert status == 200
    assert json.loads(body)["mode"] == "restore"
    # the duplicate imported rulesets are gone — replaced by the bundle's two
    assert [b["name"] for b in Store.load().rulesets()] == ["Streaming", "Fallback"]

    status, body, _ = _req(
        base + "/api/v1/import", method="POST", headers=headers, data={"text": "nope"}
    )
    assert status == 400
    assert "not an alle bundle" in json.loads(body)["error"]


def test_api_validate(live, monkeypatch):
    base, secret, address = live
    _fake_locations(monkeypatch)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Origin": f"http://{address}",
        "Content-Type": "application/json",
    }
    _, text, _ = _req(
        base + "/api/v1/export", headers={"Authorization": f"Bearer {secret}"}
    )
    status, body, _ = _req(
        base + "/api/v1/validate",
        method="POST",
        headers=headers,
        data={"text": text.decode()},
    )
    assert status == 200
    assert json.loads(body)["valid"] is True

    status, body, _ = _req(
        base + "/api/v1/validate",
        method="POST",
        headers=headers,
        data={
            "text": "kind: alle-bundle\nbundle_version: 1\nproviders:\n  nosuch: {}\n"
        },
    )
    assert status == 400
    assert "unknown provider" in json.loads(body)["error"]
