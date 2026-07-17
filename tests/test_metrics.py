"""Metrics accumulator + SQLite totals: the Clash API only reports live
connections with lifetime-cumulative counters, so the accumulator must bank
monotonic deltas, survive connection close, failed samples, and sing-box
restarts — and deleted rows must stay deleted (tombstones)."""

from __future__ import annotations

import pytest

from alle import metrics


def _conn(cid, tag, up, down):
    return {"id": cid, "chains": [tag], "upload": up, "download": down}


def _primed(generation="g1"):
    """An accumulator that has already baselined an empty snapshot of
    ``generation`` — everything that appears afterwards is under its watch."""
    acc = metrics.Accumulator()
    acc.observe([], generation=generation)
    return acc


def test_first_snapshot_of_a_generation_baselines_without_banking():
    # Counters seen on first contact may predate this daemon (a daemon restart
    # under a live sing-box) and may already be banked — never re-bank them.
    acc = metrics.Accumulator()
    banked = acc.observe([_conn("a", "out-nordvpn-us_1", 100, 200)], generation="g1")
    assert banked == {}
    assert metrics.totals() == {}


def test_connections_after_the_baseline_bank_their_full_counters():
    acc = _primed()
    banked = acc.observe([_conn("a", "out-nordvpn-us_1", 100, 200)], generation="g1")
    assert banked == {("nordvpn", "us_1"): (100, 200)}
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (100, 200)


def test_only_the_delta_is_banked_across_snapshots():
    acc = _primed()
    acc.observe([_conn("a", "out-nordvpn-us_1", 100, 200)], generation="g1")
    acc.observe(
        [_conn("a", "out-nordvpn-us_1", 250, 500)], generation="g1"
    )  # +150 / +300
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (250, 500)


def test_failed_sample_keeps_watermarks_and_banks_nothing():
    # None = "couldn't sample" (API unreachable / malformed payload). It must
    # not clear the watermarks: the next good sample banks only the increment,
    # never the whole lifetime counter again.
    acc = _primed()
    acc.observe([_conn("a", "out-nordvpn-us_1", 100, 100)], generation="g1")
    assert acc.observe(None, generation="g1") == {}
    banked = acc.observe([_conn("a", "out-nordvpn-us_1", 120, 130)], generation="g1")
    assert banked == {("nordvpn", "us_1"): (20, 30)}  # not 120/130
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (120, 130)


def test_generation_change_rebaselines_instead_of_misreading_counters():
    acc = _primed("g1")
    acc.observe([_conn("a", "out-nordvpn-us_1", 500, 500)], generation="g1")
    # sing-box restarted: same connection id reappears with small counters.
    # A new generation baselines (bytes before our first sample of it are
    # unknowable) — no negative delta, no lifetime re-bank.
    assert acc.observe([_conn("a", "out-nordvpn-us_1", 30, 40)], generation="g2") == {}
    banked = acc.observe([_conn("a", "out-nordvpn-us_1", 90, 100)], generation="g2")
    assert banked == {("nordvpn", "us_1"): (60, 60)}
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (560, 560)


def test_closed_connection_is_dropped_and_not_double_counted():
    acc = _primed()
    acc.observe([_conn("a", "out-nordvpn-us_1", 100, 100)], generation="g1")
    acc.observe([], generation="g1")  # connection closed — bytes already banked
    # a brand-new connection starts its counters from 0 under our watch
    acc.observe([_conn("b", "out-nordvpn-us_1", 50, 50)], generation="g1")
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (150, 150)


def test_counter_reset_within_a_generation_treated_as_fresh_connection():
    acc = _primed()
    acc.observe([_conn("a", "out-nordvpn-us_1", 500, 500)], generation="g1")
    # id anomaly within one generation: bank the new value, never a negative
    acc.observe([_conn("a", "out-nordvpn-us_1", 30, 40)], generation="g1")
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (530, 540)


def test_totals_are_per_channel():
    acc = _primed()
    acc.observe(
        [
            _conn("a", "out-nordvpn-us_1", 100, 0),
            _conn("b", "out-nordvpn-uk_1", 0, 300),
        ],
        generation="g1",
    )
    totals = metrics.totals()
    assert totals[("nordvpn", "us_1")]["sent"] == 100
    assert totals[("nordvpn", "uk_1")]["received"] == 300


def test_connections_without_a_channel_chain_are_ignored():
    acc = _primed()
    banked = acc.observe(
        [
            {"id": "x", "chains": ["direct"], "upload": 10, "download": 10},
            {"id": "y", "chains": [], "upload": 10, "download": 10},
        ],
        generation="g1",
    )
    assert banked == {}
    assert metrics.totals() == {}


