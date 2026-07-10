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
    with pytest.raises(providers.ProviderAuthError, match="missing"):
        providers.nordvpn_derive_key({})

    def rejected(*_args, **_kwargs):
        raise providers.ProviderAPIError("HTTP 401", status=401)

    monkeypatch.setattr(providers, "_get_json", rejected)
    with pytest.raises(providers.ProviderAuthError, match="token rejected"):
        providers.nordvpn_derive_key({"token": "bad"})

    def server_error(*_args, **_kwargs):
        raise providers.ProviderAPIError("HTTP 500", status=500)

    # a 5xx is the API's problem, not the credential's: stays retryable
    monkeypatch.setattr(providers, "_get_json", server_error)
    with pytest.raises(providers.ProviderAPIError, match="HTTP 500"):
        providers.nordvpn_derive_key({"token": "ok"})

    monkeypatch.setattr(providers, "_get_json", lambda *_a, **_k: {})
    with pytest.raises(providers.ProviderAPIError, match="nordlynx_private_key"):
        providers.nordvpn_derive_key({"token": "ok"})

    # a payload that isn't even a mapping must not crash with AttributeError
    monkeypatch.setattr(providers, "_get_json", lambda *_a, **_k: ["nonsense"])
    with pytest.raises(providers.ProviderAPIError, match="nordlynx_private_key"):
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


class _Resp:
    """A minimal urlopen response: context manager + bounded read()."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_get_json_normalizes_every_transport_failure(monkeypatch):
    def refused(_req, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", refused)
    with pytest.raises(providers.ProviderUnreachableError, match="could not reach"):
        providers._get_json("https://api.example/x")

    def http_503(_req, timeout=0):
        raise urllib.error.HTTPError("url", 503, "unavailable", Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", http_503)
    with pytest.raises(providers.ProviderAPIError, match="HTTP 503") as exc:
        providers._get_json("https://api.example/x")
    assert exc.value.status == 503

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda _req, timeout=0: _Resp(b"{not json")
    )
    with pytest.raises(providers.ProviderAPIError, match="invalid JSON"):
        providers._get_json("https://api.example/x")


def test_get_json_bounds_the_response_size(monkeypatch):
    huge = b'"' + b"x" * (providers.MAX_RESPONSE_BYTES + 10) + b'"'
    monkeypatch.setattr("urllib.request.urlopen", lambda _req, timeout=0: _Resp(huge))
    with pytest.raises(providers.ProviderAPIError, match="exceeds"):
        providers._get_json("https://api.example/x")


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
    cached = providers._nord_countries_cache
    assert cached is not None
    fetched_at, data = cached
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
    cached = providers._nord_countries_cache
    assert cached is not None
    fetched_at, data = cached
    providers._nord_countries_cache = (fetched_at - providers.NORD_CACHE_TTL - 1, data)
    monkeypatch.setattr(
        providers,
        "_get_json",
        lambda _url: (_ for _ in ()).throw(
            providers.ProviderUnreachableError("offline")
        ),
    )
    assert providers.nordvpn_locations() == {"US": []}


def test_nord_locations_fetch_error_keeps_the_typed_class(monkeypatch):
    providers.forget_nord_countries()
    monkeypatch.setattr(
        providers,
        "_get_json",
        lambda _url: (_ for _ in ()).throw(
            providers.ProviderUnreachableError("offline")
        ),
    )

    # context is added but the class survives, so callers can still tell
    # "network down" from "API answered garbage"
    with pytest.raises(providers.ProviderUnreachableError, match="could not fetch"):
        providers.nordvpn_locations()


def test_nord_country_list_schema_is_validated(monkeypatch):
    providers.forget_nord_countries()
    monkeypatch.setattr(providers, "_get_json", lambda _url: {"not": "a list"})
    with pytest.raises(providers.ProviderAPIError, match="not a list"):
        providers.nordvpn_locations()

    providers.forget_nord_countries()
    monkeypatch.setattr(
        providers,
        "_get_json",
        lambda _url: [{"id": "one", "name": "US", "cities": []}],  # id not an int
    )
    with pytest.raises(providers.ProviderAPIError, match="unexpected shape"):
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
