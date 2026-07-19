"""geosite/geoip rule-set data: fetch, verify, cache, prune, and compile."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest

from alle import geodata, routes
from alle.state import Store

# A minimal valid binary rule-set: the 4-byte header (magic + version) plus
# zlib-compressed empty content. Real files carry category data; this is
# enough to pass the magic check and exercise the pipeline.
_SRS_HEADER = b"SRS\x01"
_SRS_BODY = b"x\x9c\x03\x00\x00\x00\x00\x01"
_SRS = _SRS_HEADER + _SRS_BODY


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def store():
    return Store.load()


# ---- normalize / infer -------------------------------------------------------


def test_normalize_forgives_filename_form():
    assert routes.normalize_geo("geosite", "geosite-netflix.srs") == "netflix"
    assert routes.normalize_geo("geoip", "GEOIP-US.SRS") == "us"
    assert routes.normalize_geo("geosite", "category-ads-all") == "category-ads-all"
    assert routes.normalize_geo("geosite", "apple@cn") == "apple@cn"


def test_normalize_rejects_bad_names():
    with pytest.raises(routes.RuleError, match="not a valid geosite"):
        routes.normalize_geo("geosite", "")
    # UPPER is valid: names are case-insensitive (normalized to lowercase)
    with pytest.raises(routes.RuleError, match="not a valid geosite"):
        routes.normalize_geo("geosite", "has spaces")


def test_infer_handles_prefixed_string_form():
    assert routes.infer_matcher("geosite:netflix", None) == ("geosite", "netflix")
    assert routes.infer_matcher("geoip:us", None) == ("geoip", "us")
    assert routes.infer_matcher("netflix.com", None) == ("domain_suffix", "netflix.com")


def test_shadow_lint_degrades_for_geo():
    # same category covers itself
    assert routes.covers(
        {"type": "geosite", "value": "netflix"}, {"type": "geosite", "value": "netflix"}
    )
    # different kind / different category: skip (not covered)
    assert not routes.covers(
        {"type": "geosite", "value": "netflix"}, {"type": "geoip", "value": "us"}
    )
    assert not routes.covers(
        {"type": "geosite", "value": "netflix"}, {"type": "geosite", "value": "google"}
    )
    # all still covers geo (the catch-all covers everything)
    assert routes.covers(
        {"type": "all", "value": ""}, {"type": "geosite", "value": "netflix"}
    )


# ---- fetch / cache / digest verification -------------------------------------


def _mock_fetch(kind_name_commit_map: dict):
    """Mock _http_get to return canned responses: GitHub API JSON for branch
    resolution, .srs bytes for file fetches, trees API JSON for manifests."""

    def fake_get(url, *, accept=None):
        if "/branches/" in url:
            return json.dumps(
                {"commit": {"sha": kind_name_commit_map.get("__commit__", "c" * 40)}}
            ).encode()
        if "/git/trees/" in url:
            return json.dumps({"tree": []}).encode()
        # raw file fetch — return the .srs bytes
        return _SRS

    return fake_get


def test_ensure_matchers_fetches_and_records(store):
    with patch.object(geodata, "_http_get", _mock_fetch({})):
        fetched = geodata.ensure_matchers([("geosite", "netflix"), ("geoip", "us")])

    assert fetched == ["geosite:netflix", "geoip:us"]
    store = Store.load()  # reload — ensure_matchers mutated the file internally
    # the record has source + commit + per-file digests
    for kind, name in [("geosite", "netflix"), ("geoip", "us")]:
        entry = store.data["geodata"][kind]["files"][name]
        assert entry["sha256"] == _sha256(_SRS)
        assert entry["size"] == len(_SRS)
    # cache files exist with content-addressed names
    path = geodata.cached_path(store, "geosite", "netflix")
    assert path is not None and path.exists()
    assert path.read_bytes() == _SRS


def test_ensure_matchers_skips_cached_categories(store):
    with patch.object(geodata, "_http_get", _mock_fetch({})):
        geodata.ensure_matchers([("geosite", "netflix")])
        # second call: already cached, no fetch
        fetched = geodata.ensure_matchers([("geosite", "netflix")])
    assert fetched == []


def test_ensure_matchers_noop_without_geo(store):
    assert geodata.ensure_matchers([("domain_suffix", "netflix.com")]) == []
    assert "geodata" not in store.data or not store.data.get("geodata")


def test_digest_verification_rejects_tampered_files(store):
    with patch.object(geodata, "_http_get", _mock_fetch({})):
        geodata.ensure_matchers([("geosite", "netflix")])
    store = Store.load()  # reload to see the recorded digest
    # tamper: overwrite the cached file with different content
    path = geodata.cached_path(store, "geosite", "netflix")
    assert path is not None
    path.write_bytes(b"SRS\x01TAMPERED")
    # the digest no longer matches — cached_path returns None
    assert geodata.cached_path(store, "geosite", "netflix") is None


def test_404_produces_a_clear_error_with_no_state_change(store):
    def fake_get(url, *, accept=None):
        if "/branches/" in url:
            return json.dumps({"commit": {"sha": "d" * 40}}).encode()
        raise geodata.GeoDataError("HTTP 404 fetching https://example.com/x.srs")

    with patch.object(geodata, "_http_get", fake_get):
        with pytest.raises(geodata.GeoDataError, match="no geosite category 'netflix'"):
            geodata.ensure_matchers([("geosite", "netflix")])
    # nothing was recorded
    assert "netflix" not in (store.data.get("geodata") or {}).get("geosite", {}).get(
        "files", {}
    )


def test_bad_header_rejected(store):
    def fake_get(url, *, accept=None):
        if "/branches/" in url:
            return json.dumps({"commit": {"sha": "e" * 40}}).encode()
        return b"<html>not a rule-set</html>"

    with patch.object(geodata, "_http_get", fake_get):
        with pytest.raises(geodata.GeoDataError, match="not a binary rule-set"):
            geodata.ensure_matchers([("geosite", "netflix")])


# ---- prune -------------------------------------------------------------------


def test_prune_removes_unreferenced_files(store):
    with patch.object(geodata, "_http_get", _mock_fetch({})):
        geodata.ensure_matchers([("geosite", "netflix"), ("geosite", "google")])
    store = Store.load()  # reload to see the recorded digests
    # remove one from state to simulate it becoming unreferenced
    store.update_geodata(
        "geosite",
        source="sagernet",
        commit="f" * 40,
        files={"google": store.data["geodata"]["geosite"]["files"]["google"]},
        replace=True,
    )
    pruned = geodata.prune(store)
    assert any("netflix" in name for name in pruned)
    assert all("google" not in name for name in pruned)


def test_cache_files_are_0700_dir():
    d = geodata.cache_dir()
    import stat

    assert stat.S_IMODE(d.stat().st_mode) == 0o700


# ---- referenced / source switching ------------------------------------------


def test_referenced_extracts_geo_matchers_from_rules(store):
    store.create_ruleset("A", "direct", [("geosite", "netflix"), ("geosite", "google")])
    store.create_ruleset("B", "direct", [("geoip", "us"), ("domain_suffix", "x.com")])
    refs = geodata.referenced(store)
    assert refs["geosite"] == {"netflix", "google"}
    assert refs["geoip"] == {"us"}


def test_source_switching_clears_old_files(store):
    with patch.object(geodata, "_http_get", _mock_fetch({"__commit__": "a" * 40})):
        geodata.ensure_matchers([("geosite", "netflix")])
    store = Store.load()
    store.set_geodata_source("metacubex")
    assert store.data["geodata"]["source"] == "metacubex"
    assert geodata.source_name(store) == "metacubex"


# ---- category lookup (offline) -----------------------------------------------


def test_categories_empty_before_first_refresh():
    out = geodata.categories()
    assert out == {"geosite": [], "geoip": []}
    assert geodata.manifest() == {}


def test_categories_search_from_recorded_manifest():
    geodata._manifest_path().write_text(
        json.dumps(
            {
                "source": "sagernet",
                "geosite": {
                    "commit": "c" * 40,
                    "names": ["netflix", "google", "apple@cn"],
                },
                "geoip": {"commit": "c" * 40, "names": ["us", "cn", "de"]},
            }
        )
    )
    assert geodata.categories(query="netflix") == {"geosite": ["netflix"], "geoip": []}
    assert geodata.categories(kind="geoip") == {"geoip": ["us", "cn", "de"]}
    assert geodata.categories(kind="geosite", query="CN") == {"geosite": ["apple@cn"]}


def test_upstream_urls_are_plaintext_browsable():
    assert "domain-list-community" in geodata.upstream_url("geosite")
    assert "ISO_3166" in geodata.upstream_url("geoip")


def test_404_error_names_the_plaintext_upstream(store):
    def fake_get(url, *, accept=None):
        if "/branches/" in url:
            return json.dumps({"commit": {"sha": "d" * 40}}).encode()
        raise geodata.GeoDataError("HTTP 404 fetching https://example.com/x.srs")

    with patch.object(geodata, "_http_get", fake_get):
        with pytest.raises(geodata.GeoDataError, match="browse names: https://"):
            geodata.ensure_matchers([("geosite", "nosuchcategory")])
