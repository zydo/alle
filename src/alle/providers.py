"""VPN provider registry.

A provider is described by its **kind**, which decides how channels get their
WireGuard parameters:

* ``token`` — the provider has an API. You add the provider once with a token/login
  (``alle providers add <name>``); thereafter ``alle channels add <name>
  --country …`` resolves a concrete server from the API. NordVPN is the token
  provider and is wired end-to-end.
* ``config`` — portal-only providers (e.g. ProtonVPN) that hand out a WireGuard
  ``.conf``. There is no token; you add the provider so channels can be imported
  under it via ``channels add <name> --config <file>`` (Phase 1 in progress).

Everything the engine needs for a functional provider — derive the account key,
list locations, resolve a location to a peer — comes straight from the provider's
own API, which is fresher than any bundled server database.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from alle import credentials
from alle.credentials import mask

NORD_API = "https://api.nordvpn.com/v1"
WG_PORT = 51820  # NordLynx / standard WireGuard UDP port


class ProviderError(Exception):
    """Raised when a provider rejects credentials or returns nothing usable."""


class ProviderAuthError(ProviderError):
    """A credential problem retrying can never fix (missing/rejected token).

    Kept as a distinct *type* so auto-reconnect can give up immediately on
    auth failures without pattern-matching words in error messages — which
    would misclassify transient errors that happen to contain the same words.
    """


def _get_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "alle/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---- NordVPN ---------------------------------------------------------------

# Client interface address for NordLynx; the same for every server/account.
NORDVPN_WG_ADDRESS = ["10.5.0.2/32"]  # noqa: S1313

# In-process country/city cache as (fetched_at_monotonic, data). Time-bounded
# so a long-lived process (the daemon reconnecting channels weeks later, a
# future Web UI) doesn't pin the list it fetched at startup forever; the
# on-disk cache in locations.py has its own, longer expiry.
NORD_CACHE_TTL = 3600.0
_nord_countries_cache: tuple[float, list[dict]] | None = None


def nordvpn_derive_key(creds: dict) -> str:
    """Exchange a NordVPN access token for the account's NordLynx private key."""
    token = (creds.get("token") or "").strip()
    if not token:
        raise ProviderAuthError("nordvpn access token is missing.")
    auth = base64.b64encode(f"token:{token}".encode()).decode()
    try:
        data = _get_json(
            f"{NORD_API}/users/services/credentials",
            headers={"Authorization": f"Basic {auth}", "User-Agent": "alle/1"},
        )
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ProviderAuthError(
                f"nordvpn token rejected by API (HTTP {e.code}). "
                "Generate a fresh token at my.nordaccount.com."
            ) from e
        # e.g. 5xx: the API is unhappy, not the credential — retryable
        raise ProviderError(f"nordvpn credentials API failed (HTTP {e.code}).") from e
    key = data.get("nordlynx_private_key")
    if not key:
        raise ProviderError("nordvpn API did not return nordlynx_private_key.")
    return key


def _nord_countries() -> list[dict]:
    global _nord_countries_cache
    cached = _nord_countries_cache
    if cached is not None and time.monotonic() - cached[0] < NORD_CACHE_TTL:
        return cached[1]
    try:
        countries = _get_json(f"{NORD_API}/servers/countries")
    except (urllib.error.URLError, ValueError) as e:
        if cached is not None:
            return cached[1]  # refresh failed: stale beats failing (reconnect path)
        raise ProviderError(f"could not fetch nordvpn country list: {e}") from e
    _nord_countries_cache = (time.monotonic(), countries)
    return countries


def nordvpn_locations() -> dict[str, list[str]]:
    """Country -> sorted cities, exactly as the NordVPN API reports them today."""
    out: dict[str, list[str]] = {}
    for c in _nord_countries():
        out[c["name"]] = sorted(ci["name"] for ci in c.get("cities", []))
    return out


def _nord_ids(country: str, city: str) -> tuple[int, int | None]:
    for c in _nord_countries():
        if c["name"].lower() == country.lower():
            if city:
                for ci in c.get("cities", []):
                    if ci["name"].lower() == city.lower():
                        return c["id"], ci["id"]
                raise ProviderError(
                    f"city {city!r} is not a nordvpn location in {country}."
                )
            return c["id"], None
    raise ProviderError(f"country {country!r} is not a nordvpn location.")


def _nord_pubkey(server: dict) -> str:
    for tech in server.get("technologies", []):
        if tech.get("identifier") == "wireguard_udp":
            for m in tech.get("metadata", []):
                if m.get("name") == "public_key":
                    return m["value"]
    raise ProviderError(
        f"nordvpn server {server.get('hostname')} has no WireGuard public key."
    )


def nordvpn_resolve(country: str, city: str) -> dict:
    """Pick the recommended WireGuard server for a location -> peer parameters."""
    country_id, city_id = _nord_ids(country, city)
    flt = (
        f"filters[country_city_id]={city_id}"
        if city_id
        else f"filters[country_id]={country_id}"
    )
    url = (
        f"{NORD_API}/servers/recommendations?{flt}"
        "&filters[servers_technologies][identifier]=wireguard_udp&limit=1"
    )
    try:
        servers = _get_json(url)
    except (urllib.error.URLError, ValueError) as e:
        raise ProviderError(
            f"could not resolve a nordvpn server for {country}: {e}"
        ) from e
    if not servers:
        where = f"{city}, {country}" if city else country
        raise ProviderError(f"nordvpn has no WireGuard server available in {where}.")
    s = servers[0]
    host = (s.get("ips") or [{}])[0].get("ip", {}).get("ip") or s.get("station")
    if not host:
        raise ProviderError(f"nordvpn server {s.get('hostname')} has no usable IP.")
    return {
        "host": host,
        "port": WG_PORT,
        "public_key": _nord_pubkey(s),
        "hostname": s.get("hostname", host),
    }


