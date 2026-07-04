"""Per-provider country/city lists, cached on disk under ``$ALLE_HOME``.

Source of truth: the **provider's own API** (e.g. NordVPN's ``/v1/servers/countries``),
fetched via ``providers.PROVIDERS[p]["locations"]``. Unlike the old gluetun-image
extraction, this never lags behind what the provider actually offers — the same API
also resolves the concrete server we connect to, so cached choices cannot drift
away from what will actually be connected.

The cache is refreshed on demand when missing, stale (older than ``MAX_AGE``), or
built by a different source tag.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from alle.providers import PROVIDERS

MAX_AGE_SECONDS = 24 * 3600  # 1 day; force a refresh sooner with `--refresh`
SOURCE = "provider-api-v1"


def path_for(root: Path, provider: str) -> Path:
    return root / "providers" / f"{provider}.json"


def write(root: Path, provider: str) -> dict:
    if provider not in PROVIDERS:
        raise ValueError(
            f"unknown provider '{provider}' (known: {', '.join(sorted(PROVIDERS))})"
        )
    countries = PROVIDERS[provider]["locations"]()
    out = {
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
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
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
        meta = json.loads(p.read_text()).get("_meta", {})
    except (ValueError, OSError):
        return True
    if meta.get("source") != SOURCE:
        return True
    return int(time.time()) - int(meta.get("updated_epoch", 0)) > MAX_AGE_SECONDS


def load(root: Path, provider: str) -> dict[str, list[str]]:
    p = path_for(root, provider)
    if not p.exists():
        raise FileNotFoundError(p)
    return json.loads(p.read_text())["countries"]
