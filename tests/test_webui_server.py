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
from conftest import wg_config

WG = wg_config("1.2.3.4")


@pytest.fixture
def live():
    """A running control server. Yields ``(base_url, secret)``."""
    Store.load().add_provider("nordvpn")  # a little content for /status
    httpd = server.build_server()
    # a short poll interval keeps each test's shutdown() near-instant
    Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True).start()
    api = server.control_api()
    try:
        yield f"http://{api['address']}", api["secret"]
    finally:
        httpd.shutdown()


def _req(url, *, method="GET", headers=None, data=None, raw=None):
    """``data`` is JSON-encoded and sent as application/json (matching the
    UI's fetch helper); ``raw`` sends bytes verbatim for malformed-body tests."""
    body = json.dumps(data).encode() if data is not None else raw
    headers = dict(headers or {})
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(r) as resp:  # noqa: S310 (loopback test)
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _canon() -> str:
    """The canonical per-install host:port (sent as an explicit Host header —
    tests connect to the literal 127.0.0.1 address, like a browser that just
    resolved the .localhost name)."""
    return server._canonical_host()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get_no_redirect(url, headers=None):
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with opener.open(req) as resp:  # noqa: S310 (loopback test)
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers)


def test_root_serves_login_page_when_unauthenticated(live):
    base, _ = live
    status, body, _ = _req(base + "/", headers={"Host": _canon()})
    assert status == 200 and b"sign in" in body.lower()


def test_literal_host_page_load_redirects_to_canonical(live):
    # cookies are host-scoped: a page (and its cookie) must never live on
    # 127.0.0.1, which every other local web app shares
    base, _ = live
    status, headers = _get_no_redirect(base + "/?token=abc")
    assert status == 302
    assert headers["Location"] == f"http://{_canon()}/?token=abc"  # query kept
    assert "Set-Cookie" not in headers  # nothing minted on the literal host


def test_head_does_not_consume_a_login_token(live):
    # A HEAD carrying a one-time login token must not spend it (HEAD is
    # non-mutating). The same token then still logs in via GET.
    base, secret = live
    token = auth.mint_login_token(secret)
    status, _, headers = _req(
        base + "/?token=" + token, method="HEAD", headers={"Host": _canon()}
    )
    assert status == 200
    assert "Set-Cookie" not in headers  # no session minted
    # the token is still good for a real GET login
    status, headers = _get_no_redirect(
        base + "/?token=" + token, headers={"Host": _canon()}
    )
    assert status == 302 and "Set-Cookie" in headers


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
    # bearer API access works on any loopback host (tunnels, scripts)
    status, _, _ = _req(
        base + "/api/v1/status",
        headers={"Authorization": f"Bearer {secret}", "Host": "127.0.0.1:8080"},
    )
    assert status == 200

    # … but a session cookie is only ever minted on the canonical host —
    # a 127.0.0.1-scoped cookie would leak to every other local service
    status, body, _ = _req(
        base + "/api/v1/login",
        method="POST",
        headers={"Origin": "http://127.0.0.1:8080", "Host": "127.0.0.1:8080"},
        data={"token": secret},
    )
    assert status == 403 and _canon() in json.loads(body)["error"]

    # on the canonical host, the origin must match it exactly
    status, _, _ = _req(
        base + "/api/v1/login",
        method="POST",
        headers={"Origin": f"http://{_canon()}", "Host": _canon()},
        data={"token": secret},
    )
    assert status == 200
    status, _, _ = _req(
        base + "/api/v1/login",
        method="POST",
        headers={"Origin": "http://127.0.0.1:8081", "Host": _canon()},
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
        headers={"Origin": f"http://{_canon()}", "Host": _canon()},
        data={"token": secret},
    )
    assert status == 200
    cookie = headers.get("Set-Cookie", "")
    assert "alle_session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_one_time_token_redirects_and_is_single_use(live):
    base, secret = live
    token = auth.mint_login_token(secret)

    # first use (on the canonical host, where the login URL points): 302 to a
    # clean URL with the session cookie set
    status, headers = _get_no_redirect(
        base + f"/?token={token}", headers={"Host": _canon()}
    )
    assert status == 302 and "alle_session=" in headers.get("Set-Cookie", "")
    assert headers["Location"] == "/"

    # second use of the same token: no redirect — it just serves the login page
    st, body, _ = _req(base + f"/?token={token}", headers={"Host": _canon()})
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
    status, body, _ = _req(base + "/", headers={"Host": _canon()})
    assert status == 500 and b"assets are missing" in body  # never a silent blank page


