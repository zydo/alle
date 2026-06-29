"""The applier's change-detection: the config signature is what triggers a
reconcile, so it must move on any channel add/remove/edit and stay put for
probe-only writes."""

from __future__ import annotations

from alle.state import Store, _read_raw, config_signature

WG = {
    "private_key": "PRIV=",
    "address": ["10.5.0.2/32"],
    "peer": {
        "public_key": "PUB=",
        "endpoint_host": "se1.example.com",
        "endpoint_port": 51820,
        "preshared_key": None,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "keepalive": 25,
    },
}


def _sig():
    return config_signature(_read_raw())


def test_signature_tracks_channel_lifecycle():
    empty = _sig()
    store = Store.load()
    store.add_provider("nordvpn")
    assert _sig() == empty  # an empty provider has no config-relevant content

    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    after_add = _sig()
    assert after_add != empty

    store.remove_channel("nordvpn", ch.id)
    assert _sig() == empty  # removing it returns to the empty signature


def test_signature_ignores_probe_writes():
    store = Store.load()
    store.add_provider("nordvpn")
    ch = store.add_channel("nordvpn", "US", "", dict(WG))
    before = _sig()
    store.set_probe("nordvpn", ch.id, {"ok": True, "ip": "1.2.3.4", "at": 1})
    assert _sig() == before
