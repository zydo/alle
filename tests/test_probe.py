"""Heartbeat probe: IP-echo source fallback and response validation.

The probe routes through a channel's loopback proxy, so these tests stand in for
the real proxy with a canned opener that dispenses responses (or raises) per
``.open()`` call, in source order: cloudflare-trace, icanhazip, ipify.
"""

from __future__ import annotations

import time
import urllib.error

import pytest

from alle import probe


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """Dispenses canned responses (bytes) or exceptions per ``.open()`` call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requested: list[str] = []

    def open(self, req, timeout=None):
        self.requested.append(req.full_url)
        if not self._responses:
            raise AssertionError(f"unexpected extra .open() call: {self.requested}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


def _patch_opener(monkeypatch, responses) -> _FakeOpener:
    opener = _FakeOpener(responses)
    monkeypatch.setattr(probe, "proxy_opener", lambda port: opener)
    return opener


def test_primary_source_succeeds(monkeypatch):
    opener = _patch_opener(monkeypatch, [b"fl=42f\nh=1.1.1.1\nip=8.8.8.8\n"])
    r = probe.probe_channel(8888)
    assert r["ok"] is True
    assert r["ip"] == "8.8.8.8"
    assert r["error"] is None
    assert r["latency_ms"] is not None
    assert len(opener.requested) == 1  # stopped at the first valid source


def test_primary_fails_then_fallback_succeeds(monkeypatch):
    opener = _patch_opener(
        monkeypatch,
        [urllib.error.URLError("refused"), b"1.1.1.1\n"],  # trace down, icanhazip ok
    )
    r = probe.probe_channel(8888)
    assert r["ok"] is True
    assert r["ip"] == "1.1.1.1"
    assert len(opener.requested) == 2  # did not query the third source
    assert "/cdn-cgi/trace" in opener.requested[0]


def test_malformed_and_non_ip_responses_are_skipped(monkeypatch):
    opener = _patch_opener(
        monkeypatch,
        [
            b"<html><body>error</body></html>\n",  # trace body without an `ip=` line
            b"not-an-ip\n",  # plain text, but not an address
            b"8.8.8.8\n",  # ipify returns a valid address -> wins
        ],
    )
    r = probe.probe_channel(8888)
    assert r["ok"] is True
    assert r["ip"] == "8.8.8.8"
    assert len(opener.requested) == 3


def test_all_sources_fail(monkeypatch):
    opener = _patch_opener(
        monkeypatch,
        [
            urllib.error.URLError("nope"),
            urllib.error.URLError("nope"),
            b"down for maintenance",  # reachable but not an IP
        ],
    )
    r = probe.probe_channel(8888)
    assert r["ok"] is False
    assert r["ip"] is None
    assert r["latency_ms"] is None
    # error is a short category; the verbose "all sources failed …" is in detail
    assert r["error"] == "no valid IP"
    assert "all IP sources failed" in r["detail"]
    for name in ("cloudflare-trace", "icanhazip", "ipify"):
        assert name in r["detail"]
    assert "maintenance" not in r["detail"]  # response bodies are not leaked
    assert len(opener.requested) == 3


def test_whitespace_around_valid_ip_is_trimmed(monkeypatch):
    _patch_opener(
        monkeypatch,
        [urllib.error.URLError("nope"), b"  1.1.1.1  \n"],
    )
    r = probe.probe_channel(8888)
    assert r["ok"] is True
    assert r["ip"] == "1.1.1.1"


def test_ipv6_exit_address_is_accepted(monkeypatch):
    _patch_opener(monkeypatch, [b"ip=2606:4700:4700::1111\n"])
    r = probe.probe_channel(8888)
    assert r["ok"] is True
    assert r["ip"] == "2606:4700:4700::1111"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("8.8.8.8", "8.8.8.8"),
        ("  8.8.8.8\n", "8.8.8.8"),
        ("2606:4700:4700::1111", "2606:4700:4700::1111"),
        ("10.0.0.1", None),  # private
        ("127.0.0.1", None),  # loopback
        ("192.168.1.1", None),  # private
        ("100.64.0.1", None),  # CGNAT
        ("198.18.0.1", None),  # FakeIP / benchmark sinkhole range
        ("203.0.113.1", None),  # RFC 5737 documentation range -> not global
        ("", None),
        (None, None),
        ("<html>error</html>", None),
        ("8.8.8.8 1.1.1.1", None),  # two tokens
    ],
)
def test_valid_public_ip(text, expected):
    assert probe._valid_public_ip(text) == expected


def test_response_body_is_bounded(monkeypatch):
    # An oversized body (a real echo is a few dozen bytes) is not an IP echo we
    # should parse — and we never slurp an unbounded body on the hot path.
    big = b"x" * (probe.MAX_BODY_BYTES + 100)
    opener = _patch_opener(monkeypatch, [big])
    r = probe.probe_channel(8888)
    assert r["ok"] is False
    assert len(opener.requested) == len(probe.IP_ECHO_SOURCES)  # all tried


# ---- one deadline across the primary probe and the IPv6 lookup ---------------
#
# The channel deadline has to be true end to end: a healthy v4 result used to
# be followed by an unbounded v6 lookup, so one channel could hold a probe-pool
# worker for 27s of a 15s budget. These tests reach every boundary by moving a
# fake clock — nothing here sleeps.


class _Clock:
    """``probe.time`` with a monotonic() the test advances by hand.

    Scoped to the probe module, so pytest's own timing (and every other
    module's) keeps using the real clock.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def __getattr__(self, name):
        return getattr(time, name)


