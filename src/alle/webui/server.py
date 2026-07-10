"""The Web UI control server: stdlib HTTP on loopback, served by alled.

Runs as a daemon thread inside the applier process (``start_in_thread``), so the
UI ships and runs with the daemon — no separate service. Everything about the
transport is deliberately small: a threaded ``http.server``, JSON that is a 1:1
projection of ``alle.service`` (no business logic here), and static assets read
from the package.

Security posture (the localhost-web-UI attack class is real — DNS rebinding and
CSRF have burned loopback apps before):

* Bound strictly to ``127.0.0.1``.
* Every request's ``Host`` must be our loopback host:port — a rebound
  ``evil.com`` resolving to 127.0.0.1 sends a foreign Host and is refused.
* Mutating requests must carry a same-origin ``Origin`` — CSRF defense, on top
  of the ``SameSite=Strict`` session cookie.
* The persistent secret is only ever a Bearer header; browsers authenticate with
  a single-use login token exchanged for an HttpOnly cookie (see ``auth``).
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

from alle import applog, paths
from alle.webui import auth

_ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}

# Hard cap on any request body. The largest legitimate payload is a setup
# bundle (tens of KB of YAML); 1 MiB leaves generous headroom while keeping a
# hostile local client from making the daemon buffer arbitrary amounts.
MAX_BODY = 1 << 20


class _BadRequest(Exception):
    """A request the transport layer refuses — carries the HTTP status.

    Central to the fail-closed contract: malformed input must become a 4xx,
    never a silently-coerced ``{}`` (which once could disable the kill switch).
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def _config_path() -> Path:
    return paths.state_dir() / "control_api.json"


def control_api() -> dict:
    """The Web UI endpoint ``{"address": "127.0.0.1:<port>", "secret"}``.

    Generated once (0600) and kept — the port is a *contract* so the UI URL is
    stable and bookmarkable, and a fixed value to pin Host/Origin against.
    """
    p = _config_path()
    while True:
        try:
            cfg = json.loads(p.read_text())
            if cfg.get("address") and cfg.get("secret"):
                return {"address": cfg["address"], "secret": cfg["secret"]}
            p.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            p.unlink(missing_ok=True)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        fresh = {"address": f"127.0.0.1:{port}", "secret": secrets.token_hex(32)}
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            json.dump(fresh, f, indent=2)
        return fresh


def ui_url() -> str:
    """The plain (token-free) Web UI URL — safe to print and log."""
    return f"http://{control_api()['address']}"  # noqa:S5332


def wait_until_serving(timeout: float = 6.0) -> bool:
    """Poll until *our* control server proves it is behind the contract port.

    A bare TCP connect is not enough: the port may be squatted by an unrelated
    process (after the real bind failed), and ``alle ui`` must never hand its
    tokenized login URL to a foreign listener. Readiness therefore means
    answering an HMAC health challenge with the installation secret — which is
    never itself sent over the wire.
    """
    import time

    api = control_api()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health_ok(api):
            return True
        time.sleep(0.15)
    return False


def _health_ok(api: dict) -> bool:
    """One health challenge round-trip against the contract port."""
    import urllib.request

    nonce = secrets.token_urlsafe(16)
    try:
        req = urllib.request.Request(f"http://{api['address']}/health?nonce={nonce}")  # noqa: S5332
        with urllib.request.urlopen(req, timeout=1) as r:  # noqa: S310 (loopback)
            data = json.loads(r.read(4096))
    except (OSError, ValueError):
        return False
    proof = str((data or {}).get("proof") or "")
    return hmac.compare_digest(proof, auth.health_proof(api["secret"], nonce))


def mint_login_url() -> str:
    """A one-time login URL for ``alle ui`` — carries a single-use token."""
    api = control_api()
    token = auth.mint_login_token(api["secret"])
    return f"http://{api['address']}/?token={token}"  # noqa:S5332


# ---- body field validation -------------------------------------------------


def _fields(body: dict, *allowed: str) -> None:
    """Reject unknown body fields — typos must not silently mean defaults."""
    unknown = set(body) - set(allowed)
    if unknown:
        raise _BadRequest(400, f"unknown field(s): {', '.join(sorted(unknown))}")


def _require(body: dict, key: str):
    if key not in body or body[key] is None:
        raise _BadRequest(400, f"missing required field {key!r}")
    return body[key]


