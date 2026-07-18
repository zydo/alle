"""Startup sync (`alle sync`): the managed, idempotent boot-apply mode.

The Docker entrypoint runs it on every container start. Provenance rules under
test: repeat syncs of the same bundle are byte-idempotent, edits update each
managed block once, removals prune only managed state, hand-made channels and
rulesets are never touched (or adopted), the ``enabled`` tri-state still keeps
ad-hoc disables, and interactive ``import`` keeps its append/merge semantics.
"""

from __future__ import annotations

import base64

import pytest

from alle import bundle, cli, credentials, paths
from alle.providers import ProviderError
from alle.state import Store

KEY_A = base64.b64encode(bytes([1] * 32)).decode()
KEY_B = base64.b64encode(bytes([2] * 32)).decode()


def wg(host="1.2.3.4"):
    return {
        "private_key": KEY_A,
        "address": ["10.5.0.2/32"],
        "peer": {
            "public_key": KEY_B,
            "endpoint_host": host,
            "endpoint_port": 51820,
            "preshared_key": None,
            "allowed_ips": ["0.0.0.0/0", "::/0"],
            "keepalive": 25,
        },
    }


def bundle_text(channels=None, rulesets=None, router_extra=None):
    """A config-provider bundle (no token resolution, so syncs are offline)."""
    data = {
        "kind": "alle-bundle",
        "bundle_version": 1,
        "providers": {"protonvpn": {"channels": channels or {}}},
    }
    router = dict(router_extra or {})
    if rulesets is not None:
        router["rulesets"] = rulesets
    if router:
        data["router"] = router
    return bundle.dumps(data)


def state_bytes() -> bytes:
    return (paths.state_dir() / "state.json").read_bytes()


def raw_channel(provider, cid) -> dict:
    return Store.load().data["providers"][provider]["channels"][cid]


# ---- idempotency -----------------------------------------------------------------


def test_sync_same_bundle_is_byte_idempotent():
    text = bundle_text(
        channels={"us_1": {"country": "United States", "wg": wg()}},
        rulesets=[
            {"name": "Work", "target": "protonvpn/us_1", "matchers": ["github.com"]},
            {"name": "Fallback", "target": "direct", "matchers": [{"type": "all"}]},
        ],
        router_extra={"killswitch": True, "lan_direct": True},
    )
    first = bundle.apply_sync(text)
    assert first["channels"]["created"] == ["protonvpn/us_1"]
    assert first["rulesets"]["added"] == ["Work", "Fallback"]
    before = state_bytes()

    for _ in range(3):
        again = bundle.apply_sync(text)
        assert again["channels"]["unchanged"] == ["protonvpn/us_1"]
        assert again["channels"]["created"] == []
        assert again["rulesets"]["unchanged"] == ["Work", "Fallback"]
        assert again["rulesets"]["added"] == []
        assert state_bytes() == before

    store = Store.load()
    assert [b["name"] for b in store.rulesets()] == ["Work", "Fallback"]
    assert store.router["killswitch"] is True


def test_sync_marks_created_state_managed_but_import_does_not():
    bundle.apply_sync(
        bundle_text(channels={"us_1": {"country": "United States", "wg": wg()}})
    )
    assert raw_channel("protonvpn", "us_1").get("managed") is True
    assert Store.load().data["providers"]["protonvpn"].get("managed") is True

    bundle.apply_import(
        bundle_text(
            channels={"de_1": {"country": "Germany", "wg": wg("5.6.7.8")}},
            rulesets=[{"name": "Adhoc", "target": "direct", "matchers": ["x.com"]}],
        )
    )
    assert "managed" not in raw_channel("protonvpn", "de_1")
    assert all(
        "managed" not in r for r in Store.load().rules() if r["ruleset_name"] == "Adhoc"
    )


# ---- ruleset convergence ---------------------------------------------------------


def test_sync_updates_a_managed_ruleset_in_place():
    v1 = bundle_text(
        channels={"us_1": {"country": "United States", "wg": wg()}},
        rulesets=[
            {"name": "Work", "target": "protonvpn/us_1", "matchers": ["github.com"]},
            {"name": "Fallback", "target": "direct", "matchers": [{"type": "all"}]},
        ],
    )
    bundle.apply_sync(v1)
    v2 = bundle_text(
        channels={"us_1": {"country": "United States", "wg": wg()}},
        rulesets=[
            {
                "name": "Work",
                "target": "protonvpn/us_1",
                "matchers": ["github.com", "gitlab.com"],
            },
            {"name": "Fallback", "target": "direct", "matchers": [{"type": "all"}]},
        ],
    )
    summary = bundle.apply_sync(v2)
    assert summary["rulesets"]["updated"] == ["Work"]
    assert summary["rulesets"]["unchanged"] == ["Fallback"]

    blocks = Store.load().rulesets()
    assert [b["name"] for b in blocks] == ["Work", "Fallback"]  # position kept
    assert [r["value"] for r in blocks[0]["rules"]] == ["github.com", "gitlab.com"]

    # and the edited bundle is idempotent from here on
    before = state_bytes()
    assert bundle.apply_sync(v2)["rulesets"]["unchanged"] == ["Work", "Fallback"]
    assert state_bytes() == before