class _TimedOpener:
    """An opener whose answers cost time: each entry is ``(seconds, item)``.

    Advancing the clock before answering is what makes a budget observable —
    the next request's cap has to reflect what this one spent.
    """

    def __init__(self, clock: _Clock, entries):
        self._clock = clock
        self._entries = list(entries)
        self.requested: list[str] = []
        self.timeouts: list[float] = []

    def open(self, req, timeout=None):
        self.requested.append(req.full_url)
        self.timeouts.append(timeout)
        if not self._entries:
            raise AssertionError(f"unexpected extra .open() call: {self.requested}")
        cost, item = self._entries.pop(0)
        self._clock.t += cost
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


def _patch_timed(monkeypatch, entries) -> tuple[_Clock, _TimedOpener]:
    clock = _Clock()
    opener = _TimedOpener(clock, entries)
    monkeypatch.setattr(probe, "time", clock)
    monkeypatch.setattr(probe, "proxy_opener", lambda port: opener)
    return clock, opener


V6_BODY = b"2606:4700:4700::1111\n"


def test_ipv6_lookup_is_capped_by_the_remaining_channel_budget(monkeypatch):
    """A fast primary leaves most of the budget; the v6 open is capped by the
    per-source timeout, not by the (larger) remainder."""
    _clock, opener = _patch_timed(monkeypatch, [(0.5, V6_BODY)])
    assert probe.probe_ipv6(8888, timeout=6, budget=14.0) == "2606:4700:4700::1111"
    assert opener.timeouts == [6]  # min(timeout, remaining) — timeout wins here


def test_ipv6_fallback_spends_only_what_the_budget_has_left(monkeypatch):
    """Partial budget across the v6 fallbacks: the second source is capped by
    the remainder the first one left, not by a fresh per-source timeout."""
    _clock, opener = _patch_timed(
        monkeypatch,
        [(3.0, urllib.error.URLError("no v6 route")), (0.2, V6_BODY)],
    )
    assert probe.probe_ipv6(8888, timeout=6, budget=4.0) == "2606:4700:4700::1111"
    assert opener.timeouts == [4.0, 1.0]  # 4 remaining, then 4 - 3


def test_ipv6_lookup_stops_when_the_budget_runs_out_mid_fallback(monkeypatch):
    """An exhausted budget ends the lookup instead of starting another source."""
    _clock, opener = _patch_timed(
        monkeypatch, [(5.0, urllib.error.URLError("no v6 route"))]
    )
    assert probe.probe_ipv6(8888, timeout=6, budget=5.0) is None
    assert len(opener.requested) == 1  # the second source was never attempted


@pytest.mark.parametrize("budget", [0.0, -3.0], ids=["exact-boundary", "overspent"])
def test_ipv6_lookup_is_skipped_when_no_budget_remains(monkeypatch, budget):
    """At (or past) the boundary the supplementary lookup does not run at all —
    a primary probe that consumed the whole deadline must not be extended."""
    _clock, opener = _patch_timed(monkeypatch, [])
    assert probe.probe_ipv6(8888, timeout=6, budget=budget) is None
    assert opener.requested == []