def _str_field(body: dict, key: str, *, required: bool = False) -> str:
    v = _require(body, key) if required else body.get(key)
    if v is None:
        return ""
    if not isinstance(v, str):
        raise _BadRequest(400, f"field {key!r} must be a string")
    return v


def _opt_str_field(body: dict, key: str) -> str | None:
    """A string field where absent/null means None (e.g. optional country)."""
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise _BadRequest(400, f"field {key!r} must be a string")
    return v


def _bool_field(body: dict, key: str, *, required: bool = False) -> bool:
    """A strict JSON boolean — the string \"false\" must never read as truthy."""
    v = _require(body, key) if required else body.get(key)
    if v is None:
        return False
    if not isinstance(v, bool):
        raise _BadRequest(400, f"field {key!r} must be a boolean")
    return v


def _dict_field(body: dict, key: str) -> dict:
    v = body.get(key)
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise _BadRequest(400, f"field {key!r} must be an object")
    return v


def _str_list_field(body: dict, key: str, *, required: bool = False) -> list[str]:
    v = _require(body, key) if required else body.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _BadRequest(400, f"field {key!r} must be a list of strings")
    return v


def _matchers_field(body: dict) -> list:
    """The ruleset matcher list: ``[{value, type?}, …]`` objects (the UI's
    wire shape) or bare strings. Shape only — domain/CIDR syntax stays with
    the service layer's validation."""
    v = _require(body, "matchers")
    if not isinstance(v, list):
        raise _BadRequest(400, "field 'matchers' must be a list")
    for item in v:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            raise _BadRequest(400, "each matcher must be an object with a 'value'")
        unknown = set(item) - {"value", "type", "matcher_type"}
        if unknown:
            raise _BadRequest(
                400, f"unknown matcher field(s): {', '.join(sorted(unknown))}"
            )
        if not isinstance(item.get("value", ""), str) or any(
            item.get(k) is not None and not isinstance(item[k], str)
            for k in ("type", "matcher_type")
        ):
            raise _BadRequest(400, "matcher fields must be strings")
    return v


# ---- request handler -----------------------------------------------------------


def _asset(name: str) -> bytes | None:
    """Read a bundled static asset from ``alle/assets`` (shipped in the wheel),
    or None if it isn't one we ship."""
    if "/" in name or "\\" in name or ".." in name:
        return None  # no path traversal — flat asset dir only
    try:
        return (resources.files("alle") / "assets" / name).read_bytes()
    except (OSError, ModuleNotFoundError):
        return None


