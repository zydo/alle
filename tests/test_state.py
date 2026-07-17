"""The consolidated state.json store: provider→channel model, auto-naming, ports,
probe round-trips, cascade removal, and the config signature."""

from __future__ import annotations

import json
import stat
import threading

import pytest

from alle import fsio, paths, state
from alle.state import ReferencedError, Store, StoreReadError, config_signature
from conftest import wg_config


def _rule(store, mtype, value, target):
    """Create a singleton ruleset and return its one rule row (the test
    equivalent of the removed Store.add_rule shim)."""
    return store.create_ruleset(target, target, [(mtype, value)])["rules"][0]


WG = wg_config("se1.example.com")


def _state_file():
    return paths.state_dir() / "state.json"


def test_add_provider_is_idempotent():
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_provider("nordvpn")
    assert Store.load().provider_names() == ["nordvpn"]


def test_transaction_skips_proven_noop_but_writes_real_change(monkeypatch):
    writes = []
    original = state._write_raw
    monkeypatch.setattr(
        state, "_write_raw", lambda data: (writes.append(1), original(data))[1]
    )

    with state.transaction() as data:
        data["providers"].pop("missing", None)
    assert writes == []

    with state.transaction() as data:
        data["providers"]["nordvpn"] = {"channels": {}}
    assert writes == [1]


def test_channel_round_trips_through_disk():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "United States", "San Francisco", dict(WG))
    assert ch.id == "united_states_san_francisco_1"

    got = Store.load().get_channel("nordvpn", ch.id)
    assert got is not None
    assert got.country == "United States" and got.city == "San Francisco"
    assert got.port == ch.port
    assert got.wg["private_key"] == "PRIV="
    assert got.wg["peer"]["endpoint_host"] == "se1.example.com"


def test_direct_channel_lookups_construct_only_requested_scope(monkeypatch):
    store = Store.load()
    for provider in ("nordvpn", "protonvpn"):
        store.add_provider(provider)
        store.add_channel(provider, "US", "", dict(WG))
        store.add_channel(provider, "CA", "", dict(WG))
    store = Store.load()
    original = state._channel_view
    constructed = []

    def counted(provider, cid, channel):
        constructed.append((provider, cid))
        return original(provider, cid, channel)

    monkeypatch.setattr(state, "_channel_view", counted)
    assert [channel.id for channel in store.provider_channels("nordvpn")] == [
        "ca_1",
        "us_1",
    ]
    assert constructed == [("nordvpn", "ca_1"), ("nordvpn", "us_1")]
    constructed.clear()
    ch = store.get_channel("nordvpn", "us_1")
    assert ch is not None and ch.provider == "nordvpn"
    assert constructed == [("nordvpn", "us_1")]
    constructed.clear()
    assert store.get_channel("missing", "us_1") is None
    assert constructed == []


@pytest.mark.parametrize("upsert", [False, True])
def test_channel_write_returns_the_committed_object_if_removed_before_refresh(
    monkeypatch, upsert
):
    store = Store.load()
    store.add_provider("nordvpn")
    entered = threading.Event()
    removed = threading.Event()
    pause = {"enabled": True}
    original = state._read_raw

    def paused_read(*, lock_held=False):
        if not lock_held and pause["enabled"]:
            entered.set()
            assert removed.wait(2)
        return original(lock_held=lock_held)

    monkeypatch.setattr(state, "_read_raw", paused_read)
    result = {}

    def write():
        if upsert:
            result["value"] = store.upsert_channel(
                "nordvpn", "fixed", "US", "", dict(WG)
            )
        else:
            result["value"] = store.add_channel("nordvpn", "US", "", dict(WG))

    writer = threading.Thread(target=write)
    writer.start()
    assert entered.wait(2)
    cid = "fixed" if upsert else "us_1"
    with state.transaction() as data:
        del data["providers"]["nordvpn"]["channels"][cid]
    pause["enabled"] = False
    removed.set()
    writer.join(2)

    assert not writer.is_alive()
    committed = result["value"][0] if upsert else result["value"]
    assert committed.id == cid
    assert Store.load().get_channel("nordvpn", cid) is None


