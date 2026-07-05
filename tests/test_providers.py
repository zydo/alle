"""Provider registry, the per-provider auth schema, kinds, and credential masking."""

from __future__ import annotations

from email.message import Message
import urllib.error

import pytest

from alle import credentials, providers


def test_known_enumerates_every_provider():
    assert set(providers.known()) == {"nordvpn", "protonvpn"}


def test_supported_is_only_functional_providers():
    assert providers.supported() == ["nordvpn"]


def test_kinds_and_functional_flags():
    assert providers.kind("nordvpn") == "token" and providers.is_functional("nordvpn")
    assert providers.kind("protonvpn") == "config" and not providers.is_functional(
        "protonvpn"
    )


def test_config_provider_has_howto():
    assert "config" in providers.config_help("protonvpn").lower()


def test_auth_fields_describe_the_login_form():
    # token providers declare their login form; config providers have none
    assert [f.key for f in providers.auth_fields("nordvpn")] == ["token"]
    assert providers.auth_fields("protonvpn") == []


def test_auth_help_points_at_the_provider_console():
    help_text, url = providers.auth_help("nordvpn")
    assert "nordaccount" in help_text.lower()
    assert url.startswith("https://")


def test_match_accepts_any_known_provider_by_key_or_brand():
    assert providers.match("nordvpn") == "nordvpn"
    assert providers.match("NordVPN") == "nordvpn"
    assert providers.match("  nordvpn  ") == "nordvpn"
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
    assert providers.preview(
        "nordvpn", {"token": "nGx4verylongtokena91k"}
    ) == credentials.mask("nGx4verylongtokena91k")


def test_display_uses_formal_brand_names():
    assert providers.display_name("nordvpn") == "NordVPN"
    assert providers.display_name("protonvpn") == "Proton VPN"


def test_nord_server_parsing():
    server = {
        "hostname": "us9869.nordvpn.com",
        "ips": [{"ip": {"ip": "1.2.3.4"}}],
        "technologies": [
            {"identifier": "openvpn_udp", "metadata": []},
            {
                "identifier": "wireguard_udp",
                "metadata": [{"name": "public_key", "value": "PUB="}],
            },
        ],
    }
    assert providers._nord_pubkey(server) == "PUB="


def test_nordvpn_derive_key_errors(monkeypatch):
    with pytest.raises(providers.ProviderError, match="missing"):
        providers.nordvpn_derive_key({})

    def rejected(*_args, **_kwargs):
        raise urllib.error.HTTPError("url", 401, "Unauthorized", Message(), None)

    monkeypatch.setattr(providers, "_get_json", rejected)
    with pytest.raises(providers.ProviderError, match="token rejected"):
        providers.nordvpn_derive_key({"token": "bad"})

    monkeypatch.setattr(providers, "_get_json", lambda *_a, **_k: {})
    with pytest.raises(providers.ProviderError, match="nordlynx_private_key"):
        providers.nordvpn_derive_key({"token": "ok"})


def test_nordvpn_derive_key_success_sets_basic_auth(monkeypatch):
    seen = {}

    def fake_get_json(url, headers=None, timeout=30):
        seen["url"] = url
        seen["headers"] = headers
        seen["timeout"] = timeout
        return {"nordlynx_private_key": "PRIVATE="}

    monkeypatch.setattr(providers, "_get_json", fake_get_json)

    assert providers.nordvpn_derive_key({"token": "tok"}) == "PRIVATE="
    assert seen["url"].endswith("/users/services/credentials")
    assert seen["headers"]["Authorization"].startswith("Basic ")


def test_nord_locations_cache_and_forget(monkeypatch):
    calls = []

    def fake_get_json(_url):
        calls.append("fetch")
        return [{"id": 1, "name": "US", "cities": [{"id": 10, "name": "Seattle"}]}]

    providers.forget_nord_countries()
    monkeypatch.setattr(providers, "_get_json", fake_get_json)

    assert providers.nordvpn_locations() == {"US": ["Seattle"]}
    assert providers.nordvpn_locations() == {"US": ["Seattle"]}
    assert calls == ["fetch"]
    providers.forget_nord_countries()
    assert providers.nordvpn_locations() == {"US": ["Seattle"]}
    assert calls == ["fetch", "fetch"]


def test_nord_locations_cache_expires_after_ttl(monkeypatch):
    calls = []

    def fake_get_json(_url):
        calls.append("fetch")
        return [{"id": 1, "name": "US", "cities": []}]

    providers.forget_nord_countries()
    monkeypatch.setattr(providers, "_get_json", fake_get_json)
    providers.nordvpn_locations()
    assert calls == ["fetch"]

    # age the cached entry past the TTL: the next call re-fetches
    fetched_at, data = providers._nord_countries_cache
    providers._nord_countries_cache = (fetched_at - providers.NORD_CACHE_TTL - 1, data)
    providers.nordvpn_locations()
    assert calls == ["fetch", "fetch"]


