"""Parsing, validation, canonicalization and alle metadata headers for
WireGuard .conf files."""

from __future__ import annotations

import pytest

from alle import wgconf

SAMPLE = """
# WireGuard configuration
[Interface]
PrivateKey = aMf0PRIVATEkeyVALUE0000000000000000000000000=
Address = 10.64.0.2/32,fc00:bbbb::2/128
DNS = 10.64.0.1

[Peer]
PublicKey = bNg1PUBLICkeyVALUE00000000000000000000000000=
PresharedKey = cPh2PRESHAREDkey0000000000000000000000000000=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 185.65.135.99:51820
PersistentKeepalive = 25
"""


def test_parse_extracts_interface_and_peer():
    p = wgconf.parse(SAMPLE)
    assert p["private_key"].startswith("aMf0")
    assert p["address"] == ["10.64.0.2/32", "fc00:bbbb::2/128"]
    peer = p["peer"]
    assert peer["public_key"].startswith("bNg1")
    assert peer["preshared_key"].startswith("cPh2")
    assert peer["endpoint_host"] == "185.65.135.99"
    assert peer["endpoint_port"] == 51820
    assert peer["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert peer["keepalive"] == 25


def test_parse_requires_the_essential_fields():
    with pytest.raises(wgconf.ConfError) as e:
        wgconf.parse("[Interface]\nAddress = 10.0.0.2/32\n")
    msg = str(e.value)
    assert "PrivateKey" in msg and "PublicKey" in msg and "Endpoint" in msg


def test_defaults_when_optional_fields_absent():
    p = wgconf.parse(
        "[Interface]\nPrivateKey = K\nAddress = 10.0.0.2/32\n"
        "[Peer]\nPublicKey = P\nEndpoint = host.example.com:51820\n"
    )
    assert p["peer"]["preshared_key"] is None
    assert p["peer"]["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert p["peer"]["keepalive"] == wgconf.WG_KEEPALIVE
    assert p["peer"]["endpoint_host"] == "host.example.com"  # hostname preserved


def test_ipv6_endpoint_is_split_correctly():
    p = wgconf.parse(
        "[Interface]\nPrivateKey = K\nAddress = 10.0.0.2/32\n"
        "[Peer]\nPublicKey = P\nEndpoint = [2001:db8::1]:51820\n"
    )
    assert p["peer"]["endpoint_host"] == "2001:db8::1"
    assert p["peer"]["endpoint_port"] == 51820


def test_canonical_masked_hides_secrets():
    masked = wgconf.canonical(wgconf.parse(SAMPLE), masked=True)
    assert "aMf0PRIVATEkey" not in masked  # private key not shown in full
    assert "cPh2PRESHAREDkey" not in masked  # nor the preshared key
    assert "PublicKey = bNg1PUBLICkey" in masked  # public key is not a secret
    assert "Endpoint = 185.65.135.99:51820" in masked
    # the full canonical form keeps everything for actual use
    assert "aMf0PRIVATEkeyVALUE" in wgconf.canonical(wgconf.parse(SAMPLE))


def test_metadata_headers_round_trip():
    meta = {
        "provider": "nordvpn",
        "country": "United States",
        "city": "San Francisco",
        "port": 8889,
        "enabled": True,
        "kind": "provider",
        "name": "",  # empty values are omitted
    }
    block = wgconf.meta_block(meta)
    assert "# alle-provider: nordvpn" in block
    assert "# alle-city: San Francisco" in block  # spaces need no quoting
    assert "# alle-enabled: true" in block
    assert "name" not in block  # empty value dropped

    # headers prefix a real .conf and don't disturb parsing of the body
    full = block + wgconf.canonical(wgconf.parse(SAMPLE))
    read = wgconf.read_meta(full)
    assert read["provider"] == "nordvpn"
    assert read["country"] == "United States"
    assert read["port"] == "8889"
    assert read["enabled"] == "true"
    assert wgconf.parse(full)["peer"]["endpoint_host"] == "185.65.135.99"