def test_auto_naming_numbers_within_provider():
    store = Store.load()
    store.add_provider("nordvpn")
    a = store.add_channel("nordvpn", "United States", "San Francisco", dict(WG))
    b = store.add_channel("nordvpn", "United States", "San Francisco", dict(WG))
    c = store.add_channel("nordvpn", "United States", "", dict(WG))
    assert [a.id, b.id] == [
        "united_states_san_francisco_1",
        "united_states_san_francisco_2",
    ]
    assert c.id == "united_states_1"
    assert len({a.port, b.port, c.port}) == 3  # each gets its own port


def test_same_name_allowed_across_providers():
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_provider("protonvpn")
    n = store.add_channel("nordvpn", "United States", "", dict(WG))
    p = store.add_channel("protonvpn", "United States", "", dict(WG))
    assert n.id == p.id == "united_states_1"  # ids only need to be unique per provider


def test_remove_channel_keeps_provider():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    assert store.remove_channels([("nordvpn", ch.id)]) == [("nordvpn", ch.id)]
    assert store.remove_channels([("nordvpn", ch.id)]) == []
    after = Store.load()
    assert after.has_provider("nordvpn")  # provider stays even at 0 channels
    assert after.provider_channels("nordvpn") == []


def test_remove_provider_cascades_channels():
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG))
    store.add_channel("nordvpn", "UK", "", dict(WG))
    assert store.remove_providers(["nordvpn"]) == {"nordvpn": 2}
    assert not Store.load().has_provider("nordvpn")


def test_upsert_reimport_clears_reconnect_giveup():
    store = Store.load()
    store.add_provider("protonvpn")
    ch, created = store.upsert_channel(
        "protonvpn", "wg-US-CA-842", "United States", "CA", dict(WG)
    )
    assert created is True
    store.set_reconnect("protonvpn", ch.id, {"failed": True, "attempts": 5})

    fresh = dict(WG, private_key="ROTATED=")
    again, created = store.upsert_channel(
        "protonvpn", "wg-US-CA-842", "United States", "CA", fresh
    )
    assert created is False
    assert again.port == ch.port  # identity (id + port) is stable across re-imports
    assert (
        again.reconnect == {}
    )  # re-import is human intervention: give-up state dropped


def test_label_defaults_to_id_and_round_trips():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    assert ch.label == "" and ch.display == ch.id  # absent → falls back to id

    labelled = store.add_channel("nordvpn", "US", "", dict(WG), label="  Streaming  ")
    assert labelled.label == "  Streaming  "  # stored verbatim (service strips)
    got = Store.load().get_channel("nordvpn", labelled.id)
    assert got is not None and got.display == "  Streaming  "


def test_set_label_sets_and_clears():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    assert store.set_label("nordvpn", ch.id, "Video US") is True
    ch = Store.load().get_channel("nordvpn", ch.id)
    assert ch is not None
    assert ch.label == "Video US"
    store.set_label("nordvpn", ch.id, "")  # empty clears → back to id
    got = Store.load().get_channel("nordvpn", ch.id)
    assert got is not None
    assert got.label == "" and got.display == ch.id
    assert store.set_label("nordvpn", "nope_1", "x") is False  # missing channel


def test_label_is_not_part_of_the_id_or_signature():
    from alle.state import _read_raw, config_signature

    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    before = config_signature(_read_raw())
    store.set_label("nordvpn", ch.id, "Renamed")
    assert config_signature(_read_raw()) == before  # relabel never reconciles
    # id (the handle) is untouched by the label
    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.id == ch.id


def test_reimport_preserves_label_unless_overridden():
    store = Store.load()
    store.add_provider("protonvpn")
    ch, _ = store.upsert_channel(
        "protonvpn", "wg-US-CA-842", "US", "CA", dict(WG), label="My Server"
    )
    assert ch.label == "My Server"
    # re-import with no label keeps the user's name
    again, _ = store.upsert_channel("protonvpn", "wg-US-CA-842", "US", "CA", dict(WG))
    assert again.label == "My Server"
    # an explicit label on re-import overrides
    relabelled, _ = store.upsert_channel(
        "protonvpn", "wg-US-CA-842", "US", "CA", dict(WG), label="New Name"
    )
    assert relabelled.label == "New Name"


def test_set_probe_round_trips():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    store.set_probe(
        "nordvpn", ch.id, {"ok": True, "ip": "1.2.3.4", "latency_ms": 80, "at": 123}
    )
    got = Store.load().get_channel("nordvpn", ch.id)
    assert got is not None
    assert got.probe["ok"] is True and got.probe["ip"] == "1.2.3.4"


