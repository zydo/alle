"""Channel enable/disable (connection-budget control): a disabled channel is
not materialised — no inbound, no WireGuard endpoint, no probe, no reconnect —
while staying visible everywhere channels are listed. Covers the store
invariants (additive on-disk shape, signature stability, restrict-only
disable), the engine/probe/reconnect skips, the service surface, the CLI
verbs, and the bundle round-trip with its never-dial import semantics."""

from __future__ import annotations

import base64
import json
from typing import cast

import pytest

from alle import bundle, cli, credentials, reconnect, service, singbox
from alle.engine import Engine
from alle.providers import ProviderError
from alle.state import ReferencedError, Store, config_signature
from conftest import wg_config

WG = wg_config("1.2.3.4")
KEY_A = base64.b64encode(bytes([1] * 32)).decode()
KEY_B = base64.b64encode(bytes([2] * 32)).decode()


def seed_channel(country="United States", city=""):
    store = Store.load()
    if not store.has_provider("nordvpn"):
        store.add_provider("nordvpn")
    return store, store.add_channel("nordvpn", country, city, dict(WG))


def raw_channel(provider: str, cid: str) -> dict:
    return json.loads((service.paths.state_dir() / "state.json").read_text())[
        "providers"
    ][provider]["channels"][cid]


# ---- store: on-disk shape + signature --------------------------------------------


def test_enabled_defaults_true_and_key_is_absent_on_disk():
    store, ch = seed_channel()
    ch2 = store.get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is True
    assert "enabled" not in raw_channel("nordvpn", ch.id)


def test_disable_writes_key_enable_removes_it():
    store, ch = seed_channel()
    before = raw_channel("nordvpn", ch.id)

    assert store.set_channels_enabled([("nordvpn", ch.id)], False) == [
        ("nordvpn", ch.id)
    ]
    assert raw_channel("nordvpn", ch.id)["enabled"] is False
    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is False

    assert Store.load().set_channels_enabled([("nordvpn", ch.id)], True) == [
        ("nordvpn", ch.id)
    ]
    after = raw_channel("nordvpn", ch.id)
    assert "enabled" not in after
    # a disable/enable round-trip restores the original shape (minus the probe
    # bookkeeping the disable deliberately cleared)
    before.pop("probe", None)
    after.pop("probe", None)
    assert after == before


def test_toggle_is_idempotent_and_reports_only_changes():
    store, ch = seed_channel()
    assert store.set_channels_enabled([("nordvpn", ch.id)], True) == []
    store.set_channels_enabled([("nordvpn", ch.id)], False)
    assert Store.load().set_channels_enabled([("nordvpn", ch.id)], False) == []


def test_signature_is_stable_for_default_enabled_and_moves_on_toggle():
    store, ch = seed_channel()
    data = json.loads((service.paths.state_dir() / "state.json").read_text())
    baseline = config_signature(data)
    # adding the dataclass default must not change existing digests (no
    # spurious daemon rebuild on upgrade)
    assert config_signature(data) == baseline

    store.set_channels_enabled([("nordvpn", ch.id)], False)
    disabled = config_signature(
        json.loads((service.paths.state_dir() / "state.json").read_text())
    )
    assert disabled != baseline

    Store.load().set_channels_enabled([("nordvpn", ch.id)], True)
    assert (
        config_signature(
            json.loads((service.paths.state_dir() / "state.json").read_text())
        )
        == baseline
    )


def test_disable_clears_probe_and_reconnect_bookkeeping():
    store, ch = seed_channel()
    store.set_probe("nordvpn", ch.id, {"ok": False, "error": "timeout"})
    store.set_reconnect("nordvpn", ch.id, {"fails": 4, "attempts": 2})

    Store.load().set_channels_enabled([("nordvpn", ch.id)], False)

    raw = raw_channel("nordvpn", ch.id)
    assert "probe" not in raw and "reconnect" not in raw


def test_referenced_channel_cannot_be_disabled_and_batch_is_atomic():
    store, ch = seed_channel()
    other = store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.create_ruleset("Streaming", f"nordvpn/{ch.id}", [("domain_suffix", "n.com")])

    with pytest.raises(ReferencedError):
        Store.load().set_channels_enabled(
            [("nordvpn", other.id), ("nordvpn", ch.id)], False
        )
    # all-or-nothing: the unreferenced channel stayed enabled too
    other2 = Store.load().get_channel("nordvpn", other.id)
    assert other2 is not None
    assert other2.enabled is True


