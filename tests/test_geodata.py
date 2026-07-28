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


# ---- one read + one digest check per category, per operation -----------------
#
# Deduplication may only remove repeated work *inside* one compile or trace.
# Verification across operations is the integrity boundary: a file swapped
# between two of them must still be caught, so nothing here may be cached by
# pathname, mtime, or size.


class _HashSpy:
    """`geodata.hashlib` with a counting sha256 — the real module otherwise."""

    def __init__(self, module):
        self.__dict__["_module"] = module
        self.__dict__["digests"] = 0

    def sha256(self, data=b""):
        self.digests += 1
        return self._module.sha256(data)

    def __getattr__(self, name):
        return getattr(self._module, name)


@pytest.fixture
def digests(monkeypatch):
    spy = _HashSpy(hashlib)
    monkeypatch.setattr(geodata, "hashlib", spy)
    return spy


def _cache(*matchers):
    """Fetch and record real cache files for ``matchers``, then reload."""
    with patch.object(geodata, "_http_get", _mock_fetch({})):
        geodata.ensure_matchers(list(matchers))
    return Store.load()


def test_verified_reads_and_digests_a_category_once(store, digests):
    """The unit the counts below rest on: one call, one read, one digest — and
    the bytes handed back are the bytes that were hashed."""
    store = _cache(("geosite", "netflix"))
    digests.digests = 0

    found = geodata.verified(store, "geosite", "netflix")

    assert found is not None
    assert digests.digests == 1
    assert found.data == _SRS  # what was verified, not a re-read
    assert found.path.read_bytes() == found.data


def test_verified_rejects_a_tampered_file_without_returning_content(store, digests):
    store = _cache(("geosite", "netflix"))
    path = geodata.cached_path(store, "geosite", "netflix")
    assert path is not None
    path.write_bytes(b"SRS\x01" + b"x" * (len(_SRS) - 4))  # same length, new bytes

    assert geodata.verified(store, "geosite", "netflix") is None
    assert geodata.cached_path(store, "geosite", "netflix") is None


def _geo_store(*matchers):
    """A store whose rule table references each matcher the given number of
    times: ``(("geosite", "netflix"), 3)`` → three rules naming that category.

    The router entrypoint needs its port, or the compile has no inbound to hang
    the rules off and never reaches a geo matcher at all.
    """
    store = _cache(*[m for m, _ in matchers])
    store.ensure_router_port()
    store = Store.load()
    entries = []
    for (kind, name), count in matchers:
        entries.extend([(kind, name)] * count)
    store.create_ruleset("Geo", "direct", entries)
    return Store.load()


def test_compile_digests_each_category_once_however_often_it_is_used(store, digests):
    """Three rules naming one category is one digest check, not three."""
    from alle.engine import Engine

    store = _geo_store((("geosite", "netflix"), 3), (("geoip", "us"), 2))
    digests.digests = 0

    config, errors = Engine(store)._build_config()

    assert errors == {}
    assert digests.digests == 2  # one per distinct category, not per rule
    tags = {rs["tag"] for rs in config["route"]["rule_set"]}
    assert tags == {"geosite-netflix", "geoip-us"}


def test_compile_rejects_every_rule_naming_a_broken_category(store, digests):
    """A remembered failure still has to decorate each affected rule — the
    deduplication must not let later duplicates through unmarked."""
    from alle.engine import Engine

    store = _geo_store((("geosite", "netflix"), 3))
    path = geodata.cached_path(store, "geosite", "netflix")
    assert path is not None
    path.unlink()
    digests.digests = 0

    _config, errors = Engine(store)._build_config()

    assert len(errors) == 3  # r1, r2, r3 — all of them
    assert all("is not cached" in message for message in errors.values())
    assert digests.digests == 0  # the missing file is read once, not three times


def test_trace_verifies_and_parses_each_category_once(store, digests, monkeypatch):
    """Duplicates cost nothing a second time — including duplicates of a
    category that is missing, which used to retry the whole verification."""
    from alle import srs, tracer

    store = _geo_store((("geosite", "netflix"), 3), (("geoip", "us"), 2))
    # A category referenced twice that was never cached: the failure has to be
    # remembered too, or its second mention re-reads and re-digests.
    store.create_ruleset("Missing", "direct", [("geosite", "absent")] * 2)
    store = Store.load()

    parses = []
    real_parse = srs.parse
    monkeypatch.setattr(
        srs, "parse", lambda source, **kw: parses.append(1) or real_parse(source, **kw)
    )
    monkeypatch.setattr(
        tracer,
        "_resolve",
        lambda domain: {"a": ["1.2.3.4"], "aaaa": [], "error": None},
    )
    digests.digests = 0

    result = tracer.trace(store, "www.netflix.com")

    assert digests.digests == 2  # netflix + us; the absent one never reaches a read
    assert len(parses) == 2  # and each verified category is parsed once
    # Both failure kinds are covered, which is the point of tracking attempts
    # rather than successes: `absent` never verifies, and the minimal fixture
    # .srs verifies but does not parse. Neither is retried by its duplicates.
    assert set(result["geo_problems"]) == {
        "geosite:absent",
        "geosite:netflix",
        "geoip:us",
    }
    assert result["geo_problems"]["geosite:absent"].startswith("not cached")
    assert "unreadable" in result["geo_problems"]["geosite:netflix"]


def test_a_file_swapped_between_two_operations_is_caught(store, digests):
    """The boundary that must not move: deduplication is per operation, so the
    second compile re-reads and re-digests rather than trusting the first.

    The replacement keeps the same content-addressed path and the same length,
    so passing this cannot be explained by a pathname- or size-keyed cache.
    """
    from alle.engine import Engine

    store = _geo_store((("geosite", "netflix"), 2))

    _config, errors = Engine(store)._build_config()
    assert errors == {}

    path = geodata.cached_path(store, "geosite", "netflix")
    assert path is not None
    before = path.read_bytes()
    path.write_bytes(b"SRS\x01" + b"y" * (len(before) - 4))
    assert len(path.read_bytes()) == len(before)

    digests.digests = 0
    _config, errors = Engine(store)._build_config()

    assert digests.digests == 1  # it looked again
    assert len(errors) == 2 and all("is not cached" in m for m in errors.values())
