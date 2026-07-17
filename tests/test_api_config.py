"""The 7B env knobs: ALLE_API_LISTEN (opt-in non-loopback bind) and
ALLE_API_SECRET[_FILE] (injected Bearer secret), plus the network-bind Host
carve-outs — Bearer and /health pass a foreign Host, the cookie path never
does. Config parsing is unit-tested; the gate behavior runs against a real
server on a loopback port with the handler flipped to net mode."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from threading import Thread

import pytest

from alle import service
from alle.api import server
from alle.state import Store


@pytest.fixture(autouse=True)
def no_background(monkeypatch):
    monkeypatch.setattr(service.daemon, "ensure_running", lambda: None)


def _req(url, *, method="GET", headers=None, data=None):
    body = json.dumps(data).encode() if data is not None else None
    headers = dict(headers or {})
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    r = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(r) as resp:  # noqa: S310 (loopback test)
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# ---- ALLE_API_LISTEN parsing -----------------------------------------------


def test_parse_listen_accepts_host_and_host_port():
    assert server._parse_listen("0.0.0.0:8080") == ("0.0.0.0", 8080)
    assert server._parse_listen("0.0.0.0") == ("0.0.0.0", None)
    assert server._parse_listen("localhost:9999") == ("localhost", 9999)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ":8080",
        "0.0.0.0:",
        "0.0.0.0:0",
        "0.0.0.0:70000",
        "0.0.0.0:abc",
        "http://x:1",
        "::",
        "a b:1",
    ],
)
def test_parse_listen_rejects_malformed(raw):
    assert server._parse_listen(raw) is None


def test_listen_config_unset_is_the_loopback_contract(monkeypatch):
    monkeypatch.delenv("ALLE_API_LISTEN", raising=False)
    api = server.control_api()
    lc = server._listen_config(api)
    assert lc == {"bind": api["address"], "client": api["address"], "net": False}


def test_listen_config_invalid_falls_back_to_loopback(monkeypatch):
    # a typo must narrow, never widen
    monkeypatch.setenv("ALLE_API_LISTEN", "0.0.0.0:notaport")
    api = server.control_api()
    assert server._listen_config(api)["bind"] == api["address"]
    assert server._listen_config(api)["net"] is False


def test_listen_config_wildcard_is_net_and_client_is_loopback(monkeypatch):
    monkeypatch.setenv("ALLE_API_LISTEN", "0.0.0.0:8080")
    lc = server._listen_config(server.control_api())
    assert lc == {"bind": "0.0.0.0:8080", "client": "127.0.0.1:8080", "net": True}


def test_listen_config_portless_keeps_the_contract_port(monkeypatch):
    monkeypatch.setenv("ALLE_API_LISTEN", "0.0.0.0")
    api = server.control_api()
    port = api["address"].rsplit(":", 1)[1]
    lc = server._listen_config(api)
    assert lc["bind"] == f"0.0.0.0:{port}" and lc["net"] is True


def test_listen_config_loopback_values_are_not_net(monkeypatch):
    monkeypatch.setenv("ALLE_API_LISTEN", "127.0.0.1:9999")
    assert server._listen_config(server.control_api())["net"] is False
    monkeypatch.setenv("ALLE_API_LISTEN", "localhost:9999")
    assert server._listen_config(server.control_api())["net"] is False


# ---- ALLE_API_SECRET[_FILE] ---------------------------------------------------


def test_secret_unset_is_the_minted_one(monkeypatch):
    monkeypatch.delenv("ALLE_API_SECRET", raising=False)
    monkeypatch.delenv("ALLE_API_SECRET_FILE", raising=False)
    api = server.control_api()
    assert server._api_secret(api) == api["secret"]


def test_secret_env_overrides_the_minted_one(monkeypatch):
    monkeypatch.setenv("ALLE_API_SECRET", "an-injected-secret-value")
    api = server.control_api()
    assert server._api_secret(api) == "an-injected-secret-value"


def test_secret_file_overrides_the_minted_one(monkeypatch, tmp_path):
    p = tmp_path / "secret"
    p.write_text("a-file-injected-secret\n")  # trailing newline is stripped
    monkeypatch.setenv("ALLE_API_SECRET_FILE", str(p))
    assert server._api_secret(server.control_api()) == "a-file-injected-secret"


def test_secret_both_sources_refuse(monkeypatch, tmp_path):
    p = tmp_path / "secret"
    p.write_text("a-file-injected-secret")
    monkeypatch.setenv("ALLE_API_SECRET", "an-injected-secret-value")
    monkeypatch.setenv("ALLE_API_SECRET_FILE", str(p))
    with pytest.raises(server.ApiConfigError, match="exactly one"):
        server._api_secret(server.control_api())


def test_secret_unreadable_file_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLE_API_SECRET_FILE", str(tmp_path / "absent"))
    with pytest.raises(server.ApiConfigError, match="unreadable"):
        server._api_secret(server.control_api())


@pytest.mark.parametrize("weak", ["", "short", "fifteen-chars-x"])
def test_secret_weak_values_refuse(monkeypatch, weak):
    monkeypatch.setenv("ALLE_API_SECRET", weak)
    with pytest.raises(server.ApiConfigError, match="too short"):
        server._api_secret(server.control_api())


def test_build_server_refuses_on_secret_conflict(monkeypatch, tmp_path):
    # the server must serve NOTHING on a config the operator didn't intend
    p = tmp_path / "secret"
    p.write_text("a-file-injected-secret")
    monkeypatch.setenv("ALLE_API_SECRET", "an-injected-secret-value")
    monkeypatch.setenv("ALLE_API_SECRET_FILE", str(p))
    with pytest.raises(server.ApiConfigError):
        server.build_server()


def test_wait_until_serving_is_false_not_a_crash_on_bad_config(monkeypatch, tmp_path):
    monkeypatch.setenv("ALLE_API_SECRET", "an-injected-secret-value")
    monkeypatch.setenv("ALLE_API_SECRET_FILE", str(tmp_path / "also"))
    assert server.wait_until_serving(timeout=0.1) is False


# ---- live servers -----------------------------------------------------------


@pytest.fixture
def live_net(monkeypatch):
    """A running control server whose handler is in network-bind mode.

    The socket stays on loopback (tests can't claim real interfaces); ``net``
    is what changes the Host policy, and that is what's under test. Yields
    ``(base_url, secret)``.
    """
    Store.load().add_provider("nordvpn")
    httpd = server.build_server()
    monkeypatch.setattr(server._Handler, "net", True)
    Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True).start()
    api = server.control_api()
    try:
        yield f"http://{api['address']}", api["secret"]
    finally:
        httpd.shutdown()


@pytest.fixture
def live(monkeypatch):
    """A running control server in the default (loopback) mode."""
    Store.load().add_provider("nordvpn")
    httpd = server.build_server()
    Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True).start()
    api = server.control_api()
    try:
        yield f"http://{api['address']}", api["secret"]
    finally:
        httpd.shutdown()


FOREIGN = {"Host": "alle:8080"}  # what a compose sibling's request carries


def test_net_bind_bearer_passes_a_foreign_host(live_net):
    base, secret = live_net
    st, body = _req(
        base + "/api/v1/status",
        headers={**FOREIGN, "Authorization": f"Bearer {secret}"},
    )
    assert st == 200 and json.loads(body)["state"] in ("running", "stopped")


def test_net_bind_foreign_host_without_bearer_is_refused(live_net):
    base, _ = live_net
    st, body = _req(base + "/api/v1/status", headers=FOREIGN)
    assert st == 403 and b"bad host" in body


def test_net_bind_wrong_bearer_is_refused(live_net):
    base, _ = live_net
    st, _ = _req(
        base + "/api/v1/status",
        headers={**FOREIGN, "Authorization": "Bearer wrong-secret-value"},
    )
    assert st == 403


def test_net_bind_health_passes_a_foreign_host(live_net):
    base, _ = live_net
    st, body = _req(base + "/health?nonce=abc", headers=FOREIGN)
    assert st == 200 and b"proof" in body


def test_net_bind_cookie_path_stays_loopback_only(live_net):
    # the login page (and any cookie-authenticated path) never opens up to
    # the network — browser access from off-box is not part of the contract
    base, _ = live_net
    st, _ = _req(base + "/", headers=FOREIGN)
    assert st == 403


def test_net_bind_bearer_mutation_passes_a_foreign_host(live_net):
    base, secret = live_net
    st, body = _req(
        base + "/api/v1/providers/remove",
        method="POST",
        headers={**FOREIGN, "Authorization": f"Bearer {secret}"},
        data={"names": ["nordvpn"], "dry_run": True},
    )
    assert st == 200 and json.loads(body)["dry_run"] is True


def test_default_bind_keeps_the_strict_host_pin(live):
    # without the explicit opt-in, a foreign Host is refused even WITH a
    # valid Bearer — host installs are unchanged by this feature existing
    base, secret = live
    st, _ = _req(
        base + "/api/v1/status",
        headers={**FOREIGN, "Authorization": f"Bearer {secret}"},
    )
    assert st == 403
    st, _ = _req(base + "/health?nonce=abc", headers=FOREIGN)
    assert st == 403


def test_injected_secret_is_the_live_credential(monkeypatch):
    monkeypatch.setenv("ALLE_API_SECRET", "an-injected-secret-value")
    httpd = server.build_server()
    Thread(target=lambda: httpd.serve_forever(poll_interval=0.02), daemon=True).start()
    api = server.control_api()
    base = f"http://{api['address']}"
    try:
        st, _ = _req(
            base + "/api/v1/status",
            headers={"Authorization": "Bearer an-injected-secret-value"},
        )
        assert st == 200
        # the minted (overridden) secret no longer authenticates
        st, _ = _req(
            base + "/api/v1/status",
            headers={"Authorization": f"Bearer {api['secret']}"},
        )
        assert st == 401
    finally:
        httpd.shutdown()