def test_rules_cannot_target_a_disabled_channel():
    store, ch = seed_channel()
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    with pytest.raises(ValueError, match="disabled"):
        Store.load().create_ruleset(
            "Streaming", f"nordvpn/{ch.id}", [("domain_suffix", "n.com")]
        )


# ---- engine + probe + reconnect ----------------------------------------------------


def test_disabled_channel_is_not_materialised():
    store, ch = seed_channel()
    keep = store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    config, errors = Engine(Store.load())._build_config()

    tags = {i["tag"] for i in config["inbounds"]}
    assert keep.inbound_tag in tags and ch.inbound_tag not in tags
    assert {e["tag"] for e in config["endpoints"]} == {keep.outbound_tag}
    assert all(
        ch.inbound_tag not in r.get("inbound", []) for r in config["route"]["rules"]
    )
    assert errors == {}  # disabled is intent, not an error


def test_probe_all_defaults_to_the_enabled_set(monkeypatch):
    store, ch = seed_channel()
    keep = store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    eng = Engine(Store.load())
    monkeypatch.setattr(eng.runner, "is_running", lambda: False)
    results = eng.probe_all()

    assert set(results) == {f"nordvpn/{keep.id}"}
    assert "probe" not in raw_channel("nordvpn", ch.id)  # nothing persisted either


def test_reconnect_pass_skips_disabled_channels(monkeypatch):
    store, ch = seed_channel()
    store.set_channels_enabled([("nordvpn", ch.id)], False)
    # a failing probe on a disabled channel is exactly the stale state
    # reconnect must never act on — plant one behind the store's back
    store.set_probe("nordvpn", ch.id, {"ok": False, "error": "timeout"})

    def forbidden(provider, country, city=""):
        raise AssertionError("reconnect must not re-resolve a disabled channel")

    class ForbiddenRunner:
        """Fails on ANY use: a disabled channel must never touch the runner."""

        def __getattr__(self, name):
            raise AssertionError(f"reconnect must not touch the runner ({name})")

    reconnect.run_pass(
        Store.load(),
        runner=cast(singbox.Runner, ForbiddenRunner()),
        now=1e9,
        resolve=forbidden,
    )
    assert "reconnect" not in raw_channel("nordvpn", ch.id)


# ---- service surface ----------------------------------------------------------------


def test_service_toggle_round_trip_and_noop_reporting():
    _, ch = seed_channel()

    result = service.channel_set_enabled_many([f"nordvpn/{ch.id}"], False)
    assert result["changed"] == [f"nordvpn/{ch.id}"] and result["already"] == []

    again = service.channel_set_enabled_many([ch.id], False)
    assert again["changed"] == [] and again["already"] == [f"nordvpn/{ch.id}"]

    back = service.channel_set_enabled_many([ch.id], True)
    assert back["changed"] == [f"nordvpn/{ch.id}"]
    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is True


def test_service_dry_run_plans_without_mutating():
    _, ch = seed_channel()
    result = service.channel_set_enabled_many([ch.id], False, dry_run=True)
    assert result["dry_run"] is True
    assert [i["changed"] for i in result["channels"]] == [True]
    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is True


def test_service_disable_refused_with_blockers_lists_the_fix():
    store, ch = seed_channel()
    store.create_ruleset("Streaming", f"nordvpn/{ch.id}", [("domain_suffix", "n.com")])

    with pytest.raises(service.ServiceError) as exc:
        service.channel_set_enabled_many([ch.id], False)

    assert "cannot disable" in str(exc.value)
    assert f"nordvpn/{ch.id}" in str(exc.value)
    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is True


def test_enable_resolves_a_wgless_token_channel(monkeypatch):
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "United States", "", {})
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    resolved = []

    def fake_wg(provider, country, city=""):
        resolved.append((provider, country, city))
        return dict(WG)

    monkeypatch.setattr(service, "provider_wg", fake_wg)
    result = service.channel_set_enabled_many([ch.id], True)

    assert result["wg_resolved"] == [f"nordvpn/{ch.id}"]
    assert resolved == [("nordvpn", "United States", "")]
    after = Store.load().get_channel("nordvpn", ch.id)
    assert after is not None
    assert after.enabled is True and after.wg == WG


