"""The desktop companion's thin API client — exercised against a real control
server on an ephemeral loopback port (same harness as test_webui_server), so
the health challenge, Bearer auth, and version-skew degradation are all real."""

from __future__ import annotations

import json
import urllib.request
from threading import Thread

import pytest

from alle import companion
from alle.state import Store
from alle.webui import server


@pytest.fixture
def live(monkeypatch):
    """A running control server + a CompanionClient pointed at it. Yields the
    client. Daemon spawning is neutralized so lifecycle calls stay hermetic."""
    monkeypatch.setattr("alle.service.daemon.ensure_running", lambda: None)
    Store.load().add_provider("nordvpn")
    httpd = server.build_server()
    Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True).start()
    try:
        yield companion.CompanionClient()
    finally:
        httpd.shutdown()


def test_health_ok_only_when_our_daemon_is_behind_the_port(live):
    assert live.health_ok() is True


def test_health_ok_false_when_no_endpoint(monkeypatch, tmp_path):
    # No control_api.json at all → not configured → health is False, no raise.
    client = companion.CompanionClient()
    monkeypatch.setattr(server, "_config_path", lambda: tmp_path / "missing.json")
    assert client.health_ok() is False


def test_status_and_tray_state_projection(live):
    st = live.tray_state()
    assert st.running is False  # sing-box not started in the test
    assert st.tun is False and st.killswitch is False
    assert st.channel_count == 0
    assert st.channel_summary == "no channels"


def test_tray_state_survives_unknown_and_missing_fields(live, monkeypatch):
    # Version skew: a daemon returning an unfamiliar shape must not crash the
    # tray render — every field is read defensively.
    def weird_status():
        return {
            "running": True,
            "router": {"tun": True, "killswitch": True, "port": 8080},
            "channels": [
                {"provider": "nordvpn", "state": "Active"},
                {"provider": "nordvpn", "state": "Failed"},
            ],
            "daemon": {"installed_version": "9.9.9"},
            "surprise_field": {"from": "a newer daemon"},
        }

    monkeypatch.setattr(live, "status", weird_status)
    st = live.tray_state()
    assert st.running and st.tun and st.killswitch
    assert st.channel_summary == "1/2 healthy"
    assert st.provider_count == 1
    assert st.installed_version == "9.9.9"
    assert st.router_port == 8080


def test_tun_toggle_round_trips_through_the_api(live, monkeypatch):
    # Privileged path stubbed so the gate permits the flip in-test.
    monkeypatch.setattr("alle.service._singbox_has_net_admin", lambda: True)
    out = live.set_tun(True)
    assert out["router"]["tun"] is True
    assert live.tray_state().tun is True
    out = live.set_tun(False)
    assert out["router"]["tun"] is False


def test_killswitch_toggle_round_trips(live):
    assert live.set_killswitch(True)["router"]["killswitch"] is True
    assert live.tray_state().killswitch is True


def test_control_api_error_surfaces_verbatim(live, monkeypatch):
    # An unprivileged tun enable returns a 400 the tray shows verbatim.
    monkeypatch.setattr("alle.service._singbox_has_net_admin", lambda: False)
    monkeypatch.setattr("alle.service.daemon.daemon_info", lambda: None)
    monkeypatch.setattr("os.geteuid", lambda: 501)
    with pytest.raises(companion.CompanionError, match="privileged helper"):
        live.set_tun(True)


def test_unknown_endpoint_reads_as_feature_absent(live, monkeypatch):
    # The version-skew contract: a 404 becomes a typed "not available" error,
    # not a crash — the tray can grey out a feature the daemon lacks.
    orig = live._request

    def to_missing(method, path, body=None):
        return orig(method, "does-not-exist", body)

    monkeypatch.setattr(live, "_request", to_missing)
    with pytest.raises(companion.CompanionError, match="not available"):
        live.status()


def test_unreachable_daemon_raises_daemon_unavailable(monkeypatch, tmp_path):
    # A control_api.json pointing at a dead port → DaemonUnavailable, so the
    # tray shows a disconnected state and keeps polling.
    cfg = tmp_path / "control_api.json"
    cfg.write_text(
        json.dumps(
            {
                "address": "127.0.0.1:1",  # nothing listens on port 1
                "secret": "x" * 64,
                "host": "alle-dead.localhost",
            }
        )
    )
    monkeypatch.setattr(server, "_config_path", lambda: cfg)
    client = companion.CompanionClient(timeout=1.0)
    with pytest.raises(companion.DaemonUnavailable):
        client.status()


def test_secret_never_sent_to_a_squatted_port(monkeypatch, tmp_path):
    # A foreign process on the recorded contract port answers /health but
    # cannot forge the HMAC proof: the client must bail with
    # DaemonUnavailable having sent only the challenge — never the Bearer.
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits = []  # (path, had_authorization_header) per request the squatter saw

    class Squatter(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append((self.path, bool(self.headers.get("Authorization"))))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "ok", "proof": "forged"}')

        def log_message(self, *args):
            pass  # keep pytest output clean

    httpd = HTTPServer(("127.0.0.1", 0), Squatter)
    Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        cfg = tmp_path / "control_api.json"
        cfg.write_text(
            json.dumps(
                {
                    "address": f"127.0.0.1:{httpd.server_port}",
                    "secret": "x" * 64,
                    "host": "alle-squat.localhost",
                }
            )
        )
        monkeypatch.setattr(server, "_config_path", lambda: cfg)
        client = companion.CompanionClient(timeout=1.0)
        assert client.health_ok() is False
        with pytest.raises(companion.DaemonUnavailable, match="health challenge"):
            client.status()
    finally:
        httpd.shutdown()
    assert any(path.startswith("/health") for path, _ in hits)  # challenge ran
    assert not any(had_auth for _, had_auth in hits)  # the secret never left


def test_client_sends_bearer_and_host(live, monkeypatch):
    # The request carries the Bearer secret and the loopback Host — without
    # them the hardened server would 401/403. A successful status() proves both.
    captured = {}
    orig = urllib.request.urlopen

    def spy(req, *a, **k):
        captured["auth"] = req.get_header("Authorization")
        captured["host"] = req.get_header("Host")
        return orig(req, *a, **k)

    monkeypatch.setattr(companion.urllib.request, "urlopen", spy)
    live.status()
    assert captured["auth"].startswith("Bearer ")
    assert captured["host"].startswith("127.0.0.1:")  # loopback address:port