def forget_nord_countries() -> None:
    global _nord_countries_cache
    _nord_countries_cache = None


# ---- authentication --------------------------------------------------------
#
# Credentials are added explicitly with ``alle providers add <name>`` and
# stored locally (see credentials.py); alle never reads them from the
# environment. Each token provider declares *how* it authenticates so the CLI can
# drive the right prompt.


@dataclass(frozen=True)
class AuthField:
    """One credential a provider's login form asks for."""

    key: str  # storage key in credentials.yaml
    label: str  # prompt / form label shown to the user
    secret: bool = True  # hidden while typing and masked when displayed


# The registry. ``kind`` is "token" (API-backed) or "config" (portal .conf).
# ``functional`` marks providers wired end-to-end; alle only ships NordVPN (token)
# and Proton VPN (config) — together they cover both archetypes.
REGISTRY: dict[str, dict] = {
    "nordvpn": {
        "name": "NordVPN",
        "kind": "token",
        "functional": True,
        "fields": [AuthField("token", "Access token")],
        "help": "my.nordaccount.com → Services → NordVPN → Manual setup → "
        "generate a new access token.",
        "url": "https://my.nordaccount.com/",
        "derive_key": nordvpn_derive_key,
        "wg_address": NORDVPN_WG_ADDRESS,
        "resolve": nordvpn_resolve,
        "locations": nordvpn_locations,
        # Drops the in-process country cache so the next "locations" call truly
        # hits the API — what a forced refresh must do even in a long-lived
        # process (the daemon, a future Web UI), not just a fresh CLI run.
        "forget_locations": forget_nord_countries,
    },
    "protonvpn": {
        "name": "Proton VPN",
        "kind": "config",
        "functional": False,
        "config_help": "Proton VPN has no usable WireGuard API. Generate a WireGuard "
        "config in the Proton portal (Downloads → WireGuard configuration), "
        "then add it as a channel: "
        "alle channels add protonvpn --config /path/to/proton.conf",
        "url": "https://account.protonvpn.com/downloads",
    },
}

# Functional providers only, in the shape locations.py / provider_wg expect.
PROVIDERS = {k: v for k, v in REGISTRY.items() if v.get("functional")}

# Human-facing names. Keys stay lowercase for config/CLI use.
PROVIDER_NAMES = {k: v["name"] for k, v in REGISTRY.items()}


def known() -> list[str]:
    """Every provider alle recognises, sorted."""
    return sorted(REGISTRY)


def supported() -> list[str]:
    """Functional providers — those you can actually add channels under today."""
    return sorted(PROVIDERS)


def kind(provider: str) -> str:
    return REGISTRY.get(provider, {}).get("kind", "token")


def is_functional(provider: str) -> bool:
    return bool(REGISTRY.get(provider, {}).get("functional"))


def display_name(key: str) -> str:
    return PROVIDER_NAMES.get(key, key)


def config_help(provider: str) -> str:
    return REGISTRY.get(provider, {}).get("config_help", "")


def url(provider: str) -> str:
    return REGISTRY.get(provider, {}).get("url", "")


def auth_fields(provider: str) -> list[AuthField]:
    return REGISTRY.get(provider, {}).get("fields", [])


def auth_help(provider: str) -> tuple[str, str]:
    """``(instructions, url)`` for obtaining this provider's credential."""
    a = REGISTRY.get(provider, {})
    return a.get("help", ""), a.get("url", "")


def preview(provider: str, creds: dict) -> str:
    """Masked form of a provider's primary secret, for display (never the raw value)."""
    for f in auth_fields(provider):
        if f.secret:
            return mask(str(creds.get(f.key, "")))
    return ""


def match(name: str) -> str | None:
    """Resolve a user-typed provider (key or brand name, any case) to its key."""
    low = name.strip().lower()
    for p in REGISTRY:
        if low in (p, display_name(p).lower()):
            return p
    return None


def provider_wg(provider: str, country: str, city: str = "") -> dict:
    """Resolve a functional provider + location into WireGuard params for a channel.

    Uses the stored credential to derive the account's private key and the
    provider API to pick a server, producing the ``wgconf.parse`` shape so
    API-derived and (later) imported channels are identical at rest.
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ProviderError(
            f"{display_name(provider)} cannot resolve locations from an API."
        )
    creds = credentials.get(provider)
    if not creds:
        raise ProviderAuthError(
            f"{display_name(provider)} is not authenticated — run `alle providers add {provider}`."
        )
    private_key = spec["derive_key"](creds)
    peer = spec["resolve"](country, city)
    return {
        "private_key": private_key,
        "address": list(spec["wg_address"]),
        "peer": {
            "public_key": peer["public_key"],
            "endpoint_host": peer["host"],
            "endpoint_port": peer["port"],
            "preshared_key": None,
            "allowed_ips": ["0.0.0.0/0", "::/0"],
            "keepalive": 25,
        },
    }