# ---- session lifecycle: logout, revocation, rolling refresh ----


def _session_cookie(base, secret) -> str:
    _, _, headers = _req(
        base + "/api/v1/login",
        method="POST",
        headers={"Origin": f"http://{_canon()}", "Host": _canon()},
        data={"token": secret},
    )
    return headers["Set-Cookie"].split(";")[0]  # "alle_session=<value>"


def test_logout_revokes_every_session(live):
    base, secret = live
    cookie = _session_cookie(base, secret)
    st, _, _ = _req(
        base + "/api/v1/status", headers={"Host": _canon(), "Cookie": cookie}
    )
    assert st == 200  # the session works …

    st, _, headers = _req(
        base + "/api/v1/logout",
        method="POST",
        headers={
            "Origin": f"http://{_canon()}",
            "Host": _canon(),
            "Cookie": cookie,
        },
        data={},
    )
    assert st == 200
    assert "Max-Age=0" in headers.get("Set-Cookie", "")  # cookie cleared

    # … and is dead afterwards, even though it hasn't expired
    st, _, _ = _req(
        base + "/api/v1/status", headers={"Host": _canon(), "Cookie": cookie}
    )
    assert st == 401
    # bearer access (the persistent secret) is unaffected by logout
    st, _, _ = _req(base + "/api/v1/status", headers=_bearer(secret))
    assert st == 200


def test_corrupt_revocation_file_fails_closed(live):
    # web_revocation.json records a security decision (sign-out): if it turns
    # unreadable, every outstanding session dies (fail closed) instead of
    # signed-out sessions silently coming back (fail open).
    base, secret = live
    cookie = _session_cookie(base, secret)
    st, _, _ = _req(
        base + "/api/v1/status", headers={"Host": _canon(), "Cookie": cookie}
    )
    assert st == 200  # the session works …

    server._revocation_path().write_text("{not json")
    st, _, _ = _req(
        base + "/api/v1/status", headers={"Host": _canon(), "Cookie": cookie}
    )
    assert st == 401  # … and dies the moment the revocation record is unreadable
    # bearer access (the persistent secret) is unaffected
    st, _, _ = _req(base + "/api/v1/status", headers=_bearer(secret))
    assert st == 200

    # a fresh sign-in heals the file (durable rewrite) and works again
    healed = _session_cookie(base, secret)
    st, _, _ = _req(
        base + "/api/v1/status", headers={"Host": _canon(), "Cookie": healed}
    )
    assert st == 200
    assert server._read_revoked_at() is not None  # readable again


def test_logout_survives_as_a_complete_durable_file(live):
    # revocation goes through the durable-write path: after a logout the file
    # is a complete, parseable record (never a torn write_text)
    base, secret = live
    _session_cookie(base, secret)
    st, _, _ = _req(
        base + "/api/v1/logout", method="POST", headers=_bearer(secret), data={}
    )
    assert st == 200
    revoked_at = server._read_revoked_at()
    assert revoked_at is not None and revoked_at > 0


def test_bearer_mutation_needs_no_origin_header(live):
    # Programmatic Bearer clients (curl, scripts) are not browsers: no page can
    # attach an Authorization header cross-origin, so the CSRF Origin
    # requirement applies to the cookie path only.
    base, secret = live
    st, _, _ = _req(
        base + "/api/v1/logout", method="POST", headers=_bearer(secret), data={}
    )
    assert st == 200


def test_cookie_mutation_without_origin_is_still_refused(live):
    base, secret = live
    cookie = _session_cookie(base, secret)
    st, body, _ = _req(
        base + "/api/v1/logout",
        method="POST",
        headers={"Host": _canon(), "Cookie": cookie},
        data={},
    )
    assert st == 403 and "origin" in json.loads(body)["error"]


