"""Parsing, validation, canonicalization and alle metadata headers for
WireGuard .conf files."""

from __future__ import annotations

import pytest

from alle import wgconf

# Structurally valid WireGuard keys (32 bytes base64) with readable prefixes.
PRIV = "aMf0PRIVATEkeyVALUE" + "A" * 24 + "="
PUB = "bNg1PUBLICkeyVALUE" + "A" * 25 + "="
PSK = "cPh2PRESHAREDkey" + "A" * 27 + "="
KEY = "A" * 43 + "="  # an anonymous valid key for shape-only tests

SAMPLE = f"""
# WireGuard configuration
[Interface]
PrivateKey = {PRIV}
Address = 10.64.0.2/32,fc00:bbbb::2/128
DNS = 10.64.0.1

[Peer]
PublicKey = {PUB}
PresharedKey = {PSK}
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
        f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
        f"[Peer]\nPublicKey = {KEY}\nEndpoint = host.example.com:51820\n"
    )
    assert p["peer"]["preshared_key"] is None
    assert p["peer"]["allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert p["peer"]["keepalive"] == wgconf.WG_KEEPALIVE
    assert p["peer"]["endpoint_host"] == "host.example.com"  # hostname preserved


def test_ipv6_endpoint_is_split_correctly():
    p = wgconf.parse(
        f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
        f"[Peer]\nPublicKey = {KEY}\nEndpoint = [2001:db8::1]:51820\n"
    )
    assert p["peer"]["endpoint_host"] == "2001:db8::1"
    assert p["peer"]["endpoint_port"] == 51820


def test_malformed_keys_are_rejected_with_the_field_named():
    for bad in ("shortkey", KEY + "=", "!" * 44):  # truncated, overpadded, not base64
        with pytest.raises(wgconf.ConfError, match=r"\[Interface\] PrivateKey"):
            wgconf.parse(
                f"[Interface]\nPrivateKey = {bad}\nAddress = 10.0.0.2/32\n"
                f"[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\n"
            )
    with pytest.raises(wgconf.ConfError, match=r"\[Peer\] PresharedKey"):
        wgconf.parse(
            f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
            f"[Peer]\nPublicKey = {KEY}\nPresharedKey = nope\n"
            "Endpoint = h.example.com:51820\n"
        )


def test_out_of_range_endpoint_port_is_rejected():
    for port in ("0", "65536", "99999"):
        with pytest.raises(wgconf.ConfError, match="not host:port"):
            wgconf.parse(
                f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
                f"[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:{port}\n"
            )


def test_canonical_masked_hides_secrets():
    masked = wgconf.canonical(wgconf.parse(SAMPLE), masked=True)
    assert "aMf0PRIVATEkey" not in masked  # private key not shown in full
    assert "cPh2PRESHAREDkey" not in masked  # nor the preshared key
    assert "PublicKey = bNg1PUBLICkey" in masked  # public key is not a secret
    assert "Endpoint = 185.65.135.99:51820" in masked
    # the full canonical form keeps everything for actual use
    assert "aMf0PRIVATEkeyVALUE" in wgconf.canonical(wgconf.parse(SAMPLE))


# ---- strict structural + field validation (Phase 5.6) -------------------------


def _iface(**extra):
    """A minimal valid [Interface] + [Peer] body, with extra lines appended."""
    lines = [
        f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32",
        f"\n[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820",
    ]
    return "".join(part + "\n" for part in lines) + "".join(
        f"{k}\n" for k in extra.values()
    )


def test_second_peer_section_is_rejected_not_merged():
    # A second [Peer] used to silently override the first — a hybrid of two
    # servers. It must now fail, naming both section lines.
    text = (
        _iface() + "\n[Peer]\nPublicKey = " + "B" * 43 + "=\nEndpoint = 9.9.9.9:51820\n"
    )
    with pytest.raises(wgconf.ConfError, match="duplicate \\[Peer\\]"):
        wgconf.parse(text)


def test_duplicate_interface_section_is_rejected():
    text = f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n[Interface]\nPrivateKey = {KEY}\n[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\n"
    with pytest.raises(wgconf.ConfError, match="duplicate \\[Interface\\]"):
        wgconf.parse(text)


def test_duplicate_scalar_key_is_rejected():
    text = (
        f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
        f"PrivateKey = {'B' * 43}=\n[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\n"
    )
    with pytest.raises(wgconf.ConfError, match="duplicate privatekey"):
        wgconf.parse(text)


def test_unknown_section_is_rejected():
    text = f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n[WireGuardPeer]\nfoo = bar\n[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\n"
    with pytest.raises(wgconf.ConfError, match=r"unsupported section"):
        wgconf.parse(text)


def test_malformed_line_and_key_before_section_are_reported():
    text = (
        "PrivateKey = orphan\n[Interface]\nnot a key value\nPrivateKey = "
        + KEY
        + "\nAddress = 10.0.0.2/32\n[Peer]\nPublicKey = "
        + KEY
        + "\nEndpoint = h.example.com:51820\n"
    )
    with pytest.raises(wgconf.ConfError) as exc:
        wgconf.parse(text)
    msg = str(exc.value)
    assert "before any" in msg  # orphan key
    assert "not a 'Key = value'" in msg  # malformed line


def test_list_keys_append_instead_of_erroring():
    # Address / AllowedIPs / DNS are wg-quick lists — a repeated line appends.
    p = wgconf.parse(
        f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\nAddress = fd00::2/128\nDNS = 1.1.1.1\n"
        f"[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\nAllowedIPs = 0.0.0.0/0\nAllowedIPs = ::/0\n"
    )
    assert p["address"] == ["10.0.0.2/32", "fd00::2/128"]
    assert p["peer"]["allowed_ips"] == ["0.0.0.0/0", "::/0"]


def test_bad_cidr_in_allowed_ips_is_rejected():
    with pytest.raises(
        wgconf.ConfError, match=r"AllowedIPs '999\.0\.0\.0/8' is not a CIDR"
    ):
        wgconf.parse(
            f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
            f"[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\nAllowedIPs = 999.0.0.0/8\n"
        )


def test_bad_address_is_rejected():
    with pytest.raises(
        wgconf.ConfError, match=r"Address '10.0.0' is not an IP interface address"
    ):
        wgconf.parse(
            f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0\n"
            f"[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\n"
        )


def test_bad_keepalive_is_rejected_not_silently_defaulted():
    with pytest.raises(wgconf.ConfError, match="PersistentKeepalive 'forever'"):
        wgconf.parse(
            f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
            f"[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\nPersistentKeepalive = forever\n"
        )
    # and an out-of-range value likewise (never silently clamp to default)
    with pytest.raises(wgconf.ConfError, match="must be a number of seconds"):
        wgconf.parse(
            f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
            f"[Peer]\nPublicKey = {KEY}\nEndpoint = h.example.com:51820\nPersistentKeepalive = 99999\n"
        )


def test_invalid_hostname_endpoint_is_rejected():
    for host in ("has space", "under_score", "-leading", "trailing-"):
        with pytest.raises(wgconf.ConfError, match="is not an IP address or hostname"):
            wgconf.parse(
                f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
                f"[Peer]\nPublicKey = {KEY}\nEndpoint = {host}:51820\n"
            )


def test_valid_hostname_and_ipv6_endpoints_are_accepted():
    p = wgconf.parse(
        f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
        f"[Peer]\nPublicKey = {KEY}\nEndpoint = se1.example.com:51820\n"
    )
    assert p["peer"]["endpoint_host"] == "se1.example.com"
    p = wgconf.parse(
        f"[Interface]\nPrivateKey = {KEY}\nAddress = 10.0.0.2/32\n"
        f"[Peer]\nPublicKey = {KEY}\nEndpoint = [2606:4700::1]:51820\n"
    )
    assert p["peer"]["endpoint_host"] == "2606:4700::1"
    assert p["peer"]["endpoint_port"] == 51820


def test_all_problems_aggregated_with_line_numbers():
    # A file with several independent problems reports them all at once, each
    # annotated with its source line — not one-error-per-attempt.
    text = (
        "[Interface]\n"  # 1
        "PrivateKey = badkey\n"  # 2 — bad key
        "Address = not-an-ip\n"  # 3 — bad address
        "PrivateKey = dup\n"  # 4 — duplicate
        "[Peer]\n"  # 5
        "PublicKey = also-bad\n"  # 6 — bad key
        "Endpoint = no-port\n"  # 7 — not host:port
        "PersistentKeepalive = nope\n"  # 8 — bad keepalive
    )
    with pytest.raises(wgconf.ConfError) as exc:
        wgconf.parse(text)
    msg = str(exc.value)
    for needle in (
        "line 2",  # PrivateKey
        "line 3",  # Address
        "line 4",  # duplicate
        "line 6",  # PublicKey
        "line 7",  # endpoint
        "line 8",  # keepalive
        "PrivateKey",  # the field/message content
        "no-port",  # the endpoint value
        "PersistentKeepalive",
    ):
        assert needle in msg, f"expected {needle!r} in aggregated error"


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
