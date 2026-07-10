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

import json
import os
import secrets
import socket
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
    """Poll the control port until the server accepts a connection.

    The daemon (which serves the UI) starts asynchronously, so ``alle ui`` waits
    here before opening the browser — otherwise it would open on a port nothing
    is listening on yet.
    """
    import time

    host, port = control_api()["address"].split(":")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, int(port)), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def mint_login_url() -> str:
    """A one-time login URL for ``alle ui`` — carries a single-use token."""
    api = control_api()
    token = auth.mint_login_token(api["secret"])
    return f"http://{api['address']}/?token={token}"  # noqa:S5332


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
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            return self._root(parse_qs(parsed.query))
        if path.startswith("/api/"):
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
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        if not self._origin_ok():  # CSRF: mutating requests must be same-origin
            return self._json(403, {"error": "bad origin"})
        path = urlparse(self.path).path
        if method == "POST" and path == "/api/v1/login":
            return self._login_post()  # login authenticates itself
        if not self._authed():
            return self._json(401, {"error": "unauthorized"})
        self._api_mutate(method, path)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

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

    def _login_post(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            token = json.loads(raw).get("token", "")
        except ValueError:
            token = ""
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

    def _stream_test(self, body: dict):
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
                channel=body.get("channel") or None,
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
        from alle import service

        seg = path.strip("/").split("/")[2:]  # drop "api/v1"
        body = self._body()

        if method == "POST" and seg == ["validate"]:
            return self._call(service.setup_validate, str(body.get("text", "")))
        if method == "POST" and seg == ["import"]:
            # --replace (the UI's Replace button confirms before calling) routes
            # to the destructive whole-setup replace instead of a merge
            fn = service.setup_restore if body.get("replace") else service.setup_import
            return self._call(fn, str(body.get("text", "")))

        if method == "POST" and seg == ["providers"]:
            return self._call(_add_provider, body)
        if (
            method == "POST"
            and len(seg) == 3
            and seg[0] == "providers"
            and seg[2] == "token"
        ):
            # Replace an added token provider's credential (write-only: the token
            # is never returned). Config providers raise ServiceError → 400.
            return self._call(
                service.provider_update_token, seg[1], body.get("creds") or {}
            )
        if method == "DELETE" and len(seg) == 2 and seg[0] == "providers":
            return self._call(lambda: service.provider_remove_many([seg[1]]))
        if method == "POST" and seg == ["channels"]:
            return self._call(_add_channel, body)
        if (
            method == "POST"
            and len(seg) == 4
            and seg[0] == "channels"
            and seg[3] == "label"
        ):
            return self._call(
                service.channel_set_label, f"{seg[1]}/{seg[2]}", body.get("label", "")
            )
        if method == "DELETE" and len(seg) == 3 and seg[0] == "channels":
            return self._call(
                lambda: service.channel_remove_many([seg[2]], provider=seg[1])
            )
        if method == "POST" and seg == ["routes", "rulesets"]:
            return self._call(
                service.routes_ruleset_create,
                body.get("name", ""),
                body.get("target", ""),
                body.get("matchers") or [],
            )
        if method == "POST" and len(seg) == 3 and seg[:2] == ["routes", "rulesets"]:
            return self._call(
                service.routes_ruleset_add, seg[2], body.get("matchers") or []
            )
        if (
            method == "POST"
            and len(seg) == 4
            and seg[:2] == ["routes", "rulesets"]
            and seg[3] == "rename"
        ):
            return self._call(
                service.routes_ruleset_rename, seg[2], body.get("name", "")
            )
        if (
            method == "POST"
            and len(seg) == 4
            and seg[:2] == ["routes", "rulesets"]
            and seg[3] == "target"
        ):
            return self._call(
                service.routes_ruleset_retarget, seg[2], body.get("target", "")
            )
        if (
            method == "POST"
            and len(seg) == 4
            and seg[:2] == ["routes", "rulesets"]
            and seg[3] == "update"
        ):
            return self._call(
                service.routes_ruleset_update,
                seg[2],
                body.get("name", ""),
                body.get("target", ""),
                body.get("matchers") or [],
            )
        if method == "DELETE" and len(seg) == 3 and seg[:2] == ["routes", "rulesets"]:
            return self._call(service.routes_ruleset_remove, seg[2])
        if method == "POST" and seg == ["routes", "reorder"]:
            return self._call(
                service.routes_reorder, body.get("ids") or [], bool(body.get("flat"))
            )
        if method == "POST" and seg == ["routes", "killswitch"]:
            return self._call(service.routes_killswitch, bool(body.get("enabled")))
        if method == "POST" and seg == ["routes", "lan"]:
            return self._call(service.routes_lan_direct, bool(body.get("enabled")))
        if method == "DELETE" and len(seg) == 2 and seg[0] == "routes":
            return self._call(service.routes_remove, [seg[1]])
        if method == "POST" and seg == ["test"]:
            # A speed test streams one row per channel as each finishes
            # (application/x-ndjson), so the UI isn't blind until the whole batch
            # completes. Plain probes stay on the single-shot JSON path.
            if body.get("speed"):
                return self._stream_test(body)
            return self._call(
                service.test,
                speed=False,
                channel=body.get("channel") or None,
            )
        if method == "POST" and seg == ["lifecycle", "start"]:
            return self._call(service.start)
        if method == "POST" and seg == ["lifecycle", "stop"]:
            return self._call(service.stop)
        if method == "POST" and seg == ["lifecycle", "restart"]:
            return self._call(service.restart)
        self._json(404, {"error": "not found"})


def _add_provider(body: dict) -> dict:
    from alle import service

    provider = service.resolve_provider(body.get("provider", ""))
    if service.kind(provider) == "config":
        return service.provider_add_config(provider)
    # Posting an already-added token provider replaces its credential (and
    # refreshes its channels) rather than erroring — the idempotent-add contract.
    return service.provider_add_or_update_token(provider, body.get("creds") or {})


def _locations(path: str) -> dict:
    from alle import service

    query = parse_qs(urlparse(path).query)
    provider = (query.get("provider") or [""])[0]
    if not provider:
        raise service.ServiceError("a provider query parameter is required.")
    return service.locations_list(provider, (query.get("country") or [None])[0])


def _add_channel(body: dict) -> dict:
    from alle import service

    provider = service.resolve_provider(body.get("provider", ""))
    conf_text = body.get("conf_text")
    if conf_text is not None:  # browser upload of a .conf's contents
        return service.channel_add_conf_text(
            provider,
            body.get("conf_name") or "import.conf",
            conf_text,
            body.get("label", ""),
        )
    return service.channel_add(
        provider, body.get("country"), body.get("city"), None, body.get("label", "")
    )


def _log_lines(path: str) -> int:
    raw = (parse_qs(urlparse(path).query).get("lines") or ["200"])[0]
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 200
    return max(1, min(n, 1000))


# ---- lifecycle -----------------------------------------------------------------


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
    return ThreadingHTTPServer((host, int(port)), _Handler)


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