def test_state_file_is_private():
    store = Store.load()
    store.add_provider("nordvpn")
    mode = stat.S_IMODE(_state_file().stat().st_mode)
    assert mode == 0o600  # carries WireGuard private keys


def test_tags_are_globally_unique_and_parseable():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "United States", "", dict(WG))
    assert ch.inbound_tag == "in-nordvpn-united_states_1"
    assert ch.outbound_tag == "out-nordvpn-united_states_1"
    from alle.state import tag_to_ref

    assert tag_to_ref(ch.inbound_tag) == ("nordvpn", "united_states_1")
    assert tag_to_ref("direct") is None


def test_corrupt_state_is_quarantined_not_silently_wiped(capsys):
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG))
    _state_file().write_text('{"providers": {"nordvpn"')  # truncated write

    # The corrupt file reads as empty, but its bytes are preserved aside — so a
    # follow-up mutation can never persist the emptiness over the only copy.
    assert Store.load().provider_names() == []
    backups = list(paths.state_dir().glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == '{"providers": {"nordvpn"'
    assert "corrupt" in capsys.readouterr().err

    # Recovery restarts from blank without touching the quarantined copy.
    Store.load().add_provider("protonvpn")
    assert Store.load().provider_names() == ["protonvpn"]
    assert len(list(paths.state_dir().glob("state.json.corrupt-*"))) == 1


def test_non_object_state_is_quarantined():
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text('["not", "an", "object"]')  # valid JSON, wrong shape
    assert Store.load().provider_names() == []
    assert len(list(paths.state_dir().glob("state.json.corrupt-*"))) == 1


def test_malformed_container_schema_is_quarantined():
    # Parses as JSON but the container shapes are unusable for read-modify-write:
    # the same loud quarantine path as outright corruption, never a crash deep
    # inside a mutation (or a partial rewrite around the bad shape).
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps({"version": 1, "providers": ["nordvpn"]}))
    assert Store.load().provider_names() == []
    assert len(list(paths.state_dir().glob("state.json.corrupt-*"))) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"version": 1, "providers": None},
        {"version": 1, "providers": {"nordvpn": {"channels": None}}},
        {
            "version": 1,
            "providers": {"nordvpn": {"channels": {"us_1": {"wg": []}}}},
        },
        {"version": 1, "providers": {}, "router": None},
        {"version": 1, "providers": {}, "router": {"rules": None}},
    ],
)
def test_present_falsy_or_wrong_nested_state_shape_is_quarantined(bad):
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps(bad))

    assert Store.load().provider_names() == []
    backups = list(paths.state_dir().glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == bad


def test_stale_corrupt_reader_never_quarantines_a_new_valid_generation(monkeypatch):
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text('{"providers":')
    entered = threading.Event()
    published = threading.Event()
    original = state._quarantine

    def pause(*args, **kwargs):
        entered.set()
        assert published.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "_quarantine", pause)
    result = {}

    def read():
        result["store"] = Store.load()

    reader = threading.Thread(target=read)
    reader.start()
    assert entered.wait(2)
    with fsio.locked(state._lock_path()):
        state._write_raw(
            {
                "version": 1,
                "providers": {"nordvpn": {"channels": {}}},
                "router": state._router_blank(),
            }
        )
    published.set()
    reader.join(2)

    assert not reader.is_alive()
    assert result["store"].provider_names() == ["nordvpn"]
    assert list(paths.state_dir().glob("state.json.corrupt-*")) == []


