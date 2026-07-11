"""Heartbeat probe: IP-echo source fallback and response validation.

The probe routes through a channel's loopback proxy, so these tests stand in for
the real proxy with a canned opener that dispenses responses (or raises) per
``.open()`` call, in source order: cloudflare-trace, icanhazip, ipify.
"""

from __future__ import annotations

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
    assert "all IP sources failed" in r["error"]
    for name in ("cloudflare-trace", "icanhazip", "ipify"):
        assert name in r["error"]
    assert "maintenance" not in r["error"]  # response bodies are not leaked
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
    assert "deadline" in r["error"]
    # only the first source was attempted before the deadline ran out
    assert len(opener.requested) < len(probe.IP_ECHO_SOURCES)