def test_enable_aborts_cleanly_when_resolution_fails(monkeypatch):
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "United States", "", {})
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    def down(provider, country, city=""):
        raise ProviderError("api down")

    monkeypatch.setattr(service, "provider_wg", down)
    with pytest.raises(service.ServiceError, match="resolving a server failed"):
        service.channel_set_enabled_many([ch.id], True)

    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is False


def test_test_lists_disabled_as_skipped_rows(monkeypatch):
    store, ch = seed_channel()
    keep = store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    probed = []

    def fake_probe(self, channels=None):
        assert channels is not None
        probed.extend(f"{c.provider}/{c.id}" for c in channels)
        out = {}
        for c in channels:
            out[f"{c.provider}/{c.id}"] = {
                "ok": True,
                "at": 1,
                "latency_ms": 12.3,
                "ip": "1.2.3.4",
                "error": None,
            }
        return out

    monkeypatch.setattr(service.Engine, "probe_all", fake_probe)
    result = service.test()

    assert probed == [f"nordvpn/{keep.id}"]  # disabled channel never probed
    rows = {row["name"]: row for row in result["channels"]}
    assert rows[ch.id]["state"] == "Disabled" and rows[ch.id]["enabled"] is False
    assert rows[keep.id]["healthy"] is True
    assert result["channel_count"] == 2
    assert result["disabled_count"] == 1
    assert result["failed_count"] == 0  # skipped, not failed


def test_speed_test_skips_disabled_rows(monkeypatch):
    store, ch = seed_channel()
    store.set_channels_enabled([("nordvpn", ch.id)], False)
    monkeypatch.setattr(service.Engine, "probe_all", lambda self, channels=None: {})
    monkeypatch.setattr(
        service.throughput,
        "run",
        lambda *a, **k: pytest.fail("a disabled channel must not be speed-tested"),
    )

    result = service.test(speed=True)
    (row,) = result["channels"]
    assert row["speed_result"]["skip_reason"] == "disabled"


def test_status_snapshot_reports_disabled_state_and_counts():
    store, ch = seed_channel()
    store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    snap = service.status_snapshot()
    rows = {row["name"]: row for row in snap["channels"]}
    assert rows[ch.id]["state"] == "Disabled"
    assert snap["channel_count"] == 2
    assert snap["enabled_count"] == 1 and snap["disabled_count"] == 1


def test_channel_list_carries_enabled_for_json_consumers():
    store, ch = seed_channel()
    store.set_channels_enabled([("nordvpn", ch.id)], False)
    (row,) = service.channel_list()["channels"]
    assert row["enabled"] is False


# ---- CLI ---------------------------------------------------------------------------


def run_cli(argv, capsys):
    cli.main(argv)
    return capsys.readouterr().out.strip()


def test_cli_disable_enable_round_trip(capsys):
    _, ch = seed_channel()

    out = run_cli(["channels", "disable", ch.id], capsys)
    assert f"Disabled channel nordvpn/{ch.id}." in out
    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is False

    out = run_cli(["channels", "disable", ch.id], capsys)
    assert "already disabled — nothing to do" in out

    out = run_cli(["channels", "enable", ch.id], capsys)
    assert f"Enabled channel nordvpn/{ch.id}." in out
    ch3 = Store.load().get_channel("nordvpn", ch.id)
    assert ch3 is not None
    assert ch3.enabled is True


def test_cli_disable_supports_globs_all_and_dry_run(capsys):
    store, _ = seed_channel(city="Seattle")
    seed_channel(city="Chicago")

    dry = run_cli(["channels", "disable", "wg_us_*", "--dry-run"], capsys)
    assert dry.count("Would disable") == 2
    assert all(c.enabled for c in Store.load().channels())

    run_cli(["channels", "disable", "--provider", "nordvpn", "--all"], capsys)
    assert all(not c.enabled for c in Store.load().channels())

    run_cli(["channels", "enable", "--provider", "nordvpn", "--all"], capsys)
    assert all(c.enabled for c in Store.load().channels())


def test_cli_channels_ls_shows_status_column(capsys):
    store, ch = seed_channel()
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    lines = run_cli(["channels", "ls"], capsys).splitlines()
    assert lines[0].split()[-1] == "STATUS"
    assert lines[2].rstrip().endswith("disabled")

    data = json.loads(run_cli(["channels", "ls", "--json"], capsys))
    assert data["channels"][0]["enabled"] is False