def test_aged_session_is_rolled_on_activity(live):
    base, secret = live
    # a session past half its idle window (issued in the past, still valid)
    import time as _time

    now = int(_time.time())
    aged = auth.make_session(secret, now=now - auth.SESSION_IDLE // 2 - 5)
    st, _, headers = _req(
        base + "/api/v1/status",
        headers={"Host": _canon(), "Cookie": f"alle_session={aged}"},
    )
    assert st == 200
    refreshed = headers.get("Set-Cookie", "")
    assert "alle_session=" in refreshed  # rolled: a replacement cookie rides along
    new_value = refreshed.split(";")[0].split("=", 1)[1]
    assert new_value != aged
    # the replacement keeps the original issue time (SESSION_MAX still caps)
    assert new_value.split(".", 1)[0] == aged.split(".", 1)[0]


def test_fresh_session_is_not_rolled(live):
    base, secret = live
    cookie = _session_cookie(base, secret)
    st, _, headers = _req(
        base + "/api/v1/status", headers={"Host": _canon(), "Cookie": cookie}
    )
    assert st == 200
    assert "Set-Cookie" not in headers  # no churn on every poll


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


def test_reupload_identical_conf_reports_unchanged(live):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    _req(
        base + "/api/v1/providers",
        method="POST",
        headers=origin,
        data={"provider": "protonvpn"},
    )
    body = {
        "provider": "protonvpn",
        "conf_name": "wg-US-CA-9.conf",
        "conf_text": _conf(),
    }
    st, first, _ = _req(
        base + "/api/v1/channels", method="POST", headers=origin, data=body
    )
    assert st == 200 and json.loads(first)["unchanged"] is False

    st, again, _ = _req(
        base + "/api/v1/channels", method="POST", headers=origin, data=body
    )
    assert st == 200 and json.loads(again)["unchanged"] is True


def test_replace_token_endpoint_reresolves_and_hides_token(live, monkeypatch):
    from alle import service

    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)
    monkeypatch.setattr(
        service,
        "provider_resolver",
        lambda p, c: lambda a, b: {"private_key": "z", "peer": {}},
    )
    # add nordvpn with an initial token, then a channel to re-resolve
    _req(
        base + "/api/v1/providers",
        method="POST",
        headers=origin,
        data={"provider": "nordvpn", "creds": {"token": "first"}},
    )
    service.Store.load().add_channel(
        "nordvpn", "Japan", "", {"private_key": "old", "peer": {}}
    )

    st, body, _ = _req(
        base + "/api/v1/providers/nordvpn/token",
        method="POST",
        headers=origin,
        data={"creds": {"token": "SUPER-SECRET-NEW"}},
    )
    assert st == 200
    data = json.loads(body)
    assert data["updated"] is True
    assert data["channels"]["resolved"] == ["japan_1"]
    assert b"SUPER-SECRET-NEW" not in body  # the raw token is never echoed back
    assert service.credentials.get("nordvpn") == {"token": "SUPER-SECRET-NEW"}

    # Re-posting the identical token is a no-op: unchanged, no re-resolve.
    monkeypatch.setattr(
        service,
        "provider_resolver",
        lambda p, c: (_ for _ in ()).throw(AssertionError("must not re-resolve")),
    )
    st, body, _ = _req(
        base + "/api/v1/providers/nordvpn/token",
        method="POST",
        headers=origin,
        data={"creds": {"token": "SUPER-SECRET-NEW"}},
    )
    again = json.loads(body)
    assert st == 200 and again["unchanged"] is True and again["updated"] is False
    assert again["channels"] == {"resolved": [], "failed": []}


def test_replace_token_on_config_provider_is_400(live):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    _req(
        base + "/api/v1/providers",
        method="POST",
        headers=origin,
        data={"provider": "protonvpn"},
    )
    st, body, _ = _req(
        base + "/api/v1/providers/protonvpn/token",
        method="POST",
        headers=origin,
        data={"creds": {"token": "x"}},
    )
    assert st == 400 and "no token to replace" in json.loads(body)["error"]


