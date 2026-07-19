"""The browser harness's fixture daemon: the real control server, synthetic state.

Runs the exact stdlib API/Web-UI server the daemon runs (`build_server()` —
same handlers, same auth, same CSP headers) inside a hermetic temp ALLE_HOME
seeded with deterministic providers/channels/rulesets. Nothing touches the
network or real credentials:

* ``daemon.ensure_running`` is a no-op — mutations edit state, no runtime spawns;
* ``service.test`` is replaced with a canned implementation that emits the same
  row/terminal shapes the real one does (with small delays, so the streamed
  speed-row UI is observably incremental);
* sing-box is never started, so status truthfully reports "stopped".

A second loopback HTTP listener (the *control* port — test-only, unauthenticated,
never part of the product) lets the Playwright side drive the fixture:

  GET  /login-url    -> {"url": <single-use tokenized login URL>}
  POST /reset        -> restore the seeded state.json (cookies stay valid),
                        disarm+release the stream gate
  GET  /info         -> {"app": <canonical base URL>}
  POST /gate/arm     -> the next speed run emits its first row, then BLOCKS
                        before each further row until /gate/release
  POST /gate/release -> unblock an armed run (its remaining rows all flow)

The gate is how streaming tests stay deterministic: "row 1 rendered while
row 2 is pending" is a guaranteed state while armed — never a timing window —
and an armed-but-never-released run is guaranteed still in flight whenever a
lifetime test navigates away (a 15s server-side timeout backstops a test that
dies without releasing).

On start, prints exactly one JSON line on stdout:
  {"app": "http://alle-<rand>.localhost:<port>", "control": "http://127.0.0.1:<port>"}
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="alle-browser-fixture-"))
import os  # noqa: E402

os.environ["ALLE_HOME"] = str(HOME)
os.environ.pop("ALLE_API_LISTEN", None)
os.environ.pop("ALLE_API_SECRET", None)
os.environ.pop("ALLE_API_SECRET_FILE", None)

from alle import credentials, daemon, service  # noqa: E402
from alle.api import server as api_server  # noqa: E402
from alle.state import Store  # noqa: E402

daemon.ensure_running = lambda: None  # the fixture never spawns a runtime
# Bundle validation may consult a provider's location list; that is a network
# call in real life, so the fixture validates without location checks.
service._bundle_location_lookup = lambda: (None, [])


def _wg(host: str) -> dict:
    # syntactically valid 32-byte keys (base64 of 0x00*32 / 0x01*32) so bundle
    # round-trips validate; never used to dial anything
    return {
        "private_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "address": ["10.5.0.2/32"],
        "peer": {
            "public_key": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
            "endpoint_host": host,
            "endpoint_port": 51820,
            "preshared_key": None,
            "allowed_ips": ["0.0.0.0/0", "::/0"],
            "keepalive": 25,
        },
    }


def seed() -> None:
    store = Store.load()
    store.add_provider("nordvpn")
    # a fake token so exports round-trip validate (which requires the
    # credential inside the bundle) — never used against any real API
    credentials.set_("nordvpn", {"token": "0" * 64})
    us = store.add_channel("nordvpn", "United States", "New York", _wg("1.2.3.4"))
    de = store.add_channel("nordvpn", "Germany", "", _wg("5.6.7.8"))
    store.set_probe(
        "nordvpn", us.id, {"ok": True, "ip": "203.0.113.7", "latency_ms": 42}
    )
    store.set_probe("nordvpn", de.id, {})
    store.ensure_router_port()
    store.create_ruleset(
        "Streaming",
        f"nordvpn/{us.id}",
        [("domain_suffix", "netflix.com"), ("domain_suffix", "hulu.com")],
    )
    store.create_ruleset("Home lab", "direct", [("ip_cidr", "203.0.113.0/24")])
    store.create_ruleset("Trackers", "block", [("domain_suffix", "tracker.example")])


# The armed stream gate: a fresh Event per /gate/arm, set by /gate/release
# (and by /reset, so a straggling run can never block the next test).
_gate: dict = {"event": None}


def fake_test(
    speed: bool = False,
    channel: str | None = None,
    progress=None,
    on_row=None,
    on_begin=None,
    cancel=None,
) -> dict:
    """Deterministic stand-in for service.test — same shapes, no network."""
    gate = _gate["event"]  # captured per run: a mid-run re-arm can't swap it
    store = Store.load()
    channels = [c for c in store.channels() if c.enabled]
    if channel is not None:
        cid = channel.rpartition("/")[2]
        channels = [c for c in channels if c.id == cid]
    rows = []
    for i, c in enumerate(channels):
        if cancel and cancel():
            break
        if speed and i:
            # between rows only (the first always flows immediately): armed
            # runs block for the explicit release, unarmed runs just pace a
            # little so streaming stays observable when watched by hand
            if gate is not None:
                gate.wait(timeout=15)
            else:
                time.sleep(0.2)
        row = {
            "provider": c.provider,
            "name": c.id,
            "label": c.label,
            "enabled": True,
            "ip": f"203.0.113.{10 + i}",
            "latency_ms": 20 + 7 * i,
            "sent": 1_000_000 * (i + 1),
            "received": 5_000_000 * (i + 1),
        }
        if speed:
            row["speed_result"] = {
                "download_bps": 100_000_000 - 10_000_000 * i,
                "upload_bps": 40_000_000 - 5_000_000 * i,
            }
        rows.append(row)
        if on_row:
            on_row(row)
    return {
        "probed": True,
        "reason": None,
        "speed": speed,
        "filter": channel,
        "running": True,
        "channel_count": len(rows),
        "healthy_count": len(rows),
        "failed_count": 0,
        "channels": rows,
    }


service.test = fake_test


def main() -> None:
    seed()
    snapshot = (HOME / "state.json").read_bytes()

    httpd = api_server.build_server()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    api = api_server.control_api()

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep stdout to the one JSON line
            pass

        def _json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/login-url":
                return self._json({"url": api_server.mint_login_url()})
            if self.path == "/info":
                return self._json({"app": f"http://{api_server._canonical_host(api)}"})
            self.send_error(404)

        def do_POST(self):
            if self.path == "/reset":
                (HOME / "state.json").write_bytes(snapshot)
                gate, _gate["event"] = _gate["event"], None
                if gate is not None:
                    gate.set()  # never let a straggling armed run block a test
                return self._json({"ok": True})
            if self.path == "/gate/arm":
                _gate["event"] = threading.Event()
                return self._json({"ok": True})
            if self.path == "/gate/release":
                gate = _gate["event"]
                if gate is not None:
                    gate.set()
                return self._json({"ok": True})
            self.send_error(404)

    control = ThreadingHTTPServer(("127.0.0.1", 0), Control)
    threading.Thread(target=control.serve_forever, daemon=True).start()

    print(
        json.dumps(
            {
                "app": f"http://{api_server._canonical_host(api)}",
                "control": f"http://127.0.0.1:{control.server_address[1]}",
            }
        ),
        flush=True,
    )
    try:
        # Lifetime = the spawner's: stdin is a pipe from the Playwright worker,
        # so EOF means the parent is gone (including SIGKILL, where no signal
        # would ever arrive) — no orphaned fixture can outlive its test run.
        sys.stdin.read()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    main()