def test_quarantine_failure_aborts_without_exposing_a_blank_view(monkeypatch):
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    bad = '{"providers":'
    _state_file().write_text(bad)
    monkeypatch.setattr(
        state.os,
        "link",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with pytest.raises(StoreReadError, match="cannot preserve corrupt state.json"):
        Store.load()
    with pytest.raises(StoreReadError):
        Store().add_provider("nordvpn")
    assert _state_file().read_text() == bad
    assert list(paths.state_dir().glob("state.json.corrupt-*")) == []


def test_quarantine_fsync_failure_aborts_but_keeps_exact_evidence(monkeypatch):
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    bad = '{"providers":'
    _state_file().write_text(bad)
    monkeypatch.setattr(
        state,
        "_fsync_dir_strict",
        lambda path: (_ for _ in ()).throw(OSError("fsync failed")),
    )

    with pytest.raises(StoreReadError, match="cannot preserve corrupt state.json"):
        Store.load()

    assert not _state_file().exists()
    backups = list(paths.state_dir().glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == bad


def test_quarantine_names_never_overwrite_earlier_evidence():
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    for bad in ('{"first":', '{"second":'):
        _state_file().write_text(bad)
        assert Store.load().provider_names() == []

    backups = list(paths.state_dir().glob("state.json.corrupt-*"))
    assert len(backups) == 2
    assert {backup.read_text() for backup in backups} == {'{"first":', '{"second":'}


def test_newer_state_version_aborts_instead_of_quarantining():
    store = Store.load()
    store.add_provider("nordvpn")
    data = json.loads(_state_file().read_text())
    data["version"] = 99  # written by a newer alle
    _state_file().write_text(json.dumps(data))

    with pytest.raises(StoreReadError, match="upgrade alle"):
        Store.load()
    # a mutation aborts before writing anything…
    with pytest.raises(StoreReadError, match="upgrade alle"):
        Store().add_provider("protonvpn")
    # …and the file is neither quarantined nor rewritten — the data is fine,
    # this alle just must not touch it.
    assert list(paths.state_dir().glob("state.json.corrupt-*")) == []
    assert json.loads(_state_file().read_text())["version"] == 99


def test_channel_writes_never_resurrect_a_missing_provider():
    store = Store.load()
    with pytest.raises(ValueError, match="not added"):
        store.add_channel("nordvpn", "US", "", dict(WG))
    with pytest.raises(ValueError, match="not added"):
        store.upsert_channel("protonvpn", "p1", "", "", dict(WG))
    assert Store.load().provider_names() == []  # nothing created implicitly


def test_remove_providers_is_all_or_nothing():
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_provider("protonvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    store.upsert_channel("protonvpn", "p1", "", "", dict(WG))
    _rule(store, "domain_suffix", "netflix.com", f"nordvpn/{ch.id}")

    # One provider's channel is still referenced → the whole batch is refused,
    # including the unreferenced provider.
    with pytest.raises(ReferencedError):
        store.remove_providers(["protonvpn", "nordvpn"])
    assert Store.load().provider_names() == ["nordvpn", "protonvpn"]


def test_remove_channels_is_all_or_nothing():
    store = Store.load()
    store.add_provider("nordvpn")
    ch1 = store.add_channel("nordvpn", "US", "", dict(WG))
    ch2 = store.add_channel("nordvpn", "Japan", "", dict(WG))
    _rule(store, "domain_suffix", "netflix.com", f"nordvpn/{ch2.id}")

    with pytest.raises(ReferencedError):
        store.remove_channels([("nordvpn", ch1.id), ("nordvpn", ch2.id)])
    assert {c.id for c in Store.load().channels()} == {ch1.id, ch2.id}


def test_update_channels_wg_commits_the_whole_batch():
    store = Store.load()
    store.add_provider("nordvpn")
    ch1 = store.add_channel("nordvpn", "US", "", dict(WG))
    ch2 = store.add_channel("nordvpn", "Japan", "", dict(WG))
    fresh = dict(WG, private_key="FRESH=")

    updated = store.update_channels_wg(
        "nordvpn", {ch1.id: dict(fresh), ch2.id: dict(fresh), "ghost_1": dict(fresh)}
    )

    assert sorted(updated) == sorted([ch1.id, ch2.id])  # ghost skipped, not fatal
    reloaded = Store.load()
    for cid in (ch1.id, ch2.id):
        got = reloaded.get_channel("nordvpn", cid)
        assert got is not None
        assert got.wg["private_key"] == "FRESH="


def test_token_channel_refresh_clears_reconnect_for_unresolved_siblings():
    store = Store.load()
    store.add_provider("nordvpn")
    refreshed = store.add_channel("nordvpn", "US", "", dict(WG))
    unresolved = store.add_channel("nordvpn", "Japan", "", dict(WG))
    for ch in (refreshed, unresolved):
        store.set_reconnect("nordvpn", ch.id, {"failed": True, "error": "old token"})
    store.update_channels_wg("nordvpn", {refreshed.id: dict(WG)})
    latest = Store.load()
    refreshed_ch = latest.get_channel("nordvpn", refreshed.id)
    unresolved_ch = latest.get_channel("nordvpn", unresolved.id)
    assert refreshed_ch is not None and refreshed_ch.reconnect == {}
    assert unresolved_ch is not None and unresolved_ch.reconnect == {}


def test_merge_setup_upserts_and_appends_in_one_pass():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG), label="US East")
    old_port = ch.port
    store.set_reconnect("nordvpn", ch.id, {"state": "give-up"})
    store.create_ruleset("Existing", "direct", [("domain_suffix", "a.com")])

    summary = store.merge_setup(
        {
            "nordvpn": {
                ch.id: {
                    "country": "US",
                    "city": "",
                    "label": "",
                    "wg": dict(WG, private_key="NEW="),
                },
            },
            "protonvpn": {
                "p1": {"country": "", "city": "", "label": "", "wg": dict(WG)},
            },
        },
        [
            {
                "name": "Imported",
                "target": "block",
                "matchers": [("domain_suffix", "b.example.com")],
            }
        ],
        killswitch=None,
        lan_direct=None,
    )

    assert summary["providers_added"] == ["protonvpn"]
    assert summary["updated"] == [f"nordvpn/{ch.id}"]
    assert summary["created"] == ["protonvpn/p1"]
    assert summary["rulesets_added"] == ["Imported"]

    reloaded = Store.load()
    got = reloaded.get_channel("nordvpn", ch.id)
    assert got is not None
    assert got.port == old_port  # the local port contract survives an update
    assert got.label == "US East"  # no label in the spec → the user's naming stays
    assert got.reconnect == {}  # an import is human intervention: retry fresh
    assert got.wg["private_key"] == "NEW="
    # imported rulesets append at the BOTTOM of the priority order
    assert [b["name"] for b in reloaded.rulesets()] == ["Existing", "Imported"]
    assert reloaded.router["killswitch"] is False  # None toggles change nothing


def test_merge_setup_unchanged_channel_is_reported_not_rewritten():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    summary = store.merge_setup(
        {
            "nordvpn": {
                ch.id: {"country": "US", "city": "", "label": "", "wg": dict(WG)}
            }
        },
        [],
        killswitch=None,
        lan_direct=None,
    )
    assert summary["unchanged"] == [f"nordvpn/{ch.id}"]
    assert summary["updated"] == [] and summary["created"] == []


def test_unreadable_state_aborts_instead_of_reading_empty():
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG))
    _state_file().chmod(0)  # permission error ≠ absent file
    try:
        with pytest.raises(StoreReadError):
            Store.load()
        # a mutation aborts before writing anything — the data is never
        # replaced by the blank view an unreadable file used to produce
        with pytest.raises(StoreReadError):
            store.add_provider("protonvpn")
    finally:
        _state_file().chmod(0o600)
    assert Store.load().provider_names() == ["nordvpn"]  # nothing was lost
    # and it was not quarantined either — the file itself is fine
    assert list(paths.state_dir().glob("state.json.corrupt-*")) == []