def test_post_existing_token_provider_updates(live, monkeypatch):
    from alle import service

    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)
    monkeypatch.setattr(
        service,
        "provider_resolver",
        lambda p, c: lambda a, b: {"private_key": "z", "peer": {}},
    )
    _req(
        base + "/api/v1/providers",
        method="POST",
        headers=origin,
        data={"provider": "nordvpn", "creds": {"token": "one"}},
    )

    st, body, _ = _req(
        base + "/api/v1/providers",
        method="POST",
        headers=origin,
        data={"provider": "nordvpn", "creds": {"token": "two"}},
    )
    assert st == 200 and json.loads(body)["updated"] is True
    assert service.credentials.get("nordvpn") == {"token": "two"}


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
    Store.load().create_ruleset(
        "protonvpn/wg_de_1", "protonvpn/wg_de_1", [("domain_suffix", "x.com")]
    )

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
    assert st == 405  # flat per-rule authoring is gone: /routes is a known
    # resource, but POST /api/v1/routes has no handler (rulesets is the path)

    st, body, _ = _req(
        base + "/api/v1/routes/rulesets",
        method="POST",
        headers=origin,
        data={
            "name": "API",
            "target": "nordvpn/us_1",
            "matchers": [{"value": "api.example.com"}],
        },
    )
    assert st == 200 and json.loads(body)["ruleset"]["id"] == "rs1"
    st, body, _ = _req(
        base + "/api/v1/routes/rulesets",
        method="POST",
        headers=origin,
        data={
            "name": "Example",
            "target": "direct",
            "matchers": [{"value": "example.com"}],
        },
    )
    assert st == 200 and json.loads(body)["ruleset"]["id"] == "rs2"

    st, body, _ = _req(base + "/api/v1/routes", headers=_bearer(secret))
    data = json.loads(body)
    assert st == 200
    assert [r["id"] for r in data["rules"]] == ["r1", "r2"]
    assert [rs["id"] for rs in data["rulesets"]] == ["rs1", "rs2"]

    st, body, _ = _req(
        base + "/api/v1/routes/reorder",
        method="POST",
        headers=origin,
        data={"ids": ["rs2", "rs1"]},
    )
    data = json.loads(body)
    assert st == 200 and [rs["id"] for rs in data["rulesets"]] == ["rs2", "rs1"]
    assert data["rulesets"][1]["rules"][0]["shadowed_by"] == "r2"

    st, body, _ = _req(
        base + "/api/v1/routes/reorder",
        method="POST",
        headers=origin,
        data={"ids": ["rs1"]},
    )
    assert st == 400 and "missing ruleset" in json.loads(body)["error"]
    assert [rs["id"] for rs in Store.load().rulesets()] == ["rs2", "rs1"]

    st, body, _ = _req(
        base + "/api/v1/routes/killswitch",
        method="POST",
        headers=origin,
        data={"enabled": True},
    )
    assert st == 200 and json.loads(body)["router"]["unmatched"] == "block"

    st, body, _ = _req(
        base + "/api/v1/routes/lan",
        method="POST",
        headers=origin,
        data={"enabled": False},
    )
    assert st == 200 and json.loads(body)["router"]["lan_direct"] is False
    st, body, _ = _req(base + "/api/v1/routes", headers=_bearer(secret))
    assert json.loads(body)["router"]["lan_direct"] is False

    st, _, _ = _req(base + "/api/v1/routes/r1", method="DELETE", headers=origin)
    assert st == 200
    assert [r["id"] for r in Store.load().rules()] == ["r2"]

    st, _, _ = _req(
        base + "/api/v1/routes/rulesets/rs2", method="DELETE", headers=origin
    )
    assert st == 200
    assert Store.load().rules() == []


def test_locations_endpoint_requires_a_provider(live):
    base, secret = live
    st, body, _ = _req(base + "/api/v1/locations", headers=_bearer(secret))
    assert st == 400 and "provider" in json.loads(body)["error"]

    st, body, _ = _req(
        base + "/api/v1/locations?provider=protonvpn", headers=_bearer(secret)
    )  # config provider: no locations API, guidance payload instead
    assert st == 200 and json.loads(body)["available"] is False


# ---- /api/v1 metrics, test, logs (Phase 4.4) ----


