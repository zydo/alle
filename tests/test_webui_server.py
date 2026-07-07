"""Web UI control server: the login page, auth on the API, the loopback
hardening (Host/Origin), and the one-time login → cookie exchange — exercised
against a real server on an ephemeral loopback port."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from threading import Thread

import pytest

from alle import service
from alle.state import Store
from alle.webui import auth, server

WG = {
    "private_key": "PRIV=",
    "address": ["10.5.0.2/32"],
    "peer": {
        "public_key": "PUB=",
        "endpoint_host": "1.2.3.4",
        "endpoint_port": 51820,
        "preshared_key": None,
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "keepalive": 25,
    },
}


@pytest.fixture
def live():
    """A running control server. Yields ``(base_url, secret)``."""
    Store.load().add_provider("nordvpn")  # a little content for /status
    httpd = server.build_server()
    Thread(target=httpd.serve_forever, daemon=True).start()
    api = server.control_api()
    try:
        yield f"http://{api['address']}", api["secret"]
    finally:
        httpd.shutdown()


def _req(url, *, method="GET", headers=None, data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(r) as resp:  # noqa: S310 (loopback test)
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_root_serves_login_page_when_unauthenticated(live):
    base, _ = live
    status, body, _ = _req(base + "/")
    assert status == 200 and b"sign in" in body.lower()


def test_api_requires_auth(live):
    base, _ = live
    status, _, _ = _req(base + "/api/v1/status")
    assert status == 401


def test_api_accepts_bearer_and_returns_status(live):
    base, secret = live
    status, body, _ = _req(
        base + "/api/v1/status", headers={"Authorization": f"Bearer {secret}"}
    )
    assert status == 200
    data = json.loads(body)
    assert "running" in data and "router" in data and "channels" in data


def test_bad_host_is_refused_dns_rebinding(live):
    base, secret = live
    status, _, _ = _req(
        base + "/api/v1/status",
        headers={"Authorization": f"Bearer {secret}", "Host": "evil.example.com"},
    )
    assert status == 403


def test_loopback_forwarded_host_is_allowed_but_origin_must_match(live):
    base, secret = live
    status, _, _ = _req(
        base + "/api/v1/status",
        headers={"Authorization": f"Bearer {secret}", "Host": "127.0.0.1:8080"},
    )
    assert status == 200

    status, _, _ = _req(
        base + "/api/v1/login",
        method="POST",
        headers={"Origin": "http://127.0.0.1:8080", "Host": "127.0.0.1:8080"},
        data={"token": secret},
    )
    assert status == 200

    status, _, _ = _req(
        base + "/api/v1/login",
        method="POST",
        headers={"Origin": "http://127.0.0.1:8081", "Host": "127.0.0.1:8080"},
        data={"token": secret},
    )
    assert status == 403


def test_mutation_requires_same_origin_csrf(live):
    base, secret = live
    # cross-origin POST is refused even with a valid token in the body
    status, _, _ = _req(
        base + "/api/v1/login",
        method="POST",
        headers={"Origin": "http://evil.example.com"},
        data={"token": secret},
    )
    assert status == 403


def test_login_with_secret_sets_httponly_samesite_cookie(live):
    base, secret = live
    status, _, headers = _req(
        base + "/api/v1/login",
        method="POST",
        headers={"Origin": base},
        data={"token": secret},
    )
    assert status == 200
    cookie = headers.get("Set-Cookie", "")
    assert "alle_session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_one_time_token_redirects_and_is_single_use(live):
    base, secret = live
    token = auth.mint_login_token(secret)

    # first use: 302 to a clean URL with the session cookie set
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(base + f"/?token={token}")  # noqa: S310
        status = None
    except urllib.error.HTTPError as e:
        status, cookie = e.code, e.headers.get("Set-Cookie", "")
    assert status == 302 and "alle_session=" in cookie

    # second use of the same token: no redirect — it just serves the login page
    st, body, _ = _req(base + f"/?token={token}")
    assert st == 200 and b"sign in" in body.lower()


def test_unknown_asset_is_404(live):
    base, secret = live
    status, _, _ = _req(
        base + "/nope.js", headers={"Authorization": f"Bearer {secret}"}
    )
    assert status == 404


def test_path_traversal_is_blocked(live):
    base, _ = live
    status, _, _ = _req(base + "/..%2f..%2fetc%2fpasswd")
    assert status in (403, 404)  # never serves outside the asset dir


def test_missing_page_asset_is_a_clear_error_not_blank(live, monkeypatch):
    base, _ = live
    monkeypatch.setattr(server, "_asset", lambda name: None)  # simulate missing assets
    status, body, _ = _req(base + "/")
    assert status == 500 and b"assets are missing" in body  # never a silent blank page


# ---- /api/v1 providers + channels (Phase 4.2) ----

import base64  # noqa: E402


def _bearer(secret, extra=None):
    h = {"Authorization": f"Bearer {secret}"}
    if extra:
        h.update(extra)
    return h


def _conf():
    key = base64.b64encode(b"k" * 32).decode()
    pub = base64.b64encode(b"p" * 32).decode()
    return (
        f"[Interface]\nPrivateKey = {key}\nAddress = 10.0.0.2/32\n"
        f"[Peer]\nPublicKey = {pub}\nEndpoint = 192.0.2.1:51820\n"
    )


def test_providers_and_catalog_are_listed(live):
    base, secret = live
    st, body, _ = _req(base + "/api/v1/providers", headers=_bearer(secret))
    assert st == 200 and any(
        p["provider"] == "nordvpn" for p in json.loads(body)["providers"]
    )
    st, body, _ = _req(base + "/api/v1/providers/catalog", headers=_bearer(secret))
    provs = {p["provider"] for p in json.loads(body)["providers"]}
    assert st == 200 and {"nordvpn", "protonvpn"} <= provs


def test_add_config_provider_then_channel_via_upload(live):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    st, _, _ = _req(
        base + "/api/v1/providers",
        method="POST",
        headers=origin,
        data={"provider": "protonvpn"},
    )
    assert st == 200
    st, body, _ = _req(
        base + "/api/v1/channels",
        method="POST",
        headers=origin,
        data={
            "provider": "protonvpn",
            "conf_name": "wg-US-CA-9.conf",
            "conf_text": _conf(),
            "label": "West",
        },
    )
    assert st == 200
    ch = json.loads(body)["channel"]
    assert ch["label"] == "West" and ch["id"] == "wg_us_ca_9"


def test_relabel_channel(live):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    _req(
        base + "/api/v1/providers",
        method="POST",
        headers=origin,
        data={"provider": "protonvpn"},
    )
    _req(
        base + "/api/v1/channels",
        method="POST",
        headers=origin,
        data={
            "provider": "protonvpn",
            "conf_name": "wg-jp-1.conf",
            "conf_text": _conf(),
        },
    )
    st, body, _ = _req(
        base + "/api/v1/channels/protonvpn/wg_jp_1/label",
        method="POST",
        headers=origin,
        data={"label": "Tokyo"},
    )
    assert st == 200 and json.loads(body)["label"] == "Tokyo"


def test_remove_channel_blocked_by_rule_returns_verbatim_message(live):
    from alle.state import Store

    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    _req(
        base + "/api/v1/providers",
        method="POST",
        headers=origin,
        data={"provider": "protonvpn"},
    )
    _req(
        base + "/api/v1/channels",
        method="POST",
        headers=origin,
        data={
            "provider": "protonvpn",
            "conf_name": "wg-de-1.conf",
            "conf_text": _conf(),
        },
    )
    Store.load().add_rule("domain", "x.com", "protonvpn/wg_de_1")

    st, body, _ = _req(
        base + "/api/v1/channels/protonvpn/wg_de_1", method="DELETE", headers=origin
    )
    assert st == 400
    assert "routing rules still reference" in json.loads(body)["error"]

    Store.load().remove_rules(["r1"])
    st, _, _ = _req(
        base + "/api/v1/channels/protonvpn/wg_de_1", method="DELETE", headers=origin
    )
    assert st == 200


def test_add_unknown_provider_is_a_400(live):
    base, secret = live
    st, body, _ = _req(
        base + "/api/v1/providers",
        method="POST",
        headers={"Origin": base, "Authorization": f"Bearer {secret}"},
        data={"provider": "nope"},
    )
    assert st == 400 and "unknown provider" in json.loads(body)["error"]


def test_api_mutation_still_requires_auth(live):
    base, _ = live
    # same-origin but no bearer/cookie → 401
    st, _, _ = _req(
        base + "/api/v1/providers",
        method="POST",
        headers={"Origin": base},
        data={"provider": "protonvpn"},
    )
    assert st == 401


# ---- /api/v1 routes (Phase 4.3) ----


def test_routes_api_create_reorder_killswitch_delete(live):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    store = Store.load()
    store.add_channel("nordvpn", "US", "", dict(WG))

    st, body, _ = _req(
        base + "/api/v1/routes",
        method="POST",
        headers=origin,
        data={"type": "domain", "value": "api.example.com", "target": "nordvpn/us_1"},
    )
    assert st == 200 and json.loads(body)["rule"]["id"] == "r1"
    st, body, _ = _req(
        base + "/api/v1/routes",
        method="POST",
        headers=origin,
        data={"type": "domain_suffix", "value": "example.com", "target": "direct"},
    )
    assert st == 200 and json.loads(body)["rule"]["id"] == "r2"

    st, body, _ = _req(base + "/api/v1/routes", headers=_bearer(secret))
    assert st == 200 and [r["id"] for r in json.loads(body)["rules"]] == ["r1", "r2"]

    st, body, _ = _req(
        base + "/api/v1/routes/reorder",
        method="POST",
        headers=origin,
        data={"ids": ["r2", "r1"]},
    )
    data = json.loads(body)
    assert st == 200 and [r["id"] for r in data["rules"]] == ["r2", "r1"]
    assert data["rules"][1]["shadowed_by"] == "r2"

    st, body, _ = _req(
        base + "/api/v1/routes/reorder",
        method="POST",
        headers=origin,
        data={"ids": ["r1"]},
    )
    assert st == 400 and "missing rule" in json.loads(body)["error"]
    assert [r["id"] for r in Store.load().rules()] == ["r2", "r1"]

    st, body, _ = _req(
        base + "/api/v1/routes/killswitch",
        method="POST",
        headers=origin,
        data={"enabled": True},
    )
    assert st == 200 and json.loads(body)["router"]["unmatched"] == "block"

    st, _, _ = _req(base + "/api/v1/routes/r1", method="DELETE", headers=origin)
    assert st == 200
    assert [r["id"] for r in Store.load().rules()] == ["r2"]


# ---- /api/v1 metrics, test, logs (Phase 4.4) ----


def test_metrics_endpoint_returns_snapshot(live):
    base, secret = live
    Store.load().add_channel("nordvpn", "US", "", dict(WG))

    st, body, _ = _req(base + "/api/v1/metrics", headers=_bearer(secret))

    data = json.loads(body)
    assert st == 200
    assert data["total_sent"] == 0 and data["total_received"] == 0
    assert data["channels"][0]["name"] == "us_1"


def test_test_endpoint_calls_service_with_speed_and_channel(live, monkeypatch):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    seen = {}

    def fake_test(*, speed=False, channel=None, progress=None):
        seen.update({"speed": speed, "channel": channel, "progress": progress})
        return {"speed": speed, "filter": channel, "channels": []}

    monkeypatch.setattr(service, "test", fake_test)

    st, body, _ = _req(
        base + "/api/v1/test",
        method="POST",
        headers=origin,
        data={"speed": True, "channel": "us_1"},
    )

    assert st == 200 and json.loads(body)["filter"] == "us_1"
    assert seen == {"speed": True, "channel": "us_1", "progress": None}


def test_lifecycle_endpoints_call_service(live, monkeypatch):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    calls = []
    monkeypatch.setattr(
        service, "start", lambda: calls.append("start") or {"has_channels": False}
    )
    monkeypatch.setattr(
        service, "stop", lambda: calls.append("stop") or {"was_running": True}
    )
    monkeypatch.setattr(
        service, "restart", lambda: calls.append("restart") or {"reconnect_cleared": 0}
    )

    for action in ["start", "stop", "restart"]:
        st, _, _ = _req(
            base + f"/api/v1/lifecycle/{action}",
            method="POST",
            headers=origin,
            data={},
        )
        assert st == 200

    assert calls == ["start", "stop", "restart"]


def test_logs_endpoint_returns_tail_and_clamps_lines(live, monkeypatch):
    base, secret = live
    seen = []

    def fake_tail(lines):
        seen.append(lines)
        return "line one\nline two"

    monkeypatch.setattr(service, "logs_tail", fake_tail)

    st, body, _ = _req(base + "/api/v1/logs?lines=999999", headers=_bearer(secret))

    assert st == 200
    assert json.loads(body)["text"] == "line one\nline two"
    assert seen == [1000]