def test_cli_status_summarizes_the_enabled_split(capsys):
    store, ch = seed_channel()
    store.add_channel("nordvpn", "Japan", "", dict(WG))
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    out = run_cli(["status"], capsys)
    assert "2 channel(s) (1 enabled)" in out


# ---- bundle round-trip ---------------------------------------------------------------


def bundle_wg(host="1.2.3.4"):
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


def test_export_writes_enabled_explicitly_on_every_channel():
    # explicit on both: an unstated `enabled` means keep-as-is on a merge, so
    # a faithful backup must always state it (same discipline as lan_direct)
    store = Store.load()
    store.add_provider("nordvpn")
    credentials.set_("nordvpn", {"token": "tok-123"})
    on = store.add_channel("nordvpn", "United States", "", bundle_wg())
    off = store.add_channel("nordvpn", "Japan", "", bundle_wg())
    store.set_channels_enabled([("nordvpn", off.id)], False)

    chans = bundle.export_bundle()["providers"]["nordvpn"]["channels"]
    assert chans[on.id]["enabled"] is True
    assert chans[off.id]["enabled"] is False


def test_merge_with_unstated_enabled_keeps_the_adhoc_state(monkeypatch):
    # the Docker entrypoint re-applies bundle.yaml on every container start;
    # a bundle that says nothing about `enabled` must not undo an ad-hoc
    # `channels disable` (and must not resolve/dial the still-disabled channel)
    store = Store.load()
    store.add_provider("nordvpn")
    credentials.set_("nordvpn", {"token": "tok-123"})
    ch = store.add_channel("nordvpn", "United States", "", bundle_wg())
    store.set_channels_enabled([("nordvpn", ch.id)], False)

    monkeypatch.setattr(
        bundle,
        "provider_resolver",
        lambda provider, creds: pytest.fail("must not resolve a kept-disabled channel"),
    )
    text = bundle.dumps(
        {
            "kind": "alle-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {
                    "channels": {ch.id: {"country": "United States", "wg": bundle_wg()}}
                }
            },
        }
    )
    summary = service.setup_import(text)

    assert summary["channels"]["unchanged"] == [f"nordvpn/{ch.id}"]
    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is False

    # an explicit `enabled: true` in the bundle IS a re-enable
    text = text.replace(
        "country: United States", "country: United States\n        enabled: true"
    )
    data = bundle.loads(text)
    assert data["providers"]["nordvpn"]["channels"][ch.id]["enabled"] is True
    service.setup_import(bundle.dumps(data))
    ch3 = Store.load().get_channel("nordvpn", ch.id)
    assert ch3 is not None
    assert ch3.enabled is True


def test_import_never_resolves_or_probes_a_disabled_channel(monkeypatch):
    credentials.set_("nordvpn", {"token": "tok-123"})
    monkeypatch.setattr(
        bundle,
        "provider_resolver",
        lambda provider, creds: pytest.fail(
            "import must not resolve a disabled channel"
        ),
    )
    text = bundle.dumps(
        {
            "kind": "alle-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {
                    "channels": {
                        "spare_1": {"country": "United States", "enabled": False}
                    }
                }
            },
        }
    )
    summary = service.setup_import(text)

    assert summary["channels"]["created"] == ["nordvpn/spare_1"]
    assert summary["wg_resolved"] == [] and summary["wg_fallback"] == []
    ch = Store.load().get_channel("nordvpn", "spare_1")
    assert ch is not None
    assert ch.enabled is False and ch.wg == {}  # wg-less until enabled
    raw = raw_channel("nordvpn", "spare_1")
    assert "probe" not in raw  # nothing to probe, nothing pending


def test_import_keeps_the_snapshot_of_a_disabled_channel(monkeypatch):
    credentials.set_("nordvpn", {"token": "tok-123"})
    monkeypatch.setattr(
        bundle,
        "provider_resolver",
        lambda provider, creds: pytest.fail("no API call for a disabled channel"),
    )
    text = bundle.dumps(
        {
            "kind": "alle-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {
                    "channels": {
                        "spare_1": {
                            "country": "United States",
                            "enabled": False,
                            "wg": bundle_wg("9.9.9.9"),
                        }
                    }
                }
            },
        }
    )
    service.setup_import(text)
    ch = Store.load().get_channel("nordvpn", "spare_1")
    assert ch is not None
    assert ch.enabled is False
    assert ch.wg["peer"]["endpoint_host"] == "9.9.9.9"


