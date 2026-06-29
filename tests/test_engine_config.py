"""Engine config assembly + reconcile behaviour, with the sing-box process
stubbed out (pure logic, no downloads, no tunnels)."""

from __future__ import annotations

from alle.engine import Engine
from alle.state import Store

WG = {
    "private_key": "PRIV=",
    "address": ["10.5.0.2/32"],
    "peer": {
        "public_key": "PUB=",
        "endpoint_host": "1.2.3.4",
        "endpoint_port": 51820,
        "preshared_key": None,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "keepalive": 25,
    },
}


def _store(*specs):
    """specs: (provider, id, port, country, city, wg) tuples."""
    data = {"version": 1, "providers": {}}
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


def test_each_channel_becomes_an_inbound_and_endpoint():
    store = _store(
        ("nordvpn", "us_1", 8888, "US", "", dict(WG)),
        ("nordvpn", "uk_1", 8889, "UK", "", dict(WG)),
    )
    config, errors = Engine(store)._build_config()
    assert errors == {}
    assert [i["tag"] for i in config["inbounds"]] == ["in-nordvpn-uk_1", "in-nordvpn-us_1"]
    assert [e["tag"] for e in config["endpoints"]] == ["out-nordvpn-uk_1", "out-nordvpn-us_1"]
    assert {"inbound": ["in-nordvpn-us_1"], "outbound": "out-nordvpn-us_1"} in config["route"][
        "rules"
    ]
    assert config["route"]["final"] == "direct"


def test_inbound_and_endpoint_shape():
    config, _ = Engine(_store(("nordvpn", "us_1", 9000, "US", "", dict(WG))))._build_config()
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


def test_malformed_channel_omitted_and_reported():
    store = _store(("nordvpn", "us_1", 8888, "US", "", {}))  # no usable WireGuard config
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
        return True

    def is_running(self):
        return self._running


def test_reconcile_pushes_config():
    eng = Engine(_store(("nordvpn", "us_1", 8888, "US", "", dict(WG))))
    eng.runner = _FakeRunner()
    assert eng.reconcile() == {}
    assert eng.runner.applied
    assert [i["tag"] for i in eng.runner.applied[0]["inbounds"]] == ["in-nordvpn-us_1"]


def test_probe_all_records_stopped_when_not_running():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    eng = Engine(store)
    eng.runner = _FakeRunner()  # not running
    out = eng.probe_all()
    assert out[f"nordvpn/{ch.id}"]["error"] == "stopped"
    # persisted to state
    assert Store.load().get_channel("nordvpn", ch.id).probe["error"] == "stopped"