def test_metrics_endpoint_is_gone_and_test_rows_carry_traffic(live):
    # /api/v1/metrics was retired: test rows carry sent/received directly,
    # so the UI (and any API client) reads traffic off the one channel table.
    base, secret = live
    Store.load().add_channel("nordvpn", "US", "", dict(WG))

    st, _, _ = _req(base + "/api/v1/metrics", headers=_bearer(secret))
    assert st == 404

    st, body, _ = _req(
        base + "/api/v1/test",
        method="POST",
        headers=_bearer(secret),
        data={"speed": False},
    )
    assert st == 200
    row = json.loads(body)["channels"][0]
    assert row["name"] == "us_1"
    assert row["sent"] == 0 and row["received"] == 0


def test_test_endpoint_streams_speed_test(live, monkeypatch):
    """speed=true streams NDJSON — one row per channel as it completes, then a
    done summary — while still forwarding speed + channel to service.test."""
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    seen = {}

    def fake_test(
        *,
        speed=False,
        channel=None,
        progress=None,
        on_row=None,
        on_begin=None,
        cancel=None,
    ):
        seen.update({"speed": speed, "channel": channel})
        if on_row:
            on_row(
                {
                    "provider": "nordvpn",
                    "name": "us_1",
                    "speed_result": {"download_bps": 1e6},
                }
            )
        return {
            "probed": True,
            "filter": channel,
            "running": True,
            "channel_count": 1,
            "healthy_count": 1,
            "failed_count": 0,
            "channels": [],
        }

    monkeypatch.setattr(service, "test", fake_test)

    st, body, _ = _req(
        base + "/api/v1/test",
        method="POST",
        headers=origin,
        data={"speed": True, "channel": "us_1"},
    )

    events = [json.loads(line) for line in body.splitlines() if line.strip()]
    assert st == 200
    assert seen == {"speed": True, "channel": "us_1"}
    assert [e["type"] for e in events] == ["row", "done"]
    assert events[0]["data"]["name"] == "us_1"
    assert (
        events[-1]["data"]["filter"] == "us_1"
    )  # done carries the summary, not channels


def test_test_endpoint_probe_returns_json(live, monkeypatch):
    """speed=false stays on the single-shot JSON path (not streamed)."""
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}

    monkeypatch.setattr(
        service,
        "test",
        lambda *, speed=False, channel=None, progress=None: {
            "filter": channel,
            "channels": [],
        },
    )

    st, body, _ = _req(
        base + "/api/v1/test",
        method="POST",
        headers=origin,
        data={"speed": False, "channel": "us_1"},
    )
    assert st == 200
    assert json.loads(body)["filter"] == "us_1"  # one JSON object, not NDJSON


def test_stream_emits_exactly_one_terminal_on_failure(live, monkeypatch):
    """A mid-stream failure produces exactly one terminal ``error`` record — the
    framed decoder relies on a single terminal to stop, and a follow-up write
    must not re-fail into the dead protocol."""
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}

    def failing_test(*, speed=True, channel=None, on_row=None, cancel=None, **_):
        if on_row:
            on_row({"provider": "nordvpn", "name": "us_1"})
        raise service.ServiceError("boom mid-stream")

    monkeypatch.setattr(service, "test", failing_test)
    st, body, _ = _req(
        base + "/api/v1/test", method="POST", headers=origin, data={"speed": True}
    )
    events = [json.loads(line) for line in body.splitlines() if line.strip()]
    assert st == 200
    terminals = [e for e in events if e["type"] in ("done", "error")]
    assert len(terminals) == 1  # exactly one terminal, and it's the error
    assert terminals[0]["type"] == "error" and "boom" in terminals[0]["data"]["error"]


def test_a_second_concurrent_speed_test_is_refused(live, monkeypatch):
    """The job limiter serializes speed tests: while one is in flight, a second
    POST /api/v1/test?speed=true gets 503 (not a second overlapping run)."""
    import threading

    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    release = threading.Event()

    def blocking_test(*, speed=True, channel=None, on_row=None, cancel=None, **_):
        if on_row:
            on_row({"provider": "nordvpn", "name": "us_1"})
        release.wait(5)  # hold the job open until the test releases it
        return {"probed": True, "channel_count": 1, "channels": []}

    monkeypatch.setattr(service, "test", blocking_test)

    first = threading.Thread(
        target=lambda: _req(
            base + "/api/v1/test", method="POST", headers=origin, data={"speed": True}
        )
    )
    first.start()
    # give the first request a moment to acquire the "test" job
    import time

    time.sleep(0.2)
    st, body, _ = _req(
        base + "/api/v1/test", method="POST", headers=origin, data={"speed": True}
    )
    assert st == 503  # a second concurrent run is refused
    assert "already running" in json.loads(body)["error"]
    release.set()
    first.join(5)


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


