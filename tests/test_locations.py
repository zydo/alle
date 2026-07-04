"""Provider location cache behavior."""

from __future__ import annotations

import json
import time

import pytest

from alle import locations


def test_write_sorts_and_records_metadata(tmp_path, monkeypatch):
    monkeypatch.setitem(
        locations.PROVIDERS,
        "demo",
        {"locations": lambda: {"Zed": ["B", "A"], "Alpha": []}},
    )

    meta = locations.write(tmp_path, "demo")

    assert meta["provider"] == "demo"
    assert meta["source"] == locations.SOURCE
    assert meta["country_count"] == 2
    assert meta["city_count"] == 2
    saved = json.loads(locations.path_for(tmp_path, "demo").read_text())
    assert list(saved["countries"]) == ["Alpha", "Zed"]


def test_write_unknown_provider_errors(tmp_path):
    with pytest.raises(ValueError, match="unknown provider"):
        locations.write(tmp_path, "missing")


def test_update_forgets_provider_cache_and_reports(tmp_path, monkeypatch, capsys):
    calls: list[str] = []

    monkeypatch.setitem(
        locations.PROVIDERS,
        "demo",
        {
            "forget_locations": lambda: calls.append("forgot"),
            "locations": lambda: {"US": ["Seattle"]},
        },
    )

    result = locations.update(tmp_path, ["demo"])

    assert calls == ["forgot"]
    assert result["demo"]["country_count"] == 1
    assert "Reading demo locations from its API" in capsys.readouterr().err


def test_needs_refresh_handles_missing_bad_stale_and_wrong_source(tmp_path):
    assert locations.needs_refresh(tmp_path, "demo") is True

    p = locations.path_for(tmp_path, "demo")
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert locations.needs_refresh(tmp_path, "demo") is True

    p.write_text(json.dumps({"_meta": {"source": "old", "updated_epoch": time.time()}}))
    assert locations.needs_refresh(tmp_path, "demo") is True

    stale = int(time.time()) - locations.MAX_AGE_SECONDS - 1
    p.write_text(
        json.dumps({"_meta": {"source": locations.SOURCE, "updated_epoch": stale}})
    )
    assert locations.needs_refresh(tmp_path, "demo") is True

    fresh = int(time.time())
    p.write_text(
        json.dumps({"_meta": {"source": locations.SOURCE, "updated_epoch": fresh}})
    )
    assert locations.needs_refresh(tmp_path, "demo") is False


def test_load_reads_cached_countries(tmp_path):
    p = locations.path_for(tmp_path, "demo")
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"countries": {"US": ["Seattle"]}}))

    assert locations.load(tmp_path, "demo") == {"US": ["Seattle"]}