def test_parse_failure_does_not_advance_any_watermark():
    acc = _primed()
    acc.observe([_conn("a", "out-nordvpn-us_1", 100, 100)], generation="g1")
    malformed = _conn("b", "out-nordvpn-us_1", "not-a-counter", 1)
    with pytest.raises(ValueError):
        acc.observe(
            [_conn("a", "out-nordvpn-us_1", 150, 160), malformed],
            generation="g1",
        )
    banked = acc.observe([_conn("a", "out-nordvpn-us_1", 160, 170)], generation="g1")
    assert banked == {("nordvpn", "us_1"): (60, 70)}


def test_database_failure_does_not_advance_watermarks(monkeypatch):
    acc = _primed()
    acc.observe([_conn("a", "out-nordvpn-us_1", 100, 100)], generation="g1")
    real_add = metrics.add_deltas
    monkeypatch.setattr(
        metrics, "add_deltas", lambda _deltas: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError, match="disk"):
        acc.observe([_conn("a", "out-nordvpn-us_1", 150, 160)], generation="g1")
    monkeypatch.setattr(metrics, "add_deltas", real_add)
    banked = acc.observe([_conn("a", "out-nordvpn-us_1", 160, 170)], generation="g1")
    assert banked == {("nordvpn", "us_1"): (60, 70)}


def test_remove_channel_and_provider_clear_totals():
    metrics.add_delta("nordvpn", "us_1", 10, 10)
    metrics.add_delta("nordvpn", "uk_1", 5, 5)
    metrics.remove_channel("nordvpn", "us_1")
    assert ("nordvpn", "us_1") not in metrics.totals()
    assert ("nordvpn", "uk_1") in metrics.totals()
    metrics.remove_provider("nordvpn")
    assert metrics.totals() == {}


def test_tombstone_blocks_late_samples_until_revived():
    metrics.add_delta("nordvpn", "us_1", 10, 10)
    metrics.remove_channel("nordvpn", "us_1")

    # a late daemon sample for the deleted channel must not recreate the row
    metrics.add_delta("nordvpn", "us_1", 99, 99)
    assert metrics.totals() == {}

    # the identity is legitimately re-created: traffic counts again, from zero
    metrics.revive_channel("nordvpn", "us_1")
    metrics.add_delta("nordvpn", "us_1", 7, 8)
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (7, 8)


def test_provider_tombstone_covers_every_channel_and_lifts_on_readd():
    metrics.add_delta("nordvpn", "us_1", 10, 10)
    metrics.remove_provider("nordvpn")

    # any channel under the removed provider is refused — even one that never
    # had a row (nothing existed to delete, but nothing may be created either)
    metrics.add_delta("nordvpn", "us_1", 5, 5)
    metrics.add_delta("nordvpn", "uk_1", 5, 5)
    assert metrics.totals() == {}

    metrics.revive_provider("nordvpn")
    metrics.add_delta("nordvpn", "uk_1", 3, 4)
    assert metrics.totals()[("nordvpn", "uk_1")]["sent"] == 3


def test_channel_tombstone_survives_a_provider_revive():
    # remove one channel, then remove + re-add the provider: the provider-wide
    # tombstone lifts, but the individually-removed channel needs its own revive
    metrics.add_delta("nordvpn", "us_1", 10, 10)
    metrics.remove_channel("nordvpn", "us_1")
    metrics.revive_provider("nordvpn")  # no-op for the per-channel tombstone
    metrics.add_delta("nordvpn", "us_1", 5, 5)
    assert metrics.totals() == {}
    metrics.revive_channel("nordvpn", "us_1")
    metrics.add_delta("nordvpn", "us_1", 5, 5)
    assert metrics.totals()[("nordvpn", "us_1")]["sent"] == 5


def test_batch_tombstone_reconciliation_preserves_remove_and_revive_order():
    metrics.add_deltas(
        {
            ("nordvpn", "old"): (10, 10),
            ("protonvpn", "drop"): (20, 20),
        }
    )
    metrics.reconcile_tombstones(
        removed_providers=["nordvpn"],
        removed_channels=[("protonvpn", "drop")],
        revived_providers=["nordvpn"],
        revived_channels=[("nordvpn", "new")],
    )

    assert metrics.totals() == {}
    metrics.add_deltas(
        {
            ("nordvpn", "new"): (1, 2),
            ("protonvpn", "drop"): (3, 4),
        }
    )
    assert set(metrics.totals()) == {("nordvpn", "new")}