def test_sync_prunes_managed_rulesets_but_never_adhoc_ones():
    store = Store.load()
    store.add_provider("protonvpn")
    store.upsert_channel("protonvpn", "hand", "Iceland", "", wg("9.9.9.9"))
    store.create_ruleset("Mine", "protonvpn/hand", [("domain_suffix", "mine.io")])

    with_rules = bundle_text(
        channels={"us_1": {"country": "United States", "wg": wg()}},
        rulesets=[{"name": "Work", "target": "protonvpn/us_1", "matchers": ["g.com"]}],
    )
    bundle.apply_sync(with_rules)
    assert [b["name"] for b in Store.load().rulesets()] == ["Mine", "Work"]

    # bundle drops its rulesets entirely (no router block at all)
    summary = bundle.apply_sync(
        bundle_text(channels={"us_1": {"country": "United States", "wg": wg()}})
    )
    assert summary["rulesets"]["pruned"] == ["Work"]
    assert [b["name"] for b in Store.load().rulesets()] == ["Mine"]


def test_sync_reverts_adhoc_edits_inside_a_managed_block():
    text = bundle_text(
        channels={"us_1": {"country": "United States", "wg": wg()}},
        rulesets=[{"name": "Work", "target": "protonvpn/us_1", "matchers": ["g.com"]}],
    )
    bundle.apply_sync(text)
    rsid = Store.load().rulesets()[0]["id"]
    Store.load().add_ruleset_matchers(rsid, [("domain_suffix", "sneaky.io")])

    summary = bundle.apply_sync(text)
    assert summary["rulesets"]["updated"] == ["Work"]
    blocks = Store.load().rulesets()
    assert [r["value"] for r in blocks[0]["rules"]] == ["g.com"]


# ---- channel and provider pruning ------------------------------------------------


def test_sync_prunes_managed_channels_and_empty_provider_with_credential(monkeypatch):
    monkeypatch.setattr(
        bundle, "provider_resolver", lambda provider, creds: lambda c, city="": wg()
    )
    token_bundle = bundle.dumps(
        {
            "kind": "alle-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {
                    "credential": {"token": "tok-1"},
                    "channels": {"us_1": {"country": "United States"}},
                }
            },
        }
    )
    summary = bundle.apply_sync(token_bundle)
    assert summary["credentials"]["added"] == ["nordvpn"]
    assert credentials.get("nordvpn") == {"token": "tok-1"}

    # the bundle drops the provider entirely -> channel, provider, credential go
    summary = bundle.apply_sync(bundle_text())
    assert summary["channels"]["pruned"] == ["nordvpn/us_1"]
    assert summary["providers_pruned"] == ["nordvpn"]
    assert Store.load().get_channel("nordvpn", "us_1") is None
    assert "nordvpn" not in Store.load().provider_names()
    assert credentials.get("nordvpn") is None


def test_sync_never_prunes_or_adopts_handmade_channels():
    store = Store.load()
    store.add_provider("protonvpn")
    store.upsert_channel("protonvpn", "hand", "Iceland", "", wg("9.9.9.9"))

    # the hand-made channel is not in the bundle: survives every sync
    bundle.apply_sync(
        bundle_text(channels={"us_1": {"country": "United States", "wg": wg()}})
    )
    assert Store.load().get_channel("protonvpn", "hand") is not None

    # a bundle that *does* declare it updates it but never adopts it
    bundle.apply_sync(
        bundle_text(
            channels={
                "us_1": {"country": "United States", "wg": wg()},
                "hand": {"country": "Iceland", "wg": wg("8.8.8.8")},
            }
        )
    )
    ch = raw_channel("protonvpn", "hand")
    assert "managed" not in ch
    assert ch["wg"]["peer"]["endpoint_host"] == "8.8.8.8"

    # dropped from the bundle again -> still not pruned (it was never managed)
    bundle.apply_sync(
        bundle_text(channels={"us_1": {"country": "United States", "wg": wg()}})
    )
    assert Store.load().get_channel("protonvpn", "hand") is not None
    # the provider itself was hand-added too, so it is kept even when empty
    summary = bundle.apply_sync(bundle_text())
    assert summary["providers_pruned"] == []