def test_nord_locations_stale_cache_beats_refresh_failure(monkeypatch):
    providers.forget_nord_countries()
    monkeypatch.setattr(
        providers,
        "_get_json",
        lambda _url: [{"id": 1, "name": "US", "cities": []}],
    )
    providers.nordvpn_locations()

    # expire the cache, then make the refresh fail: stale data is served
    fetched_at, data = providers._nord_countries_cache
    providers._nord_countries_cache = (fetched_at - providers.NORD_CACHE_TTL - 1, data)
    monkeypatch.setattr(
        providers,
        "_get_json",
        lambda _url: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    assert providers.nordvpn_locations() == {"US": []}


def test_nord_locations_fetch_error(monkeypatch):
    providers.forget_nord_countries()
    monkeypatch.setattr(
        providers,
        "_get_json",
        lambda _url: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )

    with pytest.raises(providers.ProviderError, match="could not fetch"):
        providers.nordvpn_locations()


def test_nord_ids_and_pubkey_errors(monkeypatch):
    providers.forget_nord_countries()
    monkeypatch.setattr(
        providers,
        "_get_json",
        lambda _url: [
            {"id": 1, "name": "US", "cities": [{"id": 10, "name": "Seattle"}]}
        ],
    )

    assert providers._nord_ids("us", "") == (1, None)
    assert providers._nord_ids("US", "seattle") == (1, 10)
    with pytest.raises(providers.ProviderError, match="city"):
        providers._nord_ids("US", "Austin")
    with pytest.raises(providers.ProviderError, match="country"):
        providers._nord_ids("Atlantis", "")

    with pytest.raises(providers.ProviderError, match="public key"):
        providers._nord_pubkey({"hostname": "bad", "technologies": []})


def test_nordvpn_resolve_success_and_errors(monkeypatch):
    providers.forget_nord_countries()

    def fake_get_json(url, **_kwargs):
        if url.endswith("/servers/countries"):
            return [{"id": 1, "name": "US", "cities": [{"id": 10, "name": "Seattle"}]}]
        return [
            {
                "hostname": "us.example",
                "ips": [{"ip": {"ip": "1.2.3.4"}}],
                "technologies": [
                    {
                        "identifier": "wireguard_udp",
                        "metadata": [{"name": "public_key", "value": "PUB="}],
                    }
                ],
            }
        ]

    monkeypatch.setattr(providers, "_get_json", fake_get_json)
    assert providers.nordvpn_resolve("US", "Seattle") == {
        "host": "1.2.3.4",
        "port": providers.WG_PORT,
        "public_key": "PUB=",
        "hostname": "us.example",
    }

    monkeypatch.setattr(providers, "_get_json", lambda *_a, **_k: [])
    with pytest.raises(providers.ProviderError, match="no WireGuard server"):
        providers.nordvpn_resolve("US", "")

    def no_host(url, **_kwargs):
        if url.endswith("/servers/countries"):
            return [{"id": 1, "name": "US", "cities": []}]
        return [{"hostname": "empty", "technologies": []}]

    providers.forget_nord_countries()
    monkeypatch.setattr(providers, "_get_json", no_host)
    with pytest.raises(providers.ProviderError, match="no usable IP"):
        providers.nordvpn_resolve("US", "")


def test_provider_wg_builds_wireguard_shape(monkeypatch):
    monkeypatch.setattr(providers.credentials, "get", lambda _p: {"token": "tok"})
    monkeypatch.setitem(
        providers.PROVIDERS,
        "demo",
        {
            "derive_key": lambda _creds: "PRIVATE=",
            "resolve": lambda country, city: {
                "public_key": f"PUB-{country}-{city}",
                "host": "1.2.3.4",
                "port": 51820,
            },
            "wg_address": ["10.0.0.2/32"],
        },
    )

    wg = providers.provider_wg("demo", "US", "Seattle")

    assert wg["private_key"] == "PRIVATE="
    assert wg["address"] == ["10.0.0.2/32"]
    assert wg["peer"]["public_key"] == "PUB-US-Seattle"


def test_provider_wg_rejects_nonfunctional_and_missing_credentials(monkeypatch):
    with pytest.raises(providers.ProviderError, match="cannot resolve"):
        providers.provider_wg("protonvpn", "US")

    monkeypatch.setattr(providers.credentials, "get", lambda _p: None)
    with pytest.raises(providers.ProviderError, match="not authenticated"):
        providers.provider_wg("nordvpn", "US")
