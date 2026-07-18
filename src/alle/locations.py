"""Per-provider country/city lists, cached on disk under ``$ALLE_HOME``.

Source of truth: the **provider's own API** (e.g. NordVPN's ``/v1/servers/countries``),
fetched via ``providers.PROVIDERS[p]["locations"]`` — so the list never lags behind
what the provider actually offers, and because the same API also resolves the
concrete server we connect to, cached choices cannot drift away from what will
actually be connected.

The cache is refreshed on demand when missing, stale (older than ``MAX_AGE``), or
built by a different source tag.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from alle import fsio
from alle.providers import PROVIDERS

MAX_AGE_SECONDS = 24 * 3600  # 1 day; force a refresh sooner with `--refresh`
SOURCE = "provider-api-v1"


class LocationCacheError(ValueError):
    """A parsed location cache has an unusable root or nested shape."""


def path_for(root: Path, provider: str) -> Path:
    return root / "providers" / f"{provider}.json"


def _validate_countries(value) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise LocationCacheError("countries is not an object")
    for country, cities in value.items():
        if not isinstance(country, str) or not country:
            raise LocationCacheError("country name is not a non-empty string")
        if not isinstance(cities, list):
            raise LocationCacheError(f"cities for {country!r} is not a list")
        if any(not isinstance(city, str) or not city for city in cities):
            raise LocationCacheError(f"cities for {country!r} contains a bad name")
    return value


def _parse_cache(text: str, provider: str, *, require_meta: bool) -> dict:
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        raise LocationCacheError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LocationCacheError("root is not an object")
    countries = _validate_countries(data.get("countries", {}))
    if require_meta or "_meta" in data:
        meta = data.get("_meta")
        if not isinstance(meta, dict):
            raise LocationCacheError("_meta is not an object")
        if meta.get("provider", provider) != provider:
            raise LocationCacheError("_meta.provider does not match the cache")
        if not isinstance(meta.get("source"), str):
            raise LocationCacheError("_meta.source is not a string")
        epoch = meta.get("updated_epoch")
        if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
            raise LocationCacheError("_meta.updated_epoch is not a number")
        for key in ("country_count", "city_count"):
            if key in meta and (
                not isinstance(meta[key], int) or isinstance(meta[key], bool)
            ):
                raise LocationCacheError(f"_meta.{key} is not an integer")
    return {"_meta": data.get("_meta"), "countries": countries}


def write(root: Path, provider: str) -> dict:
    if provider not in PROVIDERS:
        raise ValueError(
            f"unknown provider '{provider}' (known: {', '.join(sorted(PROVIDERS))})"
        )
    countries = _validate_countries(PROVIDERS[provider]["locations"]())
    out: dict = {
        "_meta": {
            "provider": provider,
            "source": SOURCE,
            "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_epoch": int(time.time()),
            "country_count": len(countries),
            "city_count": sum(len(v) for v in countries.values()),
        },
        "countries": dict(sorted(countries.items())),
    }
    p = path_for(root, provider)
    # Locked + atomic: two concurrent refreshes serialise (last complete write
    # wins) and a reader never sees a torn file.
    with fsio.locked(p.parent / ".locations.lock"):
        fsio.write_durably(
            p,
            lambda f: f.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n"),
            prefix=f".{provider}-",
            suffix=".json",
        )
    return out["_meta"]


def update(root: Path, providers) -> dict[str, dict]:
    """Refresh the given providers from their APIs; returns {provider: meta}.

    "From their APIs" is a guarantee: any in-process location cache the provider
    module keeps is dropped first, so a forced refresh fetches fresh data even
    inside a long-lived process, not only in a new CLI invocation.
    """
    results = {}
    for p in providers:
        forget = PROVIDERS.get(p, {}).get("forget_locations")
        if forget:
            forget()
        print(f"Reading {p} locations from its API...", file=sys.stderr)
        meta = write(root, p)
        print(
            f"  {meta['country_count']} countries, {meta['city_count']} cities",
            file=sys.stderr,
        )
        results[p] = meta
    return results


def needs_refresh(root: Path, provider: str) -> bool:
    """True if the cached list is missing, from a different source, or too old."""
    p = path_for(root, provider)
    if not p.exists():
        return True
    try:
        parsed = _parse_cache(p.read_text(), provider, require_meta=True)
        meta = parsed["_meta"]
    except (LocationCacheError, OSError):
        return True
    if meta.get("source") != SOURCE:
        return True
    return int(time.time()) - int(meta["updated_epoch"]) > MAX_AGE_SECONDS


def load(root: Path, provider: str) -> dict[str, list[str]]:
    p = path_for(root, provider)
    return _parse_cache(p.read_text(), provider, require_meta=False)["countries"]