def test_import_checks_a_disabled_channels_location_against_the_catalog(monkeypatch):
    credentials.set_("nordvpn", {"token": "tok-123"})
    monkeypatch.setattr(
        service,
        "_bundle_location_lookup",
        lambda: (lambda provider: {"united states": {"seattle"}}, []),
    )
    text = bundle.dumps(
        {
            "kind": "alle-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {
                    "channels": {"spare_1": {"country": "Atlantis", "enabled": False}}
                }
            },
        }
    )
    with pytest.raises(service.ServiceError, match="not a known NordVPN country"):
        service.setup_import(text)
    assert Store.load().get_channel("nordvpn", "spare_1") is None


def test_import_enabled_channels_skip_the_catalog_check(monkeypatch):
    # an enabled channel is validated by resolution itself; the catalog must
    # not even be fetched when nothing is disabled
    credentials.set_("nordvpn", {"token": "tok-123"})
    monkeypatch.setattr(
        service,
        "_bundle_location_lookup",
        lambda: (
            lambda provider: pytest.fail("no catalog fetch for enabled-only bundles"),
            [],
        ),
    )
    monkeypatch.setattr(
        bundle, "provider_resolver", lambda p, c: lambda country, city="": bundle_wg()
    )
    text = bundle.dumps(
        {
            "kind": "alle-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {"channels": {"wg_us_1": {"country": "United States"}}}
            },
        }
    )
    service.setup_import(text)
    assert Store.load().get_channel("nordvpn", "wg_us_1") is not None


def test_bundle_ruleset_cannot_target_a_channel_it_disables():
    credentials.set_("nordvpn", {"token": "tok-123"})
    text = bundle.dumps(
        {
            "kind": "alle-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {
                    "channels": {
                        "spare_1": {
                            "country": "United States",
                            "enabled": False,
                            "wg": bundle_wg(),
                        }
                    }
                }
            },
            "router": {
                "rulesets": [
                    {
                        "name": "Streaming",
                        "target": "nordvpn/spare_1",
                        "matchers": ["netflix.com"],
                    }
                ]
            },
        }
    )
    with pytest.raises(service.ServiceError, match="disabled"):
        service.setup_import(text)


def test_import_cannot_disable_a_channel_an_existing_rule_targets(monkeypatch):
    store = Store.load()
    store.add_provider("nordvpn")
    credentials.set_("nordvpn", {"token": "tok-123"})
    ch = store.add_channel("nordvpn", "United States", "", bundle_wg())
    store.create_ruleset("Streaming", f"nordvpn/{ch.id}", [("domain_suffix", "n.com")])

    data = bundle.export_bundle()
    data["providers"]["nordvpn"]["channels"][ch.id]["enabled"] = False
    del data["router"]["rulesets"]  # keep only the pre-existing live rule

    with pytest.raises(service.ServiceError, match="cannot disable"):
        service.setup_import(bundle.dumps(data))
    ch2 = Store.load().get_channel("nordvpn", ch.id)
    assert ch2 is not None
    assert ch2.enabled is True


def test_restore_round_trips_enabled(monkeypatch):
    store = Store.load()
    store.add_provider("nordvpn")
    credentials.set_("nordvpn", {"token": "tok-123"})
    on = store.add_channel("nordvpn", "United States", "", bundle_wg())
    off = store.add_channel("nordvpn", "Japan", "", bundle_wg("9.9.9.9"))
    store.set_channels_enabled([("nordvpn", off.id)], False)

    text = bundle.dumps(bundle.export_bundle())
    # same-location channels keep live params without an API call, disabled
    # ones are never resolved — restore must not touch the network at all here
    monkeypatch.setattr(
        bundle,
        "provider_resolver",
        lambda provider, creds: pytest.fail("restore must not hit the API"),
    )
    service.setup_restore(text)

    after = Store.load()
    on_channel = after.get_channel("nordvpn", on.id)
    off_channel = after.get_channel("nordvpn", off.id)
    assert on_channel is not None
    assert on_channel.enabled is True
    assert off_channel is not None
    assert off_channel.enabled is False
    assert off_channel.wg["peer"]["endpoint_host"] == "9.9.9.9"
