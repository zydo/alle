"""Speed-test endpoint fallback: each metric tries its endpoint list in order,
a dead endpoint costs one failure (not a retry storm), and only when every
endpoint fails does the metric come back None. Transfers are driven through a
fake opener so tests stay hermetic and offline."""

from __future__ import annotations

import pytest

from alle import throughput


class _Resp:
    def __init__(self, body: bytes):
        self._body = body
        self._pos = 0

    def read(self, n: int | None = None) -> bytes:
        if n is None:
            n = len(self._body)
        chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Fake urllib opener: url -> body bytes, or an Exception to raise."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        result = self.routes.get(url, OSError("no route"))
        if isinstance(result, Exception):
            raise result
        return _Resp(result)


@pytest.fixture(autouse=True)
def _fast_download(monkeypatch):
    monkeypatch.setattr(throughput, "DOWNLOAD_SECONDS", 0.02)
    monkeypatch.setattr(throughput, "UPLOAD_BYTES", 1024)


def test_latency_uses_primary_when_healthy():
    opener = _Opener({throughput.LATENCY_URLS[0]: b""})
    assert throughput._latency_ms(opener, timeout=1) is not None
    assert set(opener.calls) == {throughput.LATENCY_URLS[0]}


def test_latency_falls_back_when_primary_dead():
    opener = _Opener(
        {
            throughput.LATENCY_URLS[0]: OSError("down"),
            throughput.LATENCY_URLS[1]: b"",
        }
    )
    assert throughput._latency_ms(opener, timeout=1) is not None
    # the dead primary was abandoned after one attempt, not sampled repeatedly
    assert opener.calls.count(throughput.LATENCY_URLS[0]) == 1
    assert opener.calls.count(throughput.LATENCY_URLS[1]) == throughput.LATENCY_SAMPLES


def test_download_falls_back_when_primary_dead():
    opener = _Opener(
        {
            throughput.DOWNLOAD_URLS[0]: OSError("down"),
            throughput.DOWNLOAD_URLS[1]: b"x" * 4096,
        }
    )
    bps = throughput._download_bps(opener, timeout=1)
    assert bps is not None and bps > 0
    assert throughput.DOWNLOAD_URLS[1] in opener.calls


def test_upload_falls_back_when_primary_dead():
    opener = _Opener(
        {
            throughput.UPLOAD_URLS[0]: OSError("down"),
            throughput.UPLOAD_URLS[1]: b"",
        }
    )
    assert throughput._upload_bps(opener, timeout=1) is not None
    assert opener.calls == [throughput.UPLOAD_URLS[0], throughput.UPLOAD_URLS[1]]


def test_all_endpoints_dead_returns_none():
    opener = _Opener({})  # every URL raises
    assert throughput._latency_ms(opener, timeout=1) is None
    assert throughput._download_bps(opener, timeout=1) is None
    assert throughput._upload_bps(opener, timeout=1) is None


def test_cancel_aborts_the_download_loop_early():
    # A streaming client that disconnects should not keep driving transfers:
    # once cancel() is true the download loop stops reading (raising Cancelled,
    # which run() catches) — well before the DOWNLOAD_SECONDS time cap.
    import time

    endless = _Opener(
        {throughput.DOWNLOAD_URLS[0]: b"\x00" * (20 * 65536)}  # large but finite
    )
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1  # flip true after the first poll

    start = time.monotonic()
    with pytest.raises(throughput.Cancelled):
        throughput._download_bps(endless, timeout=2, cancel=cancel)
    assert time.monotonic() - start < throughput.DOWNLOAD_SECONDS  # bailed early
    assert calls["n"] >= 2  # cancel was actually polled