# ---- strict request validation (fail closed, never coerce) ----


def test_malformed_json_is_rejected_and_killswitch_untouched(live):
    """Garbage bodies once coerced to {} — which read as enabled=False and
    silently disabled the kill switch. They must 400 and change nothing."""
    base, secret = live
    Store.load().set_killswitch(True)
    st, body, _ = _req(
        base + "/api/v1/routes/killswitch",
        method="POST",
        headers={
            "Origin": base,
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        raw=b"{definitely not json",
    )
    assert st == 400 and "not valid JSON" in json.loads(body)["error"]
    assert Store.load().router.get("killswitch") is True  # untouched


def test_killswitch_requires_a_strict_boolean(live):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    Store.load().set_killswitch(True)
    # the *string* "false" must not read as a boolean at all
    st, body, _ = _req(
        base + "/api/v1/routes/killswitch",
        method="POST",
        headers=origin,
        data={"enabled": "false"},
    )
    assert st == 400 and "must be a boolean" in json.loads(body)["error"]
    # a missing field must not read as False
    st, body, _ = _req(
        base + "/api/v1/routes/killswitch", method="POST", headers=origin, data={}
    )
    assert st == 400 and "missing required field" in json.loads(body)["error"]
    assert Store.load().router.get("killswitch") is True  # still untouched


def test_tun_endpoint_toggles_and_gates_on_privileges(live, monkeypatch):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    monkeypatch.setattr(service.daemon, "ensure_running", lambda: None)
    monkeypatch.setattr(service.daemon, "daemon_info", lambda: None)

    # unprivileged enable → 400 carrying the documented sudo path, state untouched
    monkeypatch.setattr("os.geteuid", lambda: 501)
    st, body, _ = _req(
        base + "/api/v1/tun", method="POST", headers=origin, data={"enabled": True}
    )
    assert st == 400 and "privileged helper" in json.loads(body)["error"]
    assert Store.load().router["tun"] is False

    # privileged enable flips the flag and reports it in router info
    monkeypatch.setattr("os.geteuid", lambda: 0)
    st, body, _ = _req(
        base + "/api/v1/tun", method="POST", headers=origin, data={"enabled": True}
    )
    assert st == 200 and json.loads(body)["router"]["tun"] is True

    # disabling never needs privileges — recovery must always be possible
    monkeypatch.setattr("os.geteuid", lambda: 501)
    st, body, _ = _req(
        base + "/api/v1/tun", method="POST", headers=origin, data={"enabled": False}
    )
    assert st == 200 and json.loads(body)["router"]["tun"] is False

    # strict boolean contract, same as the kill switch
    st, body, _ = _req(base + "/api/v1/tun", method="POST", headers=origin, data={})
    assert st == 400 and "missing required field" in json.loads(body)["error"]


def test_unknown_body_fields_are_rejected(live):
    base, secret = live
    st, body, _ = _req(
        base + "/api/v1/routes/killswitch",
        method="POST",
        headers={"Origin": base, "Authorization": f"Bearer {secret}"},
        data={"enabled": True, "enabeld": True},
    )
    assert st == 400 and "unknown field(s): enabeld" in json.loads(body)["error"]


def test_non_object_json_root_is_rejected(live):
    base, secret = live
    st, body, _ = _req(
        base + "/api/v1/lifecycle/start",
        method="POST",
        headers={"Origin": base, "Authorization": f"Bearer {secret}"},
        data=["not", "an", "object"],
    )
    assert st == 400 and "must be a JSON object" in json.loads(body)["error"]


def test_wrong_content_type_is_415(live):
    base, secret = live
    st, body, _ = _req(
        base + "/api/v1/lifecycle/start",
        method="POST",
        headers={
            "Origin": base,
            "Authorization": f"Bearer {secret}",
            "Content-Type": "text/plain",
        },
        raw=b'{"ok": true}',
    )
    assert st == 415


def test_replace_string_must_be_boolean_not_truthy(live):
    """The string "false" once *selected* the destructive whole-setup replace
    (bool("false") is True). Strict typing refuses it outright."""
    base, secret = live
    st, body, _ = _req(
        base + "/api/v1/import",
        method="POST",
        headers={"Origin": base, "Authorization": f"Bearer {secret}"},
        data={"text": "x", "replace": "false"},
    )
    assert st == 400 and "must be a boolean" in json.loads(body)["error"]


def test_oversized_body_is_413_without_reading_it(live):
    # the refusal is based on the declared Content-Length alone — the server
    # never buffers the oversized payload
    base, secret = live
    host = base.removeprefix("http://")
    head = (
        f"POST /api/v1/validate HTTP/1.1\r\nHost: {host}\r\nOrigin: {base}\r\n"
        f"Authorization: Bearer {secret}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {server.MAX_BODY + 1}\r\n\r\n"
    ).encode()
    resp = _raw_request(base, head)
    assert b"413" in resp.split(b"\r\n", 1)[0]


def test_matchers_shape_is_validated(live):
    base, secret = live
    origin = {"Origin": base, "Authorization": f"Bearer {secret}"}
    for bad in ("netflix.com", [42], [{"value": 42}], [{"vlaue": "x"}]):
        st, body, _ = _req(
            base + "/api/v1/routes/rulesets",
            method="POST",
            headers=origin,
            data={"name": "X", "target": "direct", "matchers": bad},
        )
        assert st == 400, bad


def _raw_request(base: str, payload: bytes) -> bytes:
    """Send raw HTTP bytes (for framing that urllib refuses to produce)."""
    import socket as _socket

    host, port = base.removeprefix("http://").split(":")
    with _socket.create_connection((host, int(port)), timeout=3) as s:
        s.sendall(payload)
        s.settimeout(3)
        out = b""
        try:
            while chunk := s.recv(4096):
                out += chunk
        except TimeoutError:
            pass
        return out


def test_invalid_and_conflicting_content_length_are_400(live):
    base, secret = live
    host = base.removeprefix("http://")
    common = (
        f"POST /api/v1/lifecycle/start HTTP/1.1\r\nHost: {host}\r\n"
        f"Origin: {base}\r\nAuthorization: Bearer {secret}\r\n"
        "Content-Type: application/json\r\n"
    )
    resp = _raw_request(base, (common + "Content-Length: abc\r\n\r\n").encode())
    assert b"400" in resp.split(b"\r\n", 1)[0] and b"invalid Content-Length" in resp
    resp = _raw_request(base, (common + "Content-Length: -5\r\n\r\n").encode())
    assert b"400" in resp.split(b"\r\n", 1)[0]
    resp = _raw_request(
        base,
        (common + "Content-Length: 2\r\nContent-Length: 4\r\n\r\n{}ab").encode(),
    )
    assert b"conflicting Content-Length" in resp


def test_transfer_encoding_is_refused(live):
    base, secret = live
    host = base.removeprefix("http://")
    payload = (
        f"POST /api/v1/lifecycle/start HTTP/1.1\r\nHost: {host}\r\n"
        f"Origin: {base}\r\nAuthorization: Bearer {secret}\r\n"
        "Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
        "0\r\n\r\n"
    ).encode()
    resp = _raw_request(base, payload)
    assert b"501" in resp.split(b"\r\n", 1)[0]


def test_api_prefix_is_exact(live):
    base, secret = live
    st, _, _ = _req(base + "/api/v9/status", headers=_bearer(secret))
    assert st == 404  # only /api/v1/... exists
    st, _, _ = _req(
        base + "/api/v9/routes/killswitch",
        method="POST",
        headers={"Origin": base, "Authorization": f"Bearer {secret}"},
        data={"enabled": False},
    )
    assert st == 404


def test_bounded_server_caps_worker_threads():
    httpd = server.build_server()
    try:
        assert isinstance(httpd, server._BoundedServer)
        assert httpd._slots._value == server._BoundedServer.MAX_WORKERS
    finally:
        httpd.server_close()


# ---- readiness challenge (/health) ----


def test_health_answers_the_nonce_challenge(live):
    base, secret = live
    st, body, _ = _req(base + "/health?nonce=abc123")  # no auth needed
    assert st == 200
    assert json.loads(body)["proof"] == auth.health_proof(secret, "abc123")
    st, body, _ = _req(base + "/health")  # nonce required
    assert st == 400


def test_wait_until_serving_verifies_the_listener(live, monkeypatch):
    base, _ = live
    api = server.control_api()
    assert server._health_ok(api) is True
    # a listener that cannot prove the secret (foreign process on the port,
    # or anything else) must NOT count as serving — no login URL for it
    assert server._health_ok({**api, "secret": "not-the-real-secret"}) is False
    assert server.wait_until_serving(timeout=0.3) is True


def test_health_proof_is_domain_separated():
    secret = "s3cret"
    proof = auth.health_proof(secret, "n1")
    assert proof != auth.health_proof(secret, "n2")  # nonce-bound
    # the proof is not a usable credential anywhere else
    assert auth.verify_login_token(secret, proof) is False
    assert auth.verify_session(secret, proof) is False


def test_control_api_concurrent_callers_agree_on_one_endpoint():
    # Two callers racing to first-generate the contract endpoint are serialized
    # by the lock: both get the SAME port+secret+host, not each their own with
    # one clobbering the file (which would desync the CLI's URL from the server).
    import concurrent.futures

    # fresh state dir ⇒ no control_api.json yet
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: server.control_api(), range(16)))
    assert len({r["address"] for r in results}) == 1
    assert len({r["secret"] for r in results}) == 1
    assert len({r["host"] for r in results}) == 1