def test_sync_keeps_a_dropped_channel_that_adhoc_rules_reference():
    bundle.apply_sync(
        bundle_text(channels={"us_1": {"country": "United States", "wg": wg()}})
    )
    Store.load().create_ruleset("Mine", "protonvpn/us_1", [("domain_suffix", "m.io")])

    summary = bundle.apply_sync(bundle_text())
    assert summary["channels"]["pruned"] == []
    assert summary["channels"]["kept_referenced"] == {"protonvpn/us_1": ["Mine"]}
    assert Store.load().get_channel("protonvpn", "us_1") is not None

    # once the rule is gone, the next sync completes the prune
    rsid = Store.load().rulesets()[0]["id"]
    Store.load().remove_ruleset(rsid)
    summary = bundle.apply_sync(bundle_text())
    assert summary["channels"]["pruned"] == ["protonvpn/us_1"]


def test_sync_prunes_channel_with_its_managed_ruleset_in_one_boot():
    bundle.apply_sync(
        bundle_text(
            channels={"us_1": {"country": "United States", "wg": wg()}},
            rulesets=[
                {"name": "Work", "target": "protonvpn/us_1", "matchers": ["g.com"]}
            ],
        )
    )
    # both the channel and the ruleset targeting it leave the bundle together:
    # the managed rule is pruned first, so it never blocks the channel prune
    summary = bundle.apply_sync(bundle_text())
    assert summary["rulesets"]["pruned"] == ["Work"]
    assert summary["channels"]["pruned"] == ["protonvpn/us_1"]
    assert Store.load().rulesets() == []


def test_sync_rename_keeping_declared_port_converges_in_one_boot():
    bundle.apply_sync(
        bundle_text(
            channels={"us_1": {"country": "United States", "port": 25000, "wg": wg()}}
        )
    )
    summary = bundle.apply_sync(
        bundle_text(
            channels={
                "us_east": {"country": "United States", "port": 25000, "wg": wg()}
            }
        )
    )
    assert summary["channels"]["pruned"] == ["protonvpn/us_1"]
    assert summary["channels"]["created"] == ["protonvpn/us_east"]
    ch = Store.load().get_channel("protonvpn", "us_east")
    assert ch is not None and ch.port == 25000


# ---- enable tri-state ------------------------------------------------------------


def test_sync_unstated_enabled_keeps_adhoc_disable():
    text = bundle_text(channels={"us_1": {"country": "United States", "wg": wg()}})
    bundle.apply_sync(text)
    Store.load().set_channels_enabled([("protonvpn", "us_1")], False)

    summary = bundle.apply_sync(text)
    assert summary["channels"]["unchanged"] == ["protonvpn/us_1"]
    ch = Store.load().get_channel("protonvpn", "us_1")
    assert ch is not None and ch.enabled is False

    # explicit enabled: true in the bundle IS a re-enable
    on = bundle_text(
        channels={"us_1": {"country": "United States", "enabled": True, "wg": wg()}}
    )
    assert bundle.apply_sync(on)["channels"]["updated"] == ["protonvpn/us_1"]
    ch = Store.load().get_channel("protonvpn", "us_1")
    assert ch is not None and ch.enabled is True


def test_sync_disable_referenced_by_surviving_rule_is_rejected():
    bundle.apply_sync(
        bundle_text(channels={"us_1": {"country": "United States", "wg": wg()}})
    )
    Store.load().create_ruleset("Mine", "protonvpn/us_1", [("domain_suffix", "m.io")])
    with pytest.raises(bundle.BundleError, match="cannot disable"):
        bundle.apply_sync(
            bundle_text(
                channels={
                    "us_1": {"country": "United States", "enabled": False, "wg": wg()}
                }
            )
        )
    ch = Store.load().get_channel("protonvpn", "us_1")
    assert ch is not None and ch.enabled is True


def test_sync_disable_works_when_only_a_pruned_managed_rule_referenced_it():
    bundle.apply_sync(
        bundle_text(
            channels={"us_1": {"country": "United States", "wg": wg()}},
            rulesets=[
                {"name": "Work", "target": "protonvpn/us_1", "matchers": ["g.com"]}
            ],
        )
    )
    # the same sync drops the managed rule and disables the channel
    summary = bundle.apply_sync(
        bundle_text(
            channels={
                "us_1": {"country": "United States", "enabled": False, "wg": wg()}
            },
            rulesets=[],
        )
    )
    assert summary["rulesets"]["pruned"] == ["Work"]
    ch = Store.load().get_channel("protonvpn", "us_1")
    assert ch is not None and ch.enabled is False


# ---- validation edges ------------------------------------------------------------