# ---- router entrypoint + rules --------------------------------------------------


def test_router_port_is_a_contract():
    store = Store.load()
    port = store.ensure_router_port()
    assert port > 0
    assert store.ensure_router_port() == port  # allocated once, then stable
    assert Store.load().router["port"] == port
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    assert ch.port != port  # channel allocation avoids the router's port


def test_rules_get_stable_sequential_ids():
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG))
    a = _rule(store, "domain_suffix", "netflix.com", "nordvpn/us_1")
    b = _rule(store, "ip_cidr", "10.0.0.0/8", "direct")
    assert [a["id"], b["id"]] == ["r1", "r2"]
    store.remove_rules(["r1"])
    c = _rule(store, "all", "", "block")
    assert c["id"] == "r3"  # ids are never reused while later ones exist
    assert [r["id"] for r in Store.load().rules()] == ["r2", "r3"]


def test_legacy_exact_domain_rule_reads_as_suffix():
    # Old state files may carry the removed exact "domain" type: it reads as
    # domain_suffix (alle's one domain semantic) and compiles/lints that way.
    from alle.state import transaction

    store = Store.load()
    store.add_provider("nordvpn")
    with transaction() as data:
        data["router"]["rules"] = [
            {
                "id": "r1",
                "type": "domain",
                "value": "api.example.com",
                "target": "direct",
                "ruleset": "rs1",
                "ruleset_name": "Legacy",
            }
        ]
    rules = Store.load().rules()
    assert rules[0]["type"] == "domain_suffix"
    assert rules[0]["value"] == "api.example.com"


