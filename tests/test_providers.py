"""Provider registry, the per-provider auth schema, kinds, and credential masking."""

from __future__ import annotations

from alle import credentials, providers


def test_known_enumerates_every_provider():
    assert set(providers.known()) == {"nordvpn", "mullvad", "ivpn", "pia", "protonvpn"}


def test_supported_is_only_functional_providers():
    assert providers.supported() == ["nordvpn"]


def test_kinds_and_functional_flags():
    assert providers.kind("nordvpn") == "token" and providers.is_functional("nordvpn")
    assert providers.kind("mullvad") == "token" and not providers.is_functional("mullvad")
    assert providers.kind("protonvpn") == "config" and not providers.is_functional("protonvpn")


def test_config_provider_has_howto():
    assert "config" in providers.config_help("protonvpn").lower()


def test_auth_fields_describe_the_login_form():
    assert [f.key for f in providers.auth_fields("nordvpn")] == ["token"]
    # PIA needs two fields and only the password is secret
    pia = {f.key: f for f in providers.auth_fields("pia")}
    assert set(pia) == {"username", "password"}
    assert pia["username"].secret is False
    assert pia["password"].secret is True
    # config providers have no login form
    assert providers.auth_fields("protonvpn") == []


def test_auth_help_points_at_the_provider_console():
    help_text, url = providers.auth_help("nordvpn")
    assert "nordaccount" in help_text.lower()
    assert url.startswith("https://")


def test_match_accepts_any_known_provider_by_key_or_brand():
    assert providers.match("nordvpn") == "nordvpn"
    assert providers.match("NordVPN") == "nordvpn"
    assert providers.match("  nordvpn  ") == "nordvpn"
    assert providers.match("Mullvad") == "mullvad"  # planned providers resolve too
    assert providers.match("ProtonVPN") == "protonvpn"
    assert providers.match("nope") is None


def test_mask_keeps_only_the_ends():
    secret = "nGx4verylongtokena91k"
    masked = credentials.mask(secret)
    assert masked.startswith("nGx4")
    assert masked.endswith("a91k")
    assert len(masked) == len(secret)
    assert set(masked[4:-4]) == {"*"}  # everything between the ends is starred
    # too short to keep plaintext on both ends -> fully starred
    assert credentials.mask("short") == "*****"
    assert credentials.mask("") == ""


def test_preview_masks_the_primary_secret():
    assert providers.preview("nordvpn", {"token": "nGx4verylongtokena91k"}) == credentials.mask(
        "nGx4verylongtokena91k"
    )
    # PIA's primary secret is the password, not the username
    assert providers.preview("pia", {"username": "p123", "password": "longsecretvalue"}) == (
        credentials.mask("longsecretvalue")
    )


def test_display_uses_formal_brand_names():
    assert providers.display_name("nordvpn") == "NordVPN"
    assert providers.display_name("ivpn") == "IVPN"
    assert providers.display_name("pia") == "Private Internet Access"
    assert providers.display_name("protonvpn") == "ProtonVPN"


def test_nord_server_parsing():
    server = {
        "hostname": "us9869.nordvpn.com",
        "ips": [{"ip": {"ip": "1.2.3.4"}}],
        "technologies": [
            {"identifier": "openvpn_udp", "metadata": []},
            {"identifier": "wireguard_udp", "metadata": [{"name": "public_key", "value": "PUB="}]},
        ],
    }
    assert providers._nord_pubkey(server) == "PUB="
