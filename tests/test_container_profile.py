"""The opt-in container profile enablers — and, above all, that every one of
them is inert by default (the Phase invariant: a host user who never touches
the knobs must see exactly the old behavior)."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from alle import applog, bundle, daemonctl, runtime, service, singbox
from alle.engine import Engine, _listen_addr, _ports_in_use
from alle.state import PortInUseError, Store
from conftest import wg_config

WG = wg_config("1.2.3.4")

# The bundle validator checks real WireGuard key shapes (32 bytes, base64) —
# the conftest stub keys are for state-level tests only.
_KEY = base64.b64encode(bytes([7] * 32)).decode()
BUNDLE_WG = dict(wg_config("1.2.3.4"), private_key=_KEY)
BUNDLE_WG["peer"] = dict(BUNDLE_WG["peer"], public_key=_KEY)


# ---- deterministic / explicit ports (state) ---------------------------------


def test_explicit_channel_port_is_used():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG), port=20001)
    assert ch.port == 20001
    assert Store.load().get_channel("nordvpn", ch.id).port == 20001


def test_explicit_port_conflict_raises_and_changes_nothing():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG), port=20001)
    with pytest.raises(PortInUseError, match="20001.*nordvpn/us_1"):
        store.add_channel("nordvpn", "UK", "", dict(WG), port=20001)
    ids = [c.id for c in Store.load().channels()]
    assert ids == [ch.id]  # the failed add left no half-written channel


def test_explicit_port_rejects_out_of_range():
    store = Store.load()
    store.add_provider("nordvpn")
    with pytest.raises(ValueError, match="1-65535"):
        store.add_channel("nordvpn", "US", "", dict(WG), port=70000)


def test_router_port_conflicts_with_explicit_channel_port():
    store = Store.load()
    store.add_provider("nordvpn")
    port = store.ensure_router_port()
    with pytest.raises(PortInUseError, match="router entrypoint"):
        store.add_channel("nordvpn", "US", "", dict(WG), port=port)


def test_port_base_allocates_sequentially(monkeypatch):
    monkeypatch.setenv("ALLE_PORT_BASE", "21000")
    store = Store.load()
    store.add_provider("nordvpn")
    a = store.add_channel("nordvpn", "US", "", dict(WG))
    b = store.add_channel("nordvpn", "UK", "", dict(WG))
    assert (a.port, b.port) == (21000, 21001)
    assert store.ensure_router_port() == 21002


def test_port_base_skips_explicitly_claimed_ports(monkeypatch):
    monkeypatch.setenv("ALLE_PORT_BASE", "21000")
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG), port=21001)
    a = store.add_channel("nordvpn", "UK", "", dict(WG))
    b = store.add_channel("nordvpn", "DE", "", dict(WG))
    assert (a.port, b.port) == (21000, 21002)


def test_port_base_invalid_value_errors(monkeypatch):
    monkeypatch.setenv("ALLE_PORT_BASE", "not-a-port")
    store = Store.load()
    store.add_provider("nordvpn")
    with pytest.raises(RuntimeError, match="ALLE_PORT_BASE"):
        store.add_channel("nordvpn", "US", "", dict(WG))


def test_default_allocation_stays_os_assigned():
    # The invariant check: without the env knob, ports come from the OS
    # ephemeral pool — i.e. nothing near a fixed low base.
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    assert ch.port > 1024


def test_upsert_repoints_an_existing_channel_on_explicit_port():
    store = Store.load()
    store.add_provider("protonvpn")
    ch, created = store.upsert_channel("protonvpn", "wg-US-1", "US", "", dict(WG))
    assert created
    old_port = ch.port
    ch2, created2 = store.upsert_channel(
        "protonvpn", "wg-US-1", "US", "", dict(WG), port=22000
    )
    assert not created2
    assert ch2.port == 22000 != old_port
    # and 0 keeps the declared port on the next re-import
    ch3, _ = store.upsert_channel("protonvpn", "wg-US-1", "US", "", dict(WG))
    assert ch3.port == 22000


# ---- listen address (engine) -------------------------------------------------


def _engine_store(port=8888):
    data = {
        "version": 1,
        "providers": {
            "nordvpn": {
                "channels": {
                    "us_1": {
                        "country": "US",
                        "city": "",
                        "port": port,
                        "wg": dict(WG),
                        "probe": {},
                    }
                }
            }
        },
        "router": {
            "port": 40000,
            "killswitch": False,
            "lan_direct": True,
            "rules": [],
        },
    }
    return Store(data=data)


def test_listen_defaults_to_loopback():
    config, _ = Engine(_engine_store())._build_config()
    assert {i["listen"] for i in config["inbounds"]} == {"127.0.0.1"}


def test_alle_listen_widens_every_proxy_inbound(monkeypatch):
    monkeypatch.setenv("ALLE_LISTEN", "0.0.0.0")
    config, _ = Engine(_engine_store())._build_config()
    assert {i["listen"] for i in config["inbounds"]} == {"0.0.0.0"}


def test_alle_listen_invalid_value_falls_back_to_loopback(monkeypatch):
    monkeypatch.setenv("ALLE_LISTEN", "everywhere")
    assert _listen_addr() == "127.0.0.1"
    assert "not an IP address" in applog.tail()


def test_stolen_port_parse_covers_non_loopback_binds():
    err = (
        "start inbound/mixed[in-router]: listen tcp 0.0.0.0:40000: "
        "bind: address already in use"
    )
    assert _ports_in_use(err) == {40000}
    err6 = (
        "start inbound/mixed[in-x]: listen tcp [::]:41000: bind: address already in use"
    )
    assert _ports_in_use(err6) == {41000}


# ---- bundle: declared ports ---------------------------------------------------


def _proton_bundle(**channel_extra):
    ch = {"country": "", "city": "", "wg": dict(BUNDLE_WG)}
    ch.update(channel_extra)
    return {
        "kind": "alle-bundle",
        "bundle_version": 1,
        "providers": {"protonvpn": {"channels": {"us_1": ch}}},
    }


def test_bundle_channel_port_applies_on_import():
    result = bundle.apply_import(bundle.dumps(_proton_bundle(port=23000)))
    assert result["channels"]["created"] == ["protonvpn/us_1"]
    assert Store.load().get_channel("protonvpn", "us_1").port == 23000


def test_bundle_router_port_applies_on_import():
    data = _proton_bundle()
    data["router"] = {"port": 24000}
    bundle.apply_import(bundle.dumps(data))
    assert Store.load().router["port"] == 24000


def test_bundle_reimport_with_same_port_is_unchanged():
    bundle.apply_import(bundle.dumps(_proton_bundle(port=23000)))
    result = bundle.apply_import(bundle.dumps(_proton_bundle(port=23000)))
    assert result["channels"]["unchanged"] == ["protonvpn/us_1"]


def test_bundle_duplicate_declared_ports_are_rejected():
    data = _proton_bundle(port=23000)
    data["providers"]["protonvpn"]["channels"]["us_2"] = {
        "country": "",
        "city": "",
        "wg": dict(BUNDLE_WG),
        "port": 23000,
    }
    with pytest.raises(bundle.BundleError, match="also declared"):
        bundle.apply_import(bundle.dumps(data))


def test_bundle_invalid_port_is_rejected():
    with pytest.raises(bundle.BundleError, match="1-65535"):
        bundle.apply_import(bundle.dumps(_proton_bundle(port=0)))


def test_bundle_port_clash_with_existing_setup_changes_nothing():
    store = Store.load()
    store.add_provider("nordvpn")
    store.add_channel("nordvpn", "US", "", dict(WG), port=23000)
    before = Store.load().data
    with pytest.raises(bundle.BundleError, match="23000"):
        bundle.apply_import(bundle.dumps(_proton_bundle(port=23000)))
    assert Store.load().data == before


def test_export_still_omits_ports():
    bundle.apply_import(bundle.dumps(_proton_bundle(port=23000)))
    out = bundle.export_bundle()
    assert "port" not in out["providers"]["protonvpn"]["channels"]["us_1"]
    assert "port" not in out["router"]


# ---- port provenance: only automatic ports may self-reallocate ------------------


def test_declared_ports_are_marked_explicit_everywhere():
    # bundle import
    bundle.apply_import(bundle.dumps(_proton_bundle(port=23000)))
    raw = Store.load().data["providers"]["protonvpn"]["channels"]["us_1"]
    assert raw["port_explicit"] is True
    # direct add / upsert with a declared port
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG), port=23500)
    raw = Store.load().data["providers"]["nordvpn"]["channels"][ch.id]
    assert raw["port_explicit"] is True
    store.upsert_channel("nordvpn", "up_1", "US", "", dict(WG), port=23600)
    raw = Store.load().data["providers"]["nordvpn"]["channels"]["up_1"]
    assert raw["port_explicit"] is True
    # automatic allocations carry no marker
    auto = store.add_channel("nordvpn", "DE", "", dict(WG))
    raw = Store.load().data["providers"]["nordvpn"]["channels"][auto.id]
    assert "port_explicit" not in raw


def test_stolen_explicit_port_is_held_not_moved():
    bundle.apply_import(bundle.dumps(_proton_bundle(port=23000)))
    store = Store.load()
    store.add_provider("nordvpn")
    auto = store.add_channel("nordvpn", "US", "", dict(WG))
    moved, held = Store.load().reallocate_channel_ports({23000, auto.port})
    # the automatic port recovers by moving; the declaration never moves
    assert [(p, c) for p, c, _old, _new in moved] == [("nordvpn", auto.id)]
    assert held == [("protonvpn", "us_1", 23000)]
    after = Store.load()
    assert after.get_channel("protonvpn", "us_1").port == 23000
    assert after.get_channel("nordvpn", auto.id).port != auto.port


def test_stolen_explicit_router_port_is_held():
    data = _proton_bundle()
    data["router"] = {"port": 24000}
    bundle.apply_import(bundle.dumps(data))
    moved, held = Store.load().reallocate_channel_ports({24000})
    assert moved == [] and held == [("router", "entrypoint", 24000)]
    assert Store.load().router["port"] == 24000


def test_engine_degrades_held_explicit_port_and_keeps_the_rest_alive():
    bundle.apply_import(bundle.dumps(_proton_bundle(port=23000)))
    store = Store.load()
    store.add_provider("nordvpn")
    other = store.add_channel("nordvpn", "US", "", dict(WG))

    eng = Engine(Store.load())
    assert eng._recover_stolen_ports(
        "start inbound/mixed[in-protonvpn-us_1]: listen tcp "
        "127.0.0.1:23000: bind: address already in use"
    )
    config, errors = eng._build_config()
    # the held channel is excluded (its port stays a contract, its traffic
    # fails closed) with an actionable error; everything else still builds
    assert "declared port 23000" in errors["protonvpn/us_1"]
    tags = {i["tag"] for i in config["inbounds"]}
    assert f"in-nordvpn-{other.id}" in tags
    assert "in-protonvpn-us_1" not in tags


def test_engine_degrades_held_router_port():
    data = _proton_bundle()
    data["router"] = {"port": 24000}
    bundle.apply_import(bundle.dumps(data))
    Store.load().ensure_router_port()

    eng = Engine(Store.load())
    assert eng._recover_stolen_ports(
        "start inbound/mixed[in-router]: listen tcp "
        "127.0.0.1:24000: bind: address already in use"
    )
    config, errors = eng._build_config()
    assert "declared router port 24000" in errors["router/entrypoint"]
    assert "in-router" not in {i["tag"] for i in config["inbounds"]}
    assert Store.load().router["port"] == 24000  # the contract did not move


def test_sync_converges_port_provenance():
    # a bundle that drops its port: declaration demotes the port to automatic
    # (sync converges provenance); the port number itself stays
    bundle.apply_sync(bundle.dumps(_proton_bundle(port=23000)))
    raw = Store.load().data["providers"]["protonvpn"]["channels"]["us_1"]
    assert raw["port_explicit"] is True

    summary = bundle.apply_sync(bundle.dumps(_proton_bundle()))
    assert summary["channels"]["updated"] == ["protonvpn/us_1"]
    raw = Store.load().data["providers"]["protonvpn"]["channels"]["us_1"]
    assert "port_explicit" not in raw
    assert raw["port"] == 23000  # the number is kept; only provenance changed


# ---- bundle: credential indirection -------------------------------------------


def _nord_bundle(credential: dict) -> dict:
    return {
        "kind": "alle-bundle",
        "bundle_version": 1,
        "providers": {"nordvpn": {"credential": credential, "channels": {}}},
    }


def test_credential_token_env_resolves(monkeypatch):
    monkeypatch.setenv("NORD_TOKEN", "tok-from-env")
    parsed = bundle.validate(_nord_bundle({"token_env": "NORD_TOKEN"}))
    assert parsed["providers"]["nordvpn"]["credential"] == {"token": "tok-from-env"}


def test_credential_token_env_missing_is_a_blocker(monkeypatch):
    monkeypatch.delenv("NORD_TOKEN", raising=False)
    with pytest.raises(bundle.BundleError, match="NORD_TOKEN.*not set"):
        bundle.validate(_nord_bundle({"token_env": "NORD_TOKEN"}))


def test_credential_token_file_resolves(tmp_path):
    secret = tmp_path / "nordvpn_token"
    secret.write_text("tok-from-file\n")
    parsed = bundle.validate(_nord_bundle({"token_file": str(secret)}))
    assert parsed["providers"]["nordvpn"]["credential"] == {"token": "tok-from-file"}


def test_credential_token_file_missing_is_a_blocker(tmp_path):
    with pytest.raises(bundle.BundleError, match="could not read"):
        bundle.validate(_nord_bundle({"token_file": str(tmp_path / "nope")}))


def test_credential_inline_and_env_together_are_rejected(monkeypatch):
    monkeypatch.setenv("NORD_TOKEN", "tok")
    with pytest.raises(bundle.BundleError, match="exactly one"):
        bundle.validate(_nord_bundle({"token": "inline", "token_env": "NORD_TOKEN"}))


def test_credential_inline_keeps_working():
    parsed = bundle.validate(_nord_bundle({"token": "tok-inline"}))
    assert parsed["providers"]["nordvpn"]["credential"] == {"token": "tok-inline"}


# ---- ALLE_SINGBOX override -----------------------------------------------------


def test_singbox_override_missing_path_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLE_SINGBOX", str(tmp_path / "missing"))
    with pytest.raises(singbox.SingBoxError, match="does not exist"):
        singbox.ensure_binary()


def test_singbox_override_wrong_bytes_error_never_downloads(monkeypatch, tmp_path):
    fake = tmp_path / "sing-box"
    fake.write_bytes(b"not sing-box")
    monkeypatch.setenv("ALLE_SINGBOX", str(fake))
    with pytest.raises(singbox.SingBoxError, match="not the pinned sing-box"):
        singbox.ensure_binary()
    assert fake.read_bytes() == b"not sing-box"  # untouched, no download over it


def test_singbox_override_verified_bytes_are_accepted(monkeypatch, tmp_path):
    fake = tmp_path / "sing-box"
    fake.write_bytes(b"pretend binary")
    monkeypatch.setenv("ALLE_SINGBOX", str(fake))
    key = singbox.host_platform()
    monkeypatch.setitem(
        singbox.SINGBOX_SHA256, key, hashlib.sha256(b"pretend binary").hexdigest()
    )
    assert singbox.ensure_binary() == fake
    assert singbox.bin_path() == fake  # version --singbox-path / setcap gate agree


# ---- container awareness --------------------------------------------------------


def test_in_container_defaults_off(monkeypatch):
    monkeypatch.setattr(runtime, "_MARKER_FILES", ("/nonexistent-marker",))
    assert not runtime.in_container()


def test_in_container_env_flag(monkeypatch):
    monkeypatch.setenv("ALLE_CONTAINER", "1")
    assert runtime.in_container()


def test_daemon_install_refuses_in_container(monkeypatch):
    monkeypatch.setenv("ALLE_CONTAINER", "1")
    with pytest.raises(daemonctl.DaemonCtlError, match="container"):
        daemonctl.install()
    with pytest.raises(daemonctl.DaemonCtlError, match="container"):
        daemonctl.uninstall()


def test_doomed_daemon_install_never_stops_the_running_daemon(monkeypatch):
    # Found live in the container smoke test: `alle daemon install` used to
    # stop the daemon (PID 1 in a container — the whole container) on its way
    # to the "no backend" error. The refusal must come first.
    from alle import daemon

    monkeypatch.setenv("ALLE_CONTAINER", "1")
    stopped = {}
    monkeypatch.setattr(daemon, "stop", lambda: stopped.setdefault("hit", True))
    with pytest.raises(service.ServiceError, match="container"):
        service.daemon_install()
    assert not stopped


def test_tun_hint_names_the_container_recipe(monkeypatch):
    monkeypatch.setenv("ALLE_CONTAINER", "1")
    hint = service._tun_privilege_hint()
    assert "--cap-add NET_ADMIN" in hint
    assert "/dev/net/tun" in hint
    assert "sudo alle helper install" not in hint
    assert "setcap cap_net_admin" not in hint


def test_tun_hint_unchanged_on_hosts(monkeypatch):
    monkeypatch.setattr(runtime, "_MARKER_FILES", ("/nonexistent-marker",))
    assert "--cap-add" not in service._tun_privilege_hint()


def test_process_uid_works_without_ps(monkeypatch):
    # Found live in the container smoke test: slim images ship no `ps`, so the
    # tun gate read a root daemon as "unknown uid" and refused. /proc must be
    # enough on Linux; either path must resolve our own process anywhere.
    import os

    assert service._process_uid(os.getpid()) == os.geteuid()


# ---- health ---------------------------------------------------------------------


def test_health_reports_down_on_a_fresh_state():
    result = service.health()
    assert result == {
        "ok": False,
        "daemon": False,
        "singbox": False,
        "channels": 0,
        "runtime": None,
    }


def test_health_cli_exit_code_and_json(capsys):
    from alle import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["health", "--json"])
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False


# ---- foreground run --------------------------------------------------------------


def test_cmd_run_marks_the_process_and_echoes_logs(monkeypatch):
    import os

    from alle import cli, daemon

    monkeypatch.delenv("ALLE_APPLIER", raising=False)
    monkeypatch.setattr(applog, "echo_stderr", False)
    called = {}
    monkeypatch.setattr(
        daemon,
        "run_applier",
        # foreground runs own their children (SIGTERM tears down sing-box)
        lambda own_children=False: called.setdefault("own", own_children),
    )
    try:
        cli.main(["run"])
        assert called["own"] is True
        assert applog.echo_stderr is True
        assert os.environ.get("ALLE_APPLIER") == "1"
    finally:
        # cmd_run writes the marker straight into os.environ (that is its job);
        # monkeypatch never saw it, so drop it here or it leaks into later tests.
        os.environ.pop("ALLE_APPLIER", None)


def test_applog_echo_stderr_tees_lines(monkeypatch, capsys):
    monkeypatch.setattr(applog, "echo_stderr", True)
    applog.log("hello from the foreground")
    assert "hello from the foreground" in capsys.readouterr().err
    assert "hello from the foreground" in applog.tail()