def test_sync_ruleset_may_not_target_a_channel_this_sync_prunes():
    bundle.apply_sync(
        bundle_text(channels={"us_1": {"country": "United States", "wg": wg()}})
    )
    with pytest.raises(bundle.BundleError, match="no channel"):
        bundle.apply_sync(
            bundle_text(
                channels={"de_1": {"country": "Germany", "wg": wg("5.6.7.8")}},
                rulesets=[
                    {"name": "Work", "target": "protonvpn/us_1", "matchers": ["g.com"]}
                ],
            )
        )


def test_sync_ruleset_may_target_a_handmade_channel():
    store = Store.load()
    store.add_provider("protonvpn")
    store.upsert_channel("protonvpn", "hand", "Iceland", "", wg("9.9.9.9"))
    summary = bundle.apply_sync(
        bundle_text(
            rulesets=[
                {"name": "Work", "target": "protonvpn/hand", "matchers": ["g.com"]}
            ]
        )
    )
    assert summary["rulesets"]["added"] == ["Work"]


def test_sync_credential_rotation_is_written_once(monkeypatch):
    monkeypatch.setattr(
        bundle, "provider_resolver", lambda provider, creds: lambda c, city="": wg()
    )

    def token_bundle(token):
        return bundle.dumps(
            {
                "kind": "alle-bundle",
                "bundle_version": 1,
                "providers": {
                    "nordvpn": {
                        "credential": {"token": token},
                        "channels": {"us_1": {"country": "United States"}},
                    }
                },
            }
        )

    assert bundle.apply_sync(token_bundle("tok-1"))["credentials"]["added"] == [
        "nordvpn"
    ]
    # same token again: no credential write, state stays byte-identical
    before = state_bytes()
    summary = bundle.apply_sync(token_bundle("tok-1"))
    assert summary["credentials"] == {"added": [], "replaced": [], "removed": []}
    assert state_bytes() == before

    summary = bundle.apply_sync(token_bundle("tok-2"))
    assert summary["credentials"]["replaced"] == ["nordvpn"]
    assert credentials.get("nordvpn") == {"token": "tok-2"}


# ---- interactive merge stays merge ----------------------------------------------


def test_interactive_import_still_appends_and_never_prunes():
    text = bundle_text(
        channels={"us_1": {"country": "United States", "wg": wg()}},
        rulesets=[{"name": "Work", "target": "protonvpn/us_1", "matchers": ["g.com"]}],
    )
    bundle.apply_sync(text)
    # plain import of the same bundle: rulesets append (documented semantics),
    # nothing is pruned, and the managed markers are untouched
    summary = bundle.apply_import(text)
    assert summary["rulesets_added"] == ["Work"]
    assert [b["name"] for b in Store.load().rulesets()] == ["Work", "Work"]
    assert raw_channel("protonvpn", "us_1").get("managed") is True

    # ...and the next sync converges the managed block while the imported
    # (unmanaged) duplicate survives as ad-hoc state
    summary = bundle.apply_sync(text)
    assert summary["rulesets"]["unchanged"] == ["Work"]
    assert [b["name"] for b in Store.load().rulesets()] == ["Work", "Work"]


# ---- CLI -------------------------------------------------------------------------


def run_cli(args, capsys):
    cli.main(args)
    return capsys.readouterr().out


def test_cli_sync_prints_summary(capsys, tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text(
        bundle_text(
            channels={"us_1": {"country": "United States", "wg": wg()}},
            rulesets=[
                {"name": "Work", "target": "protonvpn/us_1", "matchers": ["g.com"]}
            ],
        )
    )
    out = run_cli(["sync", str(f)], capsys)
    assert "+ channel protonvpn/us_1" in out
    assert "+ ruleset 'Work'" in out

    out = run_cli(["sync", str(f)], capsys)
    assert "Nothing to change" in out

    f.write_text(bundle_text())
    out = run_cli(["sync", str(f)], capsys)
    assert "- channel protonvpn/us_1 pruned" in out
    assert "- ruleset 'Work' pruned" in out


def test_cli_sync_missing_file_fails_cleanly():
    with pytest.raises(SystemExit) as e:
        cli.main(["sync", "/nonexistent/b.yaml"])
    assert "could not read" in str(e.value.code)


def test_sync_wg_fallback_keeps_snapshot(monkeypatch):
    def down(provider, creds):
        raise ProviderError("api down")

    monkeypatch.setattr(bundle, "provider_resolver", down)
    text = bundle.dumps(
        {
            "kind": "alle-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {
                    "credential": {"token": "tok-1"},
                    "channels": {
                        "us_1": {"country": "United States", "wg": wg("7.7.7.7")}
                    },
                }
            },
        }
    )
    summary = bundle.apply_sync(text)
    assert summary["wg_fallback"] == ["nordvpn/us_1"]
    ch = Store.load().get_channel("nordvpn", "us_1")
    assert ch is not None and ch.wg["peer"]["endpoint_host"] == "7.7.7.7"
