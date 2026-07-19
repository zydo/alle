"""Best-effort location from a WireGuard config *file name*.

Config-provider files follow a country/subdivision naming convention. ProtonVPN,
for example, names them ``wg-<CC>-<SUB>-<n>.conf`` where ``CC`` is an ISO 3166-1
alpha-2 country code and ``SUB`` an ISO 3166-2 subdivision code (a US state, a
province, …) — e.g. ``wg-US-CA-842`` → United States / California. Because those
are ISO codes, the "convention" is just the ISO 3166 standard, which ``pycountry``
already encodes; there is no provider-specific table to maintain.

This reads only the *name* the user gave the file — never the config contents, and
never a network/geo-IP lookup. Anything that doesn't resolve to a real ISO entry
comes back blank so the caller can render it as unknown rather than guess.
"""

from __future__ import annotations

import re

import pycountry


def country_code(name: str) -> str:
    """Lowercase ISO 3166-1 alpha-2 code for a country display name, or ``""``.

    The reverse of what :func:`from_filename` does: providers' location APIs
    hand back display names ("United States", "Switzerland"), and channel ids
    want the compact code (``us``, ``ch``). Exact name/official/common-name
    matches first, then pycountry's fuzzy search as a safety net ("South
    Korea" → KR). Anything unresolvable returns ``""`` so the caller can fall
    back to the full name rather than guess.
    """
    label = (name or "").strip()
    if not label:
        return ""
    if len(label) == 2 and label.isalpha():  # already a code
        found = pycountry.countries.get(alpha_2=label.upper())
        return found.alpha_2.lower() if found else ""
    lowered = label.lower()
    for entry in pycountry.countries:
        for field in ("name", "common_name", "official_name"):
            if str(getattr(entry, field, "")).lower() == lowered:
                return str(entry.alpha_2).lower()
    try:
        matches = pycountry.countries.search_fuzzy(label)
    except LookupError:
        return ""
    return str(matches[0].alpha_2).lower() if matches else ""


def from_filename(stem: str) -> tuple[str, str]:
    """``("United States", "California")`` from a stem like ``wg-US-CA-842``.

    Either element is ``""`` when it can't be resolved. Recognises an optional
    leading ``wg`` token, then ``<country>`` optionally followed by
    ``<subdivision>``; trailing tokens (server number, etc.) are ignored.
    """
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", stem) if t]
    if tokens and tokens[0].lower() == "wg":
        tokens = tokens[1:]
    if not tokens or len(tokens[0]) != 2:
        return "", ""

    country = pycountry.countries.get(alpha_2=tokens[0].upper())
    if country is None:
        return "", ""
    country_name = getattr(country, "common_name", country.name)

    city = ""
    if len(tokens) >= 2 and tokens[1].isalpha():
        sub = pycountry.subdivisions.get(code=f"{country.alpha_2}-{tokens[1].upper()}")
        if sub is not None:
            city = str(getattr(sub, "name", ""))
    return country_name, city