def test_control_api_rejects_a_shape_wrong_file():
    from alle import paths

    # parses as JSON but the fields aren't usable strings → strict validation
    # regenerates rather than returning a half-formed endpoint
    (paths.state_dir() / "control_api.json").write_text(
        '{"address": 123, "secret": null, "host": 5}'
    )
    api = server.control_api()
    assert isinstance(api["address"], str)
    assert isinstance(api["secret"], str)
    assert isinstance(api["host"], str)


def test_every_response_carries_security_headers(live):
    base, secret = live
    # an API response, an error, and a static asset all carry the same set
    for path in ["/api/v1/status", "/api/v1/no-such", "/style.css"]:
        st, _, headers = _req(
            base + path, headers={"Host": _canon(), "Authorization": f"Bearer {secret}"}
        )
        assert "Content-Security-Policy" in headers
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Permissions-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
    # and HEAD too
    st, _, headers = _req(base + "/health", method="HEAD", headers={"Host": _canon()})
    assert "Content-Security-Policy" in headers


def test_server_banner_does_not_leak_the_python_version(live):
    base, _ = live
    _, _, headers = _req(base + "/", headers={"Host": _canon()})
    assert headers["Server"] == "alle-webui"
    assert "Python" not in headers["Server"]


def test_known_api_path_wrong_method_is_405_with_allow(live):
    base, secret = live
    # Origin must match the (literal) Host for the CSRF check; Bearer works on
    # any loopback host.
    auth_ = {"Origin": base, "Authorization": f"Bearer {secret}"}
    # status is GET-only; POST names a known resource under the wrong method
    st, body, headers = _req(base + "/api/v1/status", method="POST", headers=auth_)
    assert st == 405
    assert "Allow" in headers and "GET" in headers["Allow"]
    # import is POST-only; GET is the wrong method for it
    st, _, headers = _req(base + "/api/v1/import", headers=auth_)
    assert st == 405 and headers["Allow"] == "POST"


def test_unknown_api_path_is_still_404(live):
    base, secret = live
    st, _, _ = _req(
        base + "/api/v1/bogus-resource",
        headers={"Host": _canon(), "Authorization": f"Bearer {secret}"},
    )
    assert st == 404  # not a known resource at all


def test_login_page_uses_an_external_script(live):
    # the inline script moved to /login.js so CSP can drop 'unsafe-inline'
    base, _ = live
    _, body, _ = _req(base + "/", headers={"Host": _canon()})
    assert b"<script" in body and b"login.js" in body
    assert b"addEventListener" not in body  # no inline script body remains
    st, js_body, _ = _req(base + "/login.js", headers={"Host": _canon()})
    assert st == 200 and b"addEventListener" in js_body


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