class _Handler(BaseHTTPRequestHandler):
    server_version = "alle-webui"
    secret = ""
    address = ""
    consumed: set[str] = set()
    # Socket deadline for reading the request line, headers, and body — a
    # client that connects and stalls cannot pin a worker thread forever.
    timeout = 30

    def log_message(self, *args):  # quiet: the app log is the record, not stderr
        pass

    # -- helpers --
    def _loopback_host(self, host: str) -> bool:
        parsed = urlparse("//" + host)
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"}

    def _host_ok(self) -> bool:
        return self._loopback_host(self.headers.get("Host", ""))

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin is None:
            return False
        parsed = urlparse(origin)
        return (
            parsed.scheme == "http"
            and parsed.netloc == host
            and self._loopback_host(host)
        )

    def _cookie(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "alle_session":
                return v
        return None

    def _authed(self) -> bool:
        return auth.check_bearer(
            self.secret, self.headers.get("Authorization")
        ) or auth.verify_session(self.secret, self._cookie())

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None):
        # service results may nest dataclasses (e.g. a Channel) — serialize via
        # __dict__, matching the CLI's --json projection.
        body = json.dumps(
            obj, default=lambda o: getattr(o, "__dict__", None) or str(o)
        ).encode()
        self._send(code, body, "application/json", extra)

    def _set_session_cookie(self) -> dict:
        cookie = auth.make_session(self.secret)
        return {
            "Set-Cookie": (
                f"alle_session={cookie}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={auth.SESSION_TTL}"
            )
        }

    # -- verbs --
    def do_GET(self):
        try:
            self._get()
        except _BadRequest as e:
            self._json(e.status, {"error": e.message})

    def _get(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            return self._root(parse_qs(parsed.query))
        if path == "/health":
            # unauthenticated on purpose: `alle ui` uses it to prove the
            # process behind the contract port is really alle *before* handing
            # a tokenized login URL to it (see wait_until_serving)
            return self._health(parse_qs(parsed.query))
        if path.startswith("/api/"):
            if not path.startswith("/api/v1/"):
                return self._json(404, {"error": "not found"})  # exact contract
            if not self._authed():
                return self._json(401, {"error": "unauthorized"})
            return self._api_get(path)
        # static assets (the app shell + login page carry no secrets)
        name = path.lstrip("/") or "index.html"
        data = _asset(name)
        if data is None:
            return self._json(404, {"error": "not found"})
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        # no-store: the UI ships inside the daemon, so a browser holding a stale
        # dashboard.js across an upgrade (or an edit during development) would run
        # mismatched code against the live API. Loopback + single user = no cache.
        self._send(
            200,
            data,
            _ASSET_TYPES.get(ext, "application/octet-stream"),
            {"Cache-Control": "no-store"},
        )

    do_HEAD = do_GET

    def do_POST(self):
        self._mutate("POST")

    def do_DELETE(self):
        self._mutate("DELETE")

    def _mutate(self, method: str):
        try:
            self._mutate_checked(method)
        except _BadRequest as e:
            self._json(e.status, {"error": e.message})

    def _mutate_checked(self, method: str):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        if not self._origin_ok():  # CSRF: mutating requests must be same-origin
            return self._json(403, {"error": "bad origin"})
        path = urlparse(self.path).path
        if method == "POST" and path == "/api/v1/login":
            return self._login_post()  # login authenticates itself
        if not path.startswith("/api/v1/"):
            return self._json(404, {"error": "not found"})  # exact contract
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
        self._api_mutate(method, path)

    def _read_body(self) -> bytes:
        """The raw request body, strictly framed — or a typed 4xx refusal.

        Framing errors must never degrade into "empty body": a body that
        cannot be trusted end-to-end is rejected outright.
        """
        if self.headers.get("Transfer-Encoding"):
            raise _BadRequest(501, "transfer encodings are not supported")
        lengths = self.headers.get_all("Content-Length") or []
        if len({v.strip() for v in lengths}) > 1:
            raise _BadRequest(400, "conflicting Content-Length headers")
        if not lengths:
            return b""
        try:
            length = int(lengths[0])
        except ValueError:
            raise _BadRequest(400, "invalid Content-Length") from None
        if length < 0:
            raise _BadRequest(400, "invalid Content-Length")
        if length > MAX_BODY:
            raise _BadRequest(413, "request body too large")
        data = self.rfile.read(length)
        if len(data) != length:
            raise _BadRequest(400, "truncated request body")
        return data

    def _json_body(self) -> dict:
        """The request body as a JSON object, or a typed 4xx refusal.

        Never coerces: malformed JSON, a non-object root, or a wrong media
        type is an error, not ``{}`` — ``{}`` once meant "disable the kill
        switch" on the killswitch endpoint.
        """
        data = self._read_body()
        if not data:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise _BadRequest(415, "Content-Type must be application/json")
        try:
            obj = json.loads(data)
        except ValueError:
            raise _BadRequest(400, "request body is not valid JSON") from None
        if not isinstance(obj, dict):
            raise _BadRequest(400, "request body must be a JSON object")
        return obj

    # -- routes --
    def _root(self, query: dict):
        # `alle ui` lands here with a one-time token: consume it, set the session
        # cookie, and redirect to a clean URL so the token leaves the address bar.
        token = (query.get("token") or [None])[0]
        if token and auth.verify_login_token(self.secret, token, self.consumed):
            return self._send(
                302, b"", "text/plain", {"Location": "/", **self._set_session_cookie()}
            )
        page = (
            "index.html"
            if auth.verify_session(self.secret, self._cookie())
            else "login.html"
        )
        body = _asset(page)
        if body is None:  # assets missing/misplaced — never a silent blank page
            return self._send(
                500,
                b"alle Web UI assets are missing from this build. "
                b"Try `alle restart`, or reinstall alle.",
                "text/plain; charset=utf-8",
            )
        self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _health(self, query: dict):
        """Answer a readiness challenge with an HMAC over the caller's nonce.

        Proves possession of the installation secret without ever sending it;
        the ``health:`` domain separation means the answer can never be
        replayed as a login token or session cookie.
        """
        nonce = (query.get("nonce") or [""])[0]
        if not nonce or len(nonce) > 128:
            raise _BadRequest(400, "a nonce query parameter (<=128 chars) is required")
        self._json(
            200,
            {"proof": auth.health_proof(self.secret, nonce)},
            {"Cache-Control": "no-store"},
        )

    def _login_post(self):
        body = self._json_body()
        _fields(body, "token")
        token = _str_field(body, "token")
        # accept either a one-time login token or the raw secret (manual paste)
        if auth.verify_login_token(
            self.secret, token, self.consumed
        ) or auth.secret_matches(self.secret, token):
            return self._json(200, {"ok": True}, self._set_session_cookie())
        self._json(401, {"error": "invalid or expired token"})

    # -- /api/v1 (a 1:1 projection of alle.service; no business logic here) --
    def _call(self, fn, *args, **kwargs):
        """Run a service call and return its result as JSON; map a user-facing
        error to a 400 whose message the UI shows verbatim (blocker lists,
        rejected tokens, …)."""
        from alle import service
        from alle.providers import ProviderError

        try:
            return self._json(200, fn(*args, **kwargs))
        except (service.ServiceError, ProviderError) as e:
            return self._json(400, {"error": str(e)})

    def _stream_test(self, channel: str | None):
        """Stream a speed test as newline-delimited JSON.

        Emits one ``{"type":"row","data":<row>}`` per channel as it completes,
        then a final ``{"type":"done",…}`` summary (or ``{"type":"error",…}`` if
        the run fails mid-stream). Each line is flushed immediately so the client
        can render incrementally instead of waiting for the whole batch. The body
        is delimited by connection close (no Content-Length) — fine for this
        loopback, single-user server.
        """
        from alle import service
        from alle.providers import ProviderError

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        def write(obj):
            self.wfile.write(
                (
                    json.dumps(
                        obj, default=lambda o: getattr(o, "__dict__", None) or str(o)
                    )
                    + "\n"
                ).encode()
            )
            self.wfile.flush()

        def on_row(row):
            write({"type": "row", "data": row})

        try:
            result = service.test(
                speed=True,
                channel=channel or None,
                on_row=on_row,
            )
        except (service.ServiceError, ProviderError) as e:
            write({"type": "error", "data": {"error": str(e)}})
            return
        except Exception as e:  # surface any failure on the stream
            write({"type": "error", "data": {"error": str(e)}})
            return

        # Summary only: the client already collected the rows above, so the full
        # channels list isn't resent on the wire.
        write(
            {
                "type": "done",
                "data": {
                    "probed": result.get("probed"),
                    "reason": result.get("reason"),
                    "filter": result.get("filter"),
                    "running": result.get("running"),
                    "channel_count": result.get("channel_count"),
                    "healthy_count": result.get("healthy_count"),
                    "failed_count": result.get("failed_count"),
                },
            }
        )

    def _api_get(self, path: str):
        from alle import service

        seg = path.strip("/").split("/")[2:]  # drop "api/v1"
        if seg == ["export"]:
            return self._export_get()
        routes_ = {
            ("status",): service.status_snapshot,
            ("providers",): service.provider_list,
            ("providers", "catalog"): service.provider_catalog,
            ("channels",): service.channel_list,
            ("routes",): service.routes_list,
            ("locations",): lambda: _locations(self.path),
            ("metrics",): lambda: service.metrics_snapshot(
                (parse_qs(urlparse(self.path).query).get("channel") or [None])[0]
            ),
            ("logs",): lambda: {"text": service.logs_tail(_log_lines(self.path))},
        }
        fn = routes_.get(tuple(seg))
        if fn is None:
            return self._json(404, {"error": "not found"})
        self._call(fn)  # same ServiceError -> 400 mapping as mutations

    def _export_get(self):
        """The setup bundle as a YAML download (not JSON — it's a file).

        The response body carries WireGuard private keys and provider tokens,
        same trust model as every other authed loopback response; the browser
        saves it under the user's default download permissions, so the UI
        warns before triggering it.
        """
        import time

        from alle import service

        try:
            result = service.setup_export()
        except service.ServiceError as e:
            return self._json(400, {"error": str(e)})
        name = f"alle-backup-{time.strftime('%Y%m%d-%H%M%S')}.yaml"
        self._send(
            200,
            result["text"].encode(),
            "application/yaml; charset=utf-8",
            {
                "Content-Disposition": f'attachment; filename="{name}"',
                "Cache-Control": "no-store",
            },
        )

    def _api_mutate(self, method: str, path: str):
        """Dispatch a mutation. Every body field is schema-checked here —
        unknown fields, wrong primitive types, and framing problems are 4xx
        via ``_BadRequest`` before any service call runs."""
        from alle import service

        seg = path.strip("/").split("/")[2:]  # drop "api/v1"
        body = self._json_body()

        if method == "POST" and seg == ["validate"]:
            _fields(body, "text")
            return self._call(service.setup_validate, _str_field(body, "text"))
        if method == "POST" and seg == ["import"]:
            _fields(body, "text", "replace")
            # --replace (the UI's Replace button confirms before calling) routes
            # to the destructive whole-setup replace instead of a merge. A
            # strict boolean: the *string* "false" must never select replace.
            fn = (
                service.setup_restore
                if _bool_field(body, "replace")
                else service.setup_import
            )
            return self._call(fn, _str_field(body, "text"))

        if method == "POST" and seg == ["providers"]:
            _fields(body, "provider", "creds")
            return self._call(_add_provider, body)
        if (
            method == "POST"
            and len(seg) == 3
            and seg[0] == "providers"
            and seg[2] == "token"
        ):
            # Replace an added token provider's credential (write-only: the token
            # is never returned). Config providers raise ServiceError → 400.
            _fields(body, "creds")
            return self._call(
                service.provider_update_token, seg[1], _dict_field(body, "creds")
            )
        if method == "DELETE" and len(seg) == 2 and seg[0] == "providers":
            return self._call(lambda: service.provider_remove_many([seg[1]]))
        if method == "POST" and seg == ["channels"]:
            _fields(
                body, "provider", "country", "city", "label", "conf_text", "conf_name"
            )
            return self._call(_add_channel, body)
        if (
            method == "POST"
            and len(seg) == 4
            and seg[0] == "channels"
            and seg[3] == "label"
        ):
            _fields(body, "label")
            return self._call(
                service.channel_set_label,
                f"{seg[1]}/{seg[2]}",
                _str_field(body, "label"),
            )
        if method == "DELETE" and len(seg) == 3 and seg[0] == "channels":
            return self._call(
                lambda: service.channel_remove_many([seg[2]], provider=seg[1])
            )
        if method == "POST" and seg == ["routes", "rulesets"]:
            _fields(body, "name", "target", "matchers")
            return self._call(
                service.routes_ruleset_create,
                _str_field(body, "name"),
                _str_field(body, "target"),
                _matchers_field(body),
            )
        if method == "POST" and len(seg) == 3 and seg[:2] == ["routes", "rulesets"]:
            _fields(body, "matchers")
            return self._call(service.routes_ruleset_add, seg[2], _matchers_field(body))
        if (
            method == "POST"
            and len(seg) == 4
            and seg[:2] == ["routes", "rulesets"]
            and seg[3] == "rename"
        ):
            _fields(body, "name")
            return self._call(
                service.routes_ruleset_rename, seg[2], _str_field(body, "name")
            )
        if (
            method == "POST"
            and len(seg) == 4
            and seg[:2] == ["routes", "rulesets"]
            and seg[3] == "target"
        ):
            _fields(body, "target")
            return self._call(
                service.routes_ruleset_retarget, seg[2], _str_field(body, "target")
            )
        if (
            method == "POST"
            and len(seg) == 4
            and seg[:2] == ["routes", "rulesets"]
            and seg[3] == "update"
        ):
            _fields(body, "name", "target", "matchers")
            return self._call(
                service.routes_ruleset_update,
                seg[2],
                _str_field(body, "name"),
                _str_field(body, "target"),
                _matchers_field(body),
            )
        if method == "DELETE" and len(seg) == 3 and seg[:2] == ["routes", "rulesets"]:
            return self._call(service.routes_ruleset_remove, seg[2])
        if method == "POST" and seg == ["routes", "reorder"]:
            _fields(body, "ids", "flat")
            return self._call(
                service.routes_reorder,
                _str_list_field(body, "ids", required=True),
                _bool_field(body, "flat"),
            )
        if method == "POST" and seg == ["routes", "killswitch"]:
            # `enabled` is required and strictly boolean: a missing field or a
            # coerced string must never silently disable the kill switch
            _fields(body, "enabled")
            return self._call(
                service.routes_killswitch, _bool_field(body, "enabled", required=True)
            )
        if method == "POST" and seg == ["routes", "lan"]:
            _fields(body, "enabled")
            return self._call(
                service.routes_lan_direct, _bool_field(body, "enabled", required=True)
            )
        if method == "DELETE" and len(seg) == 2 and seg[0] == "routes":
            return self._call(service.routes_remove, [seg[1]])
        if method == "POST" and seg == ["test"]:
            # A speed test streams one row per channel as each finishes
            # (application/x-ndjson), so the UI isn't blind until the whole batch
            # completes. Plain probes stay on the single-shot JSON path.
            _fields(body, "speed", "channel")
            channel = _opt_str_field(body, "channel")
            if _bool_field(body, "speed"):
                return self._stream_test(channel)
            return self._call(service.test, speed=False, channel=channel or None)
        if method == "POST" and seg == ["lifecycle", "start"]:
            _fields(body)
            return self._call(service.start)
        if method == "POST" and seg == ["lifecycle", "stop"]:
            _fields(body)
            return self._call(service.stop)
        if method == "POST" and seg == ["lifecycle", "restart"]:
            _fields(body)
            return self._call(service.restart)
        self._json(404, {"error": "not found"})


def _add_provider(body: dict) -> dict:
    from alle import service

    provider = service.resolve_provider(_str_field(body, "provider", required=True))
    if service.kind(provider) == "config":
        return service.provider_add_config(provider)
    # Posting an already-added token provider replaces its credential (and
    # refreshes its channels) rather than erroring — the idempotent-add contract.
    return service.provider_add_or_update_token(provider, _dict_field(body, "creds"))


def _locations(path: str) -> dict:
    from alle import service

    query = parse_qs(urlparse(path).query)
    provider = (query.get("provider") or [""])[0]
    if not provider:
        raise service.ServiceError("a provider query parameter is required.")
    return service.locations_list(provider, (query.get("country") or [None])[0])


def _add_channel(body: dict) -> dict:
    from alle import service

    provider = service.resolve_provider(_str_field(body, "provider", required=True))
    conf_text = _opt_str_field(body, "conf_text")
    if conf_text is not None:  # browser upload of a .conf's contents
        return service.channel_add_conf_text(
            provider,
            _str_field(body, "conf_name") or "import.conf",
            conf_text,
            _str_field(body, "label"),
        )
    return service.channel_add(
        provider,
        _opt_str_field(body, "country"),
        _opt_str_field(body, "city"),
        None,
        _str_field(body, "label"),
    )


def _log_lines(path: str) -> int:
    raw = (parse_qs(urlparse(path).query).get("lines") or ["200"])[0]
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 200
    return max(1, min(n, 1000))


# ---- lifecycle -----------------------------------------------------------------


class _BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on concurrent handler threads.

    Thread-per-connection with no bound lets any local process drive the
    daemon to thousands of threads; over-capacity connections are closed
    immediately instead (the single-user UI never needs this many).
    """

    MAX_WORKERS = 16

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.MAX_WORKERS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def build_server() -> ThreadingHTTPServer:
    """Create the control server bound to the contract port (not yet serving).

    Wires the handler's credentials from ``control_api.json`` and resets the
    single-use-token set. Raises ``OSError`` if the port cannot be bound.
    """
    api = control_api()
    host, port = api["address"].split(":")
    _Handler.secret = api["secret"]
    _Handler.address = api["address"]
    _Handler.consumed = set()
    return _BoundedServer((host, int(port)), _Handler)


def start_in_thread() -> str | None:
    """Start the control server in a daemon thread.

    Returns the plain URL, or None if the port could not be bound (logged, never
    fatal to the daemon).
    """
    try:
        httpd = build_server()
    except OSError as e:
        applog.log(f"web ui: could not bind {control_api()['address']}: {e}")
        return None
    Thread(target=httpd.serve_forever, name="alle-webui", daemon=True).start()
    url = ui_url()
    applog.log(f"web ui: serving {url}")
    return url
