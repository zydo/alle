"""On-demand speed test for a channel, run through its local proxy.

Where the heartbeat :mod:`probe` only asks "is this tunnel alive and what's its
exit IP", this drives a real bulk transfer through the channel to estimate
download/upload throughput plus a latency floor. It is strictly on-demand (the
``alle test --speed`` command) and never runs on the daemon's hot path.

Method — everything goes through the channel's ``mixed`` proxy via a urllib
``ProxyHandler`` (same technique as the probe):

* **latency** — the min round trip of a few tiny GETs (min, not mean, to shed
  scheduler/one-off jitter). It bundles TCP+TLS setup so it reads higher than raw
  ICMP ping, but is consistent across channels, so the *ordering* is meaningful.
* **download** — read a large object for up to ``DOWNLOAD_SECONDS``, then stop;
  throughput is bytes read / elapsed. Time-bounding keeps a fast link honest and a
  slow one quick.
* **upload** — POST a fixed payload of zeros to an endpoint that discards it.

Each metric has an ordered list of endpoints and the first one that works wins —
the same multi-source approach as the heartbeat probe, so one retired or flaky
endpoint degrades to a backup instead of reporting "-". Primaries: download uses
Cloudflare; upload uses a plain upload sink because Cloudflare's ``__up`` throttles
proxied POSTs heavily (it is kept only as the last-resort backup). Upload is also
naturally capped by the machine's own uplink (shared by every channel), so all
channels tend to converge there.
"""

from __future__ import annotations

import time
import urllib.request
from collections.abc import Callable

from alle.probe import proxy_opener

# 50 MB per fetch — Cloudflare rejects much larger single requests, so the
# download test loops fetches until DOWNLOAD_SECONDS is up rather than asking
# for one huge object. Backups are large static objects on independent
# infrastructure (OVH, Tele2).
DOWNLOAD_URLS = [
    "https://speed.cloudflare.com/__down?bytes=50000000",
    "https://proof.ovh.net/files/100Mb.dat",
    "http://speedtest.tele2.net/100MB.zip",  # noqa: S5332
]
# Endpoints that accept and discard a POST body.
UPLOAD_URLS = [
    "http://speedtest.tele2.net/upload.php",  # noqa: S5332
    "https://librespeed.org/backend/empty.php",
    "https://speed.cloudflare.com/__up",  # throttles proxied POSTs; last resort
]
# Tiny objects on independent infrastructure (Cloudflare, Google, Mozilla).
LATENCY_URLS = [
    "https://speed.cloudflare.com/__down?bytes=1",
    "https://www.gstatic.com/generate_204",
    "http://detectportal.firefox.com/success.txt",  # noqa: S5332
]

DOWNLOAD_SECONDS = 5.0  # read the download stream for at most this long
UPLOAD_BYTES = 8 * 1024 * 1024  # payload pushed for the upload test
LATENCY_SAMPLES = 5  # tiny requests; the fastest one wins
_CHUNK = 65536
_USER_AGENT = "alle-speedtest/1"


class Cancelled(Exception):
    """Raised to abort a throughput run mid-transfer (e.g. the streaming client
    disconnected). Caught by :func:`run`, which returns what it has so far."""


def _latency_ms(
    opener, timeout: int, cancel: Callable[[], bool] | None = None
) -> float | None:
    """Min round trip against the first latency endpoint that answers.

    A failed sample abandons that endpoint for the next one (rather than
    retrying a dead host ``LATENCY_SAMPLES`` times), so a broken primary costs
    one timeout, not five.
    """
    for url in LATENCY_URLS:
        if cancel and cancel():
            raise Cancelled
        best: float | None = None
        for _ in range(LATENCY_SAMPLES):
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            start = time.monotonic()
            try:
                with opener.open(req, timeout=timeout) as r:  # noqa: S310 (loopback proxy)
                    r.read()
            except Exception:  # noqa: BLE001 — endpoint not usable; try the next source
                break
            ms = (time.monotonic() - start) * 1000
            best = ms if best is None else min(best, ms)
        if best is not None:
            return round(best, 1)
    return None


def _download_bps(
    opener, timeout: int, cancel: Callable[[], bool] | None = None
) -> float | None:
    for url in DOWNLOAD_URLS:
        if cancel and cancel():
            raise Cancelled
        total = 0
        start = time.monotonic()
        try:
            while time.monotonic() - start < DOWNLOAD_SECONDS:
                req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
                with opener.open(req, timeout=timeout) as r:  # noqa: S310 (loopback proxy)
                    while time.monotonic() - start < DOWNLOAD_SECONDS:
                        if cancel and cancel():
                            raise Cancelled
                        chunk = r.read(_CHUNK)
                        if not chunk:
                            break  # object exhausted; loop fetches another
                        total += len(chunk)
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001
            if total == 0:
                continue  # nothing flowed from this endpoint — try the next
            # died mid-stream: measure what we got rather than discarding it
        elapsed = time.monotonic() - start
        if total and elapsed > 0:
            return total * 8 / elapsed
    return None


def _upload_bps(
    opener, timeout: int, cancel: Callable[[], bool] | None = None
) -> float | None:
    payload = b"\0" * UPLOAD_BYTES
    for url in UPLOAD_URLS:
        if cancel and cancel():
            raise Cancelled
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/octet-stream",
            },
        )
        start = time.monotonic()
        try:
            with opener.open(req, timeout=timeout) as r:  # noqa: S310 (loopback proxy)
                r.read()
        except Exception:  # noqa: BLE001 — endpoint not usable; try the next sink
            continue
        elapsed = time.monotonic() - start
        if elapsed > 0:
            return len(payload) * 8 / elapsed
    return None


def run(
    port: int,
    timeout: int = 60,
    progress=None,
    measure_latency: bool = True,
    cancel: Callable[[], bool] | None = None,
) -> dict:
    """Measure latency + download + upload through the proxy on ``port``.

    Returns ``{"latency_ms", "download_bps", "upload_bps"}``; any metric whose
    transfer failed (proxy down, endpoint unreachable) comes back ``None``.

    ``progress`` is an optional callback invoked with the phase name
    (``"latency"`` → ``"download"`` → ``"upload"``) just before each phase starts,
    so a caller can drive a live indicator during the (several-second) run.

    ``measure_latency`` skips the latency phase when the caller already has a
    fresher probe latency (``alle test --speed`` probes first) — the returned
    ``latency_ms`` is then ``None`` for the caller to fill in, avoiding a redundant
    round of tiny requests.

    ``cancel`` is an optional predicate polled between and within phases; when it
    returns true the run aborts (a streaming client that disconnected should not
    keep driving transfers). A cancelled phase yields ``None`` for that metric.
    """
    opener = proxy_opener(port)

    def _phase(name: str) -> None:
        if progress is not None:
            progress(name)

    latency = download = upload = None
    try:
        if measure_latency:
            _phase("latency")
            latency = _latency_ms(opener, timeout, cancel)
        _phase("download")
        download = _download_bps(opener, timeout, cancel)
        _phase("upload")
        upload = _upload_bps(opener, timeout, cancel)
    except Cancelled:
        pass  # return whatever phases completed before the cancel
    return {"latency_ms": latency, "download_bps": download, "upload_bps": upload}
