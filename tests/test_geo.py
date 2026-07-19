"""Parsing country/subdivision from a WireGuard config file name (ISO 3166 via
pycountry). Only the *name* is read — never contents, never geo-IP."""

from __future__ import annotations

import pytest

from alle import geo


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("wg-US-CA-842", ("United States", "California")),
        ("wg_US_CA_842", ("United States", "California")),  # slug separators too
        ("wg-GB-ENG-3", ("United Kingdom", "England")),
        ("wg-DE-BE-1", ("Germany", "Berlin")),
        ("wg-JP-42", ("Japan", "")),  # no subdivision token
        (
            "wg-US-842",
            ("United States", ""),
        ),  # numeric second token isn't a subdivision
        ("US-CA-14", ("United States", "California")),  # no wg prefix
        ("wg-ZZ-XX-1", ("", "")),  # ZZ is not a country
        ("random-file", ("", "")),  # doesn't match the convention
        ("", ("", "")),
    ],
)
def test_from_filename(stem, expected):
    assert geo.from_filename(stem) == expected


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("United States", "us"),
        ("Switzerland", "ch"),
        ("United Kingdom", "gb"),
        ("South Korea", "kr"),  # via fuzzy search (common short form)
        ("Czech Republic", "cz"),
        ("US", "us"),  # already a code
        ("de", "de"),
        ("Atlantis", ""),  # unresolvable -> empty, caller falls back
        ("", ""),
    ],
)
def test_country_code(name, code):
    assert geo.country_code(name) == code