def _v6_channel(port: int = 8888):
    """A channel the engine considers v6-capable: a provider that carries v6
    and a global v6 interface address of its own."""
    from alle.state import Channel

    return Channel(
        provider="protonvpn",
        id="wg_us_1",
        port=port,
        wg={"address": ["10.5.0.2/32", "2a07:b944::2:2/128"]},
    )


def _probe_one(ch):
    from alle.engine import Engine

    return Engine._probe_one(ch)


def test_a_fast_primary_leaves_the_ipv6_lookup_its_remaining_budget(monkeypatch):
    _clock, opener = _patch_timed(
        monkeypatch,
        [(0.4, b"ip=8.8.8.8\n"), (0.3, V6_BODY)],  # primary, then the v6 echo
    )
    result = _probe_one(_v6_channel())
    assert result["ok"] is True
    assert result["ip"] == "8.8.8.8"
    assert result["ipv6"] == "2606:4700:4700::1111"
    # The v6 open is capped by its own 6s timeout while ~14.6s of budget remain.
    assert opener.timeouts == [10, 6]


def test_a_slow_primary_leaves_the_ipv6_lookup_a_partial_budget(monkeypatch):
    """The v6 lookup inherits the remainder, so the channel total stays inside
    CHANNEL_DEADLINE instead of adding a fresh per-source timeout on top."""
    _clock, opener = _patch_timed(
        monkeypatch,
        [
            (8.0, urllib.error.URLError("refused")),  # primary source 1 burns 8s
            (3.5, b"1.1.1.1\n"),  # source 2 answers at 11.5s
            (0.2, V6_BODY),  # v6 echo, with 3.5s left of 15
        ],
    )
    result = _probe_one(_v6_channel())
    assert result["ok"] is True and result["ipv6"] == "2606:4700:4700::1111"
    assert opener.timeouts == [10, 7.0, 3.5]  # each capped by what was left


def test_an_exhausted_deadline_skips_the_ipv6_lookup_entirely(monkeypatch):
    """A healthy result that consumed the whole deadline must not be extended:
    the verdict stands, the v6 exit is simply unknown."""
    _clock, opener = _patch_timed(
        monkeypatch,
        [
            (10.0, urllib.error.URLError("refused")),
            (5.0, b"1.1.1.1\n"),  # healthy exactly at the 15s boundary
        ],
    )
    result = _probe_one(_v6_channel())
    assert result["ok"] is True  # IPv6 never changes a healthy primary verdict
    assert result["ipv6"] is None
    assert len(opener.requested) == 2  # no v6 request was made


def test_a_v4_only_channel_never_runs_the_ipv6_lookup(monkeypatch):
    from alle.state import Channel

    _clock, opener = _patch_timed(monkeypatch, [(0.1, b"ip=8.8.8.8\n")])
    v4_only = Channel(provider="nordvpn", id="wg_us_1", port=8888, wg=WG_V4_ONLY)
    result = _probe_one(v4_only)
    assert result["ok"] is True and "ipv6" not in result
    assert len(opener.requested) == 1


WG_V4_ONLY = {"address": ["10.5.0.2/32"]}


def test_channel_deadline_stops_trying_remaining_sources(monkeypatch):
    """The overall channel deadline caps the pass across all sources, so once
    it's exhausted no further sources are tried — a dead channel can never cost
    ``sources × per-request timeout`` wall clock."""
    opener = _patch_opener(
        monkeypatch,
        [urllib.error.URLError("refused"), urllib.error.URLError("refused")],
    )

    # Controlled clock: time starts at 0; after the first source fails, the
    # loop's monotonic read jumps past the deadline, so source 2 is never
    # reached.
    clock = {"t": 0.0}
    real_monotonic = probe.time.monotonic

    def fake_monotonic():
        clock["t"] += 0.001
        return clock["t"]

    monkeypatch.setattr(probe.time, "monotonic", fake_monotonic)
    r = probe.probe_channel(8888, deadline=0.001)
    monkeypatch.setattr(probe.time, "monotonic", real_monotonic)
    assert r["ok"] is False
    # the deadline firing is recorded in the verbose detail (the short error is
    # the failure category — "proxy closed" here, since the first source refused)
    assert "deadline" in r["detail"]
    # only the first source was attempted before the deadline ran out
    assert len(opener.requested) < len(probe.IP_ECHO_SOURCES)
