"""Metrics accumulator + SQLite totals: the Clash API only reports live
connections with lifetime-cumulative counters, so the accumulator must bank
monotonic deltas and survive connection close and counter resets."""

from __future__ import annotations

from alle import metrics


def _conn(cid, tag, up, down):
    return {"id": cid, "chains": [tag], "upload": up, "download": down}


def test_first_snapshot_banks_full_counters():
    acc = metrics.Accumulator()
    banked = acc.observe([_conn("a", "out-nordvpn-us_1", 100, 200)])
    assert banked == {("nordvpn", "us_1"): (100, 200)}
    assert metrics.totals()[("nordvpn", "us_1")] == {
        "sent": 100,
        "received": 200,
        "updated_at": metrics.totals()[("nordvpn", "us_1")]["updated_at"],
    }


def test_only_the_delta_is_banked_across_snapshots():
    acc = metrics.Accumulator()
    acc.observe([_conn("a", "out-nordvpn-us_1", 100, 200)])
    acc.observe([_conn("a", "out-nordvpn-us_1", 250, 500)])  # +150 / +300
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (250, 500)


def test_closed_connection_is_dropped_and_not_double_counted():
    acc = metrics.Accumulator()
    acc.observe([_conn("a", "out-nordvpn-us_1", 100, 100)])
    acc.observe([])  # connection closed — bytes already banked, nothing new
    # a brand-new connection reusing counters from 0 banks its full value
    acc.observe([_conn("b", "out-nordvpn-us_1", 50, 50)])
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (150, 150)


def test_counter_reset_treated_as_fresh_connection():
    acc = metrics.Accumulator()
    acc.observe([_conn("a", "out-nordvpn-us_1", 500, 500)])
    # id reused after a sing-box restart with a smaller counter -> bank new value,
    # never a negative delta
    acc.observe([_conn("a", "out-nordvpn-us_1", 30, 40)])
    t = metrics.totals()[("nordvpn", "us_1")]
    assert (t["sent"], t["received"]) == (530, 540)


def test_totals_are_per_channel():
    acc = metrics.Accumulator()
    acc.observe(
        [
            _conn("a", "out-nordvpn-us_1", 100, 0),
            _conn("b", "out-nordvpn-uk_1", 0, 300),
        ]
    )
    totals = metrics.totals()
    assert totals[("nordvpn", "us_1")]["sent"] == 100
    assert totals[("nordvpn", "uk_1")]["received"] == 300


def test_connections_without_a_channel_chain_are_ignored():
    acc = metrics.Accumulator()
    banked = acc.observe(
        [
            {"id": "x", "chains": ["direct"], "upload": 10, "download": 10},
            {"id": "y", "chains": [], "upload": 10, "download": 10},
        ]
    )
    assert banked == {}
    assert metrics.totals() == {}


def test_remove_channel_and_provider_clear_totals():
    metrics.add_delta("nordvpn", "us_1", 10, 10)
    metrics.add_delta("nordvpn", "uk_1", 5, 5)
    metrics.remove_channel("nordvpn", "us_1")
    assert ("nordvpn", "us_1") not in metrics.totals()
    assert ("nordvpn", "uk_1") in metrics.totals()
    metrics.remove_provider("nordvpn")
    assert metrics.totals() == {}