def test_rule_channel_target_must_exist():
    store = Store.load()
    store.add_provider("nordvpn")
    with pytest.raises(ValueError, match="no channel 'nordvpn/us_1'"):
        _rule(store, "domain_suffix", "a.com", "nordvpn/us_1")
    # direct/block targets need no channel
    assert _rule(store, "domain_suffix", "a.com", "direct")["id"] == "r1"


def test_referenced_channel_cannot_be_removed():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    _rule(store, "domain_suffix", "netflix.com", f"nordvpn/{ch.id}")

    with pytest.raises(ReferencedError) as exc:
        store.remove_channels([("nordvpn", ch.id)])
    assert f"nordvpn/{ch.id}" in exc.value.blockers
    with pytest.raises(ReferencedError):
        store.remove_providers(["nordvpn"])
    assert Store.load().get_channel("nordvpn", ch.id) is not None  # untouched

    store.remove_rules(["r1"])
    # unreferenced → fine
    assert store.remove_channels([("nordvpn", ch.id)]) == [("nordvpn", ch.id)]


def test_killswitch_round_trips():
    store = Store.load()
    assert store.router["killswitch"] is False
    store.set_killswitch(True)
    assert Store.load().router["killswitch"] is True


def test_tun_defaults_off_and_round_trips():
    store = Store.load()
    assert store.router["tun"] is False
    store.set_tun(True)
    assert Store.load().router["tun"] is True
    store.set_tun(False)
    assert Store.load().router["tun"] is False


def test_lan_direct_defaults_on_and_round_trips():
    store = Store.load()
    assert store.router["lan_direct"] is True  # recommended default
    store.set_lan_direct(False)
    assert Store.load().router["lan_direct"] is False
    store.set_lan_direct(True)
    assert Store.load().router["lan_direct"] is True


def test_reallocate_covers_the_router_port():
    store = Store.load()
    port = store.ensure_router_port()  # auto-allocated -> may move
    moved, held = store.reallocate_channel_ports({port})
    assert held == [] and len(moved) == 1
    who, what, old, new = moved[0]
    assert (who, what, old) == ("router", "entrypoint", port)
    assert new != port and Store.load().router["port"] == new


def test_config_signature_tracks_router_changes():
    from alle.state import _read_raw

    store = Store.load()
    empty = config_signature(_read_raw())
    store.ensure_router_port()
    with_port = config_signature(_read_raw())
    assert with_port != empty  # port allocation must trigger a reconcile
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG))
    before = config_signature(_read_raw())
    _rule(store, "domain_suffix", "a.com", "nordvpn/us_1")
    after_rule = config_signature(_read_raw())
    assert after_rule != before  # rule edits reconcile like channel edits
    store.set_killswitch(True)
    after_kill = config_signature(_read_raw())
    assert after_kill != after_rule
    store.set_lan_direct(False)
    after_lan = config_signature(_read_raw())
    assert after_lan != after_kill  # LAN toggle reconciles too
    store.set_tun(True)
    assert config_signature(_read_raw()) != after_lan  # tun flip reconciles too


def test_config_signature_tracks_tun_alone():
    # tun can flip before the router port or any rule exists; the signature
    # must still move so the daemon reconciles the tun inbound in/out.
    from alle.state import _read_raw

    empty = config_signature(_read_raw())
    Store.load().set_tun(True)
    assert config_signature(_read_raw()) != empty


def test_config_signature_ignores_probe_results():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    from alle.state import _read_raw

    before = config_signature(_read_raw())
    store.set_probe("nordvpn", ch.id, {"ok": True, "ip": "9.9.9.9", "at": 1})
    assert (
        config_signature(_read_raw()) == before
    )  # probe writes don't trigger reconcile

    store.add_channel("nordvpn", "UK", "", dict(WG))
    assert config_signature(_read_raw()) != before  # a new channel does
