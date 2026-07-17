"""The control API server: stdlib HTTP on loopback, served by alled.

Serves both the programmatic REST API (``/api/v1``, Bearer-authenticated) and
the browser Web UI (static assets + cookie sessions) — one server, one
contract. Runs as a daemon thread inside the applier process
(``start_in_thread``), so it ships and runs with the daemon — no separate
service. Everything about the transport is deliberately small: a threaded
``http.server``, JSON that is a 1:1 projection of ``alle.service`` (no
business logic here), and static assets read from the package.

Security posture (the localhost-web-UI attack class is real — DNS rebinding and
CSRF have burned loopback apps before):

* Bound to ``127.0.0.1`` unless the operator opts into a network bind with
  ``ALLE_API_LISTEN`` (the container/compose knob; invalid values fall back to
  loopback, never widen). The secret can be injected via ``ALLE_API_SECRET`` /
  ``ALLE_API_SECRET_FILE`` so compose siblings can be handed the same value;
  a conflicting or weak injection refuses to start rather than guess.
* Every request's ``Host`` must be a loopback host:port — a rebound
  ``evil.com`` resolving to 127.0.0.1 sends a foreign Host and is refused.
  On an explicit network bind, two carve-outs pass a foreign Host: requests
  presenting a valid Bearer (no browser attaches one cross-origin, so
  rebinding gains nothing) and ``/health`` (an unauthenticated challenge that
  reveals nothing and mutates nothing). The cookie path — login page, assets,
  sessions — stays loopback-only even then.
* Browsers use a per-installation ``alle-<rand>.localhost`` hostname (cookies
  are scoped to hosts, not ports, so a cookie set for ``127.0.0.1`` would be
  sent to every other local web app): literal-host page loads redirect to the
  canonical host and the session cookie is only ever minted there. Programmatic
  Bearer access still works on any loopback host.
* Cookie-authenticated mutations must carry a same-origin ``Origin`` — CSRF
  defense, on top of the ``SameSite=Strict`` session cookie. Bearer requests
  are exempt: no browser attaches an Authorization header cross-origin.
* The persistent secret is only ever a Bearer header; browsers authenticate with
  a single-use login token exchanged for an HttpOnly cookie (see ``auth``);
  sessions idle out, roll on activity, and are revocable via logout.

The full threat model lives in ``docs/security.md``.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlparse

from alle import applog, fsio, paths
from alle.api import auth

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

# A uniform security-header set attached to EVERY response (see the
# send_response override). The UI loads only same-origin external module
# scripts and one stylesheet, posts forms/fetches to its own origin, and is
# never framed — so the policy is tight: no inline scripts, no inline styles,
# no plugins, no embedding, no <base>. login.html's script is an external
# module (assets/login.js) precisely so script-src can drop 'unsafe-inline'.
_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
_PERMISSIONS_POLICY = ", ".join(
    [
        "camera=()",
        "microphone=()",
        "geolocation=()",
        "interest-cohort=()",
        "browsing-topics=()",
    ]
)
_SECURITY_HEADERS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",
    "Permissions-Policy": _PERMISSIONS_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


# The HTTP methods each top-level /api/v1/<resource> accepts. A request whose
# path names one of these resources but whose method isn't listed is a 405
# (with an Allow header); a path whose first segment isn't here is a 404.
def _api_route_methods(seg: list[str]) -> set[str] | None:
    """Methods for one exact REST target, or ``None`` when it is unknown.

    This registry is deliberately shape-only and body-free. Dispatch consults
    it before media-type/JSON parsing so an unknown nested target remains 404
    and a known target under the wrong verb remains 405 with its exact Allow.
    """
    exact: dict[tuple[str, ...], set[str]] = {
        ("status",): {"GET"},
        ("providers",): {"GET", "POST"},
        ("providers", "catalog"): {"GET"},
        ("providers", "remove"): {"POST"},
        ("channels",): {"GET", "POST"},
        ("channels", "enabled"): {"POST"},
        ("channels", "remove"): {"POST"},
        ("routes",): {"GET"},
        ("routes", "rulesets"): {"POST"},
        ("routes", "reorder"): {"POST"},
        ("routes", "killswitch"): {"POST"},
        ("routes", "lan"): {"POST"},
        ("locations",): {"GET"},
        ("metrics",): {"GET"},
        ("logs",): {"GET"},
        ("test",): {"POST"},
        ("tun",): {"POST"},
        ("export",): {"GET"},
        ("import",): {"POST"},
        ("validate",): {"POST"},
        ("login",): {"POST"},
        ("logout",): {"POST"},
    }
    methods = exact.get(tuple(seg))
    if methods is not None:
        return methods
    if len(seg) == 2 and seg[0] == "providers":
        return {"DELETE"}
    if len(seg) == 3 and seg[0] == "providers" and seg[2] == "token":
        return {"POST"}
    if len(seg) == 3 and seg[0] == "channels":
        return {"DELETE"}
    if len(seg) == 4 and seg[0] == "channels" and seg[3] in {"label", "enabled"}:
        return {"POST"}
    if len(seg) == 3 and seg[:2] == ["routes", "rulesets"]:
        return {"POST", "DELETE"}
    if (
        len(seg) == 4
        and seg[:2] == ["routes", "rulesets"]
        and seg[3] in {"rename", "target", "update"}
    ):
        return {"POST"}
    if len(seg) == 2 and seg[0] == "routes":
        return {"DELETE"}
    if (
        len(seg) == 2
        and seg[0] == "lifecycle"
        and seg[1]
        in {
            "start",
            "stop",
            "restart",
        }
    ):
        return {"POST"}
    return None


# Public contract summary retained for OpenAPI drift tests and tooling. Actual
# dispatch uses the exact shape registry above, so this cannot make an unknown
# nested path look like a known resource.
_API_RESOURCE_METHODS = {
    "status": {"GET"},
    "metrics": {"GET"},
    "logs": {"GET"},
    "test": {"POST"},
    "lifecycle": {"POST"},
    "providers": {"GET", "POST", "DELETE"},
    "channels": {"GET", "POST", "DELETE"},
    "locations": {"GET"},
    "routes": {"GET", "POST", "DELETE"},
    "tun": {"POST"},
    "export": {"GET"},
    "validate": {"POST"},
    "import": {"POST"},
    "login": {"POST"},
    "logout": {"POST"},
}


class _JobLimiter:
    """Per-kind size-1 semaphores bounding expensive operations.

    A single-user loopback server should never run two of the *same* expensive
    job at once — a second speed test, bundle import/restore, or provider token
    refresh while one is in flight conflicts with the first (shared state, the
    one sing-box process, the same network resolver). The limiter serializes
    each kind: a second concurrent request of that kind is rejected (503) rather
    than queued, so a UI double-click or a stuck tab cannot pile up work.
    Different kinds run independently (a speed test does not block an import).
    """

    def __init__(self):
        self._sems: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def try_acquire(self, kind: str) -> bool:
        with self._lock:
            sem = self._sems.get(kind)
            if sem is None:
                sem = threading.BoundedSemaphore(1)
                self._sems[kind] = sem
        return sem.acquire(blocking=False)

    def release(self, kind: str) -> None:
        with self._lock:
            sem = self._sems.get(kind)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass  # never acquired (e.g. an early return path) — ignore


_jobs = _JobLimiter()


@contextmanager
def _guarded(kind: str):
    """Serialize one expensive job ``kind``. Yields ``acquired``: False means a
    job of this kind is already running and the handler should answer 503."""
    acquired = _jobs.try_acquire(kind)
    try:
        yield acquired
    finally:
        if acquired:
            _jobs.release(kind)


class _BadRequest(Exception):
    """A request the transport layer refuses — carries the HTTP status.

    Central to the fail-closed contract: malformed input must become a 4xx,
    never a silently-coerced ``{}`` (which once could disable the kill switch).
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class _StreamClosed(Exception):
    """The streaming client disconnected mid-response — abort, write no more."""


def _config_path() -> Path:
    return paths.state_dir() / "control_api.json"


def _mint_host() -> str:
    """A per-installation browser hostname, e.g. ``alle-3f9c2ab1.localhost``.

    Browsers resolve any ``*.localhost`` to loopback themselves (RFC 6761), so
    no hosts-file entry is needed. Uniqueness is the point: cookies are scoped
    to *hosts*, not ports, so a session cookie set for ``127.0.0.1`` would be
    sent to every other local web app on any port — and two alle homes would
    collide. A random name no other service occupies isolates the session.
    """
    return f"alle-{secrets.token_hex(4)}.localhost"


def _valid_control_api(cfg) -> dict | None:
    """The exact generated endpoint contract, or ``None`` when unsafe/broken."""
    if not isinstance(cfg, dict):
        return None
    address = cfg.get("address")
    secret = cfg.get("secret")
    host = cfg.get("host")
    # Spell these checks out so static type checkers narrow each value; the
    # equivalent ``all(...)`` predicate does not preserve per-variable types.
    if (
        not isinstance(address, str)
        or not isinstance(secret, str)
        or not isinstance(host, str)
    ):
        return None
    bind_host, sep, port_text = address.rpartition(":")
    if bind_host != "127.0.0.1" or sep != ":" or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 0 < port <= 65535 or str(port) != port_text:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", secret) is None:
        return None
    if re.fullmatch(r"alle-[0-9a-f]{8}\.localhost", host) is None:
        return None
    return {"address": address, "secret": secret, "host": host}


def _read_control_api() -> dict | None:
    """Read the daemon-owned endpoint without generating or replacing it."""
    try:
        return _valid_control_api(json.loads(_config_path().read_text()))
    except (OSError, ValueError):
        return None


def control_api() -> dict:
    """The Web UI endpoint ``{"address": "127.0.0.1:<port>", "secret", "host"}``.

    Generated once (0600) and kept — the port is a *contract* so the UI URL is
    stable and bookmarkable, and a fixed value to pin Host/Origin against.
    ``host`` is the per-installation browser hostname (see :func:`_mint_host`);
    a file that fails validation (missing ``host``, wrong shape, unparseable)
    is regenerated wholesale — a fresh port, secret, and hostname.

    Locking and durable publishing live in :func:`alle.fsio.generated_endpoint`
    — concurrent first-generators agree on one endpoint, and a reader never
    sees a half-written file.
    """

    def generate() -> dict:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        return {
            "address": f"127.0.0.1:{port}",
            "secret": secrets.token_hex(32),
            "host": _mint_host(),
        }

    return fsio.generated_endpoint(_config_path(), _valid_control_api, generate)


class ApiConfigError(Exception):
    """An unusable ``ALLE_API_*`` configuration. The server refuses to start
    rather than guess — a wrong guess here is a security-posture change."""


def _parse_listen(raw: str) -> tuple[str, int | None] | None:
    """``host`` or ``host:port`` → ``(host, port-or-None)``; None if malformed.

    IPv4/hostname only (the server is AF_INET); a port-less value keeps the
    minted contract port.
    """
    host, sep, port_s = raw.partition(":")
    host = host.strip()
    if not host or "/" in host or " " in host or ":" in port_s:
        return None
    if not sep:
        return host, None
    if not port_s.isdigit():
        return None
    port = int(port_s)
    if not 1 <= port <= 65535:
        return None
    return host, port


def _listen_config(api: dict) -> dict:
    """Where the server binds and how a same-machine client reaches it.

    ``{"bind": "host:port", "client": "host:port", "net": bool}`` — ``bind``
    is the listening address, ``client`` what local tools connect to (loopback
    for a wildcard bind), ``net`` True when ``ALLE_API_LISTEN`` names a
    non-loopback host. Env unset = the minted loopback contract,
    byte-for-byte; an invalid value logs and keeps loopback — a typo must
    narrow, never widen.
    """
    contract = {"bind": api["address"], "client": api["address"], "net": False}
    raw = (os.environ.get("ALLE_API_LISTEN") or "").strip()
    if not raw:
        return contract
    parsed = _parse_listen(raw)
    if parsed is None:
        applog.log(
            f"api: invalid ALLE_API_LISTEN {raw!r} — keeping the loopback "
            f"contract {api['address']}"
        )
        return contract
    host, port = parsed
    if port is None:
        port = int(api["address"].rsplit(":", 1)[1])
    loopback = host in ("localhost", "::1") or host.startswith("127.")
    client_host = "127.0.0.1" if host == "0.0.0.0" else host
    return {
        "bind": f"{host}:{port}",
        "client": f"{client_host}:{port}",
        "net": not loopback,
    }


def _api_secret(api: dict) -> str:
    """The effective Bearer/signing secret: injected via ``ALLE_API_SECRET``
    or ``ALLE_API_SECRET_FILE`` (exactly one), else the minted per-install one.

    Injection failures raise :class:`ApiConfigError` — a conflicting or weak
    secret must refuse to serve, never fall back silently (the operator
    believes the injected value is in force).
    """
    env = os.environ.get("ALLE_API_SECRET")
    path = os.environ.get("ALLE_API_SECRET_FILE")
    if env is not None and path is not None:
        raise ApiConfigError(
            "both ALLE_API_SECRET and ALLE_API_SECRET_FILE are set — set exactly one"
        )
    if path is not None:
        try:
            value = Path(path).read_text().strip()
        except OSError as e:
            raise ApiConfigError(
                f"ALLE_API_SECRET_FILE {path!r} is unreadable: {e}"
            ) from e
    elif env is not None:
        value = env.strip()
    else:
        return api["secret"]
    if len(value) < 16:
        raise ApiConfigError(
            "the injected API secret is too short — use at least 16 characters "
            "(e.g. openssl rand -hex 32)"
        )
    return value


def effective_api() -> dict:
    """The client-facing endpoint after env overrides: ``{"address", "secret",
    "host", "net"}`` — what same-machine tools (``alle ui``, the tray) use.

    May generate the loopback contract file; read-only callers (the
    companion) compose :func:`_listen_config`/:func:`_api_secret` over their
    own read instead.
    """
    api = control_api()
    lc = _listen_config(api)
    return {
        "address": lc["client"],
        "secret": _api_secret(api),
        "host": api["host"],
        "net": lc["net"],
    }


def _canonical_host(api: dict | None = None) -> str:
    """``<per-install-hostname>:<port>`` — the only host the browser UI uses."""
    api = api or control_api()
    port = _listen_config(api)["client"].rsplit(":", 1)[1]
    return f"{api['host']}:{port}"


def ui_url() -> str:
    """The plain (token-free) Web UI URL — safe to print and log."""
    return f"http://{_canonical_host()}"  # noqa:S5332


def wait_until_serving(timeout: float = 6.0) -> bool:
    """Poll until *our* control server proves it is behind the contract port.

    A bare TCP connect is not enough: the port may be squatted by an unrelated
    process (after the real bind failed), and ``alle ui`` must never hand its
    tokenized login URL to a foreign listener. Readiness therefore means
    answering an HMAC health challenge with the installation secret — which is
    never itself sent over the wire.
    """
    import time

    try:
        api = effective_api()
    except ApiConfigError:
        return False  # the server refuses to start on this config
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
            raw = r.read(4097)
            if len(raw) > 4096:
                return False
            data = json.loads(raw)
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("proof"), str):
        return False
    proof = data["proof"]
    return hmac.compare_digest(proof, auth.health_proof(api["secret"], nonce))


def _login_url_for(api: dict) -> str:
    token = auth.mint_login_token(api["secret"])
    return f"http://{_canonical_host(api)}/?token={token}"  # noqa:S5332


def mint_login_url() -> str:
    """A one-time login URL for ``alle ui`` — carries a single-use token.

    Uses the canonical per-install hostname so the resulting session cookie is
    scoped to a host no other local service can ever be."""
    cfg = _read_control_api()
    if cfg is None:
        raise ApiConfigError("the daemon control endpoint is not configured")
    api = {
        "address": _listen_config(cfg)["client"],
        "secret": _api_secret(cfg),
        "host": cfg["host"],
    }
    if not _health_ok(api):
        raise ApiConfigError(
            f"no alle daemon owns the control endpoint at {api['address']}"
        )
    return _login_url_for(api)


# ---- session revocation (logout) --------------------------------------------


def _revocation_path() -> Path:
    return paths.state_dir() / "web_revocation.json"


def _read_revoked_at() -> int | None:
    """The persisted logout time: 0 when never revoked (file absent), ``None``
    when the file exists but cannot be read or parsed — callers fail closed."""
    p = _revocation_path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    try:
        value = json.loads(text).get("revoked_at", 0)
    except (ValueError, AttributeError):
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


_revocation_warned = False


def revoked_at() -> int:
    """The logout time sessions are verified against: issued at or before it
    means dead.

    Fails closed: an unreadable or corrupt revocation file reads as "revoked
    just now" — killing every outstanding session — never as "never revoked".
    The file records a security decision (sign-out), and losing it must not
    silently resurrect signed-out sessions. The next sign-in heals the file
    (see ``_set_session_cookie``).
    """
    global _revocation_warned
    value = _read_revoked_at()
    if value is None:
        if not _revocation_warned:
            _revocation_warned = True
            applog.log(
                "web ui: web_revocation.json is unreadable — failing closed "
                "(all sessions revoked until the next sign-in rewrites it)"
            )
        import time

        return int(time.time())
    _revocation_warned = False
    return value


def revoke_sessions(now: int) -> None:
    """Persist a logout: every session issued at or before ``now`` stops
    verifying.

    Survives daemon restarts (sessions themselves are stateless, so revocation
    must not be in-memory only) and is written durably (temp, fsync, atomic
    rename): a torn or lost write here would silently resurrect signed-out
    sessions, so it gets the same treatment as every other security-relevant
    store. The content is not a secret — just an epoch.
    """
    fsio.write_durably(
        _revocation_path(),
        lambda f: json.dump({"revoked_at": int(now)}, f),
        prefix=".web_revocation-",
        suffix=".json",
    )


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
    server_version = "alle-api"
    secret = ""
    address = ""
    canonical = ""  # "<per-install-hostname>:<port>" — the browser-facing host
    net = False  # True when the operator opted into a non-loopback bind
    logins: auth.LoginTokenStore  # persisted one-time login-token consumption
    # Socket deadline for reading the request line, headers, and body — a
    # client that connects and stalls cannot pin a worker thread forever.
    timeout = 30

    def version_string(self) -> str:
        # Hide the default "<server_version> Python/<version>" banner: the
        # Server header names alle only, not the interpreter revision.
        return self.server_version

    def send_response(self, code, message=None):
        # Every response — JSON, errors, assets, the ndjson stream, HEAD — gets
        # the same security-header set, attached once right after the status
        # line (before per-route headers and end_headers).
        super().send_response(code, message)
        for header, value in _SECURITY_HEADERS.items():
            self.send_header(header, value)

    def end_headers(self):
        self._response_started = True
        super().end_headers()

    def log_message(self, *args):  # quiet: the app log is the record, not stderr
        pass

    # -- helpers --
    def _loopback_host(self, host: str) -> bool:
        parsed = urlparse("//" + host)
        name = parsed.hostname
        return name in {"127.0.0.1", "localhost", "::1"} or (
            bool(name) and host == self.canonical
        )

    def _host_ok(self) -> bool:
        return self._loopback_host(self.headers.get("Host", ""))

    def _bearer_ok(self) -> bool:
        return auth.check_bearer(self.secret, self.headers.get("Authorization"))

    def _host_gate(self, path: str) -> bool:
        """The Host check, with the network-bind carve-outs.

        A loopback (or canonical) Host always passes — the browser regime.
        On an operator-configured network bind (``self.net``) two extra cases
        pass a foreign Host: a valid Bearer (no browser attaches one
        cross-origin, so DNS rebinding gains nothing here) and ``/health``
        (unauthenticated readiness challenge — reveals nothing, mutates
        nothing). Everything else — login page, assets, the cookie path —
        stays loopback-only even when bound to the network.
        """
        if self._host_ok():
            return True
        if not self.net:
            return False
        return path == "/health" or self._bearer_ok()

    def _dry_run(self) -> bool:
        """The ``?dry_run=`` query flag on destructive endpoints — strict: an
        unrecognized value is a 400, never "false" (a typo must not turn a
        preview into the real removal)."""
        raw = (parse_qs(urlparse(self.path).query).get("dry_run") or [None])[0]
        if raw is None:
            return False
        if raw in ("1", "true"):
            return True
        if raw in ("0", "false"):
            return False
        raise _BadRequest(400, "dry_run must be one of: 1, true, 0, false")

    def _on_canonical_host(self) -> bool:
        return self.headers.get("Host", "") == self.canonical

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
        if auth.check_bearer(self.secret, self.headers.get("Authorization")):
            return True
        cookie = self._cookie()
        if not auth.verify_session(self.secret, cookie, revoked_at=revoked_at()):
            return False
        # Rolling idle refresh: as the session ages past half its idle window,
        # re-issue it (attached by _send) so activity keeps it alive up to the
        # SESSION_MAX cap, while a closed tab idles out in SESSION_IDLE.
        refreshed = auth.refresh_session(self.secret, cookie or "")
        if refreshed:
            self._refresh_cookie = self._cookie_header_value(refreshed)
        return True

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        # Security headers (CSP/XFO/Permissions-Policy/nosniff/no-referrer) are
        # attached uniformly by the send_response override.
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        refresh = getattr(self, "_refresh_cookie", None)
        # an explicit Set-Cookie (login/logout) always outranks a refresh
        if refresh and 200 <= code < 300 and "Set-Cookie" not in (extra or {}):
            self.send_header("Set-Cookie", refresh)
            self._refresh_cookie = None
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None):
        # service results may nest dataclasses (e.g. a Channel) — serialize via
        # __dict__, matching the CLI's --json projection.
        body = json.dumps(
            obj, default=lambda o: getattr(o, "__dict__", None) or str(o)
        ).encode()
        headers = {"Cache-Control": "no-store", **(extra or {})}
        self._send(code, body, "application/json", headers)

    def _unexpected(self) -> None:
        """Bound unexpected handler failures without exposing request data."""
        request_id = secrets.token_hex(6)
        path = self.path.partition("?")[0][:256]
        applog.log(
            f"api: unexpected handler failure request={request_id} "
            f"method={self.command} path={path}"
        )
        if getattr(self, "_response_started", False):
            return
        try:
            self._json(
                500,
                {"error": "internal server error", "request_id": request_id},
            )
        except Exception:  # noqa: BLE001 — the socket itself may be unusable
            pass

    def _cookie_header_value(self, cookie: str) -> str:
        return (
            f"alle_session={cookie}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={auth.SESSION_IDLE}"
        )

    def _set_session_cookie(self) -> dict:
        import time

        now = int(time.time())
        if _read_revoked_at() is None:
            # Unreadable/corrupt revocation file: heal it fail-closed — every
            # session from before this sign-in stays revoked, and the durable
            # rewrite makes the file readable again.
            revoke_sessions(now)
        # mint strictly after any persisted revocation, so a re-login in the
        # same second as a logout is not swallowed by it
        cookie = auth.make_session(
            self.secret, now=now, issued=max(now, revoked_at() + 1)
        )
        return {"Set-Cookie": self._cookie_header_value(cookie)}

    # -- verbs --
    def do_GET(self):
        try:
            self._get()
        except _BadRequest as e:
            self._json(e.status, {"error": e.message})
        except Exception:  # noqa: BLE001 — top-level credential-free boundary
            self._unexpected()

    def _get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._host_gate(path):
            return self._json(403, {"error": "bad host"})

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

    def do_HEAD(self):
        # HEAD is non-mutating: it never consumes a one-time login token, sets a
        # session cookie, or otherwise changes state, and writes headers only
        # (no body), per RFC 9110. Aliasing it to GET would let a HEAD carrying
        # ``?token=`` spend a login token. The UI does not rely on HEAD; this
        # only lets a liveness probe ask "is the server there" safely.
        try:
            path = urlparse(self.path).path
            if not self._host_gate(path):
                code = 403
            else:
                code = 200 if path in ("/", "/health") else 404
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception:  # noqa: BLE001 — a probe must never raise to the client
            try:
                self.send_response(500)
                self.end_headers()
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):
        self._mutate("POST")

    def do_DELETE(self):
        self._mutate("DELETE")

    def _mutate(self, method: str):
        try:
            self._mutate_checked(method)
        except _BadRequest as e:
            self._json(e.status, {"error": e.message})
        except Exception:  # noqa: BLE001 — top-level credential-free boundary
            self._unexpected()

    def _mutate_checked(self, method: str):
        path = urlparse(self.path).path
        if not self._host_gate(path):
            return self._json(403, {"error": "bad host"})
        # CSRF applies to the cookie path only: a browser cannot attach an
        # Authorization header cross-origin, so a Bearer-authenticated request
        # (curl, scripts) needs no Origin; everything else must be same-origin.
        if not self._bearer_ok() and not self._origin_ok():
            return self._json(403, {"error": "bad origin"})
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
        if not self._on_canonical_host():
            # Send browsers on a literal loopback host (old bookmark, typed
            # 127.0.0.1) to the per-install hostname, query preserved — the
            # session cookie must only ever exist on our unique host, never on
            # 127.0.0.1 where every other local web app would receive it.
            return self._send(
                302,
                b"",
                "text/plain",
                {
                    "Location": f"http://{self.canonical}{self.path}",  # noqa: S5332
                    "Cache-Control": "no-store",
                },
            )
        # `alle ui` lands here with a one-time token: consume it, set the session
        # cookie, and redirect to a clean URL so the token leaves the address bar.
        token = (query.get("token") or [None])[0]
        if token and self.logins.verify_and_consume(self.secret, token):
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
        if not self._on_canonical_host():
            # a session cookie must never be minted for a shared literal host
            return self._json(403, {"error": f"sign in at http://{self.canonical}/"})
        body = self._json_body()
        _fields(body, "token")
        token = _str_field(body, "token")
        # accept either a one-time login token or the raw secret (manual paste)
        if self.logins.verify_and_consume(self.secret, token) or auth.secret_matches(
            self.secret, token
        ):
            return self._json(200, {"ok": True}, self._set_session_cookie())
        self._json(401, {"error": "invalid or expired token"})

    # -- /api/v1 (a 1:1 projection of alle.service; no business logic here) --
    def _call(self, fn, *args, **kwargs):
        """Run a service call and return its result as JSON; map a user-facing
        error to a 400 whose message the UI shows verbatim (blocker lists,
        rejected tokens, …). An error that carries a ``web_message`` (the same
        refusal phrased for a browser — ruleset names instead of rule ids and
        CLI commands) sends that instead of the CLI text."""
        from alle import service
        from alle.providers import ProviderError

        try:
            return self._json(200, fn(*args, **kwargs))
        except (service.ServiceError, ProviderError) as e:
            message = getattr(e, "web_message", None) or str(e)
            return self._json(400, {"error": message})

    def _stream_test(self, channel: str | None):
        """Stream a speed test as newline-delimited JSON — a framed protocol.

        Emits one ``{"type":"row","data":<row>}`` per channel as it completes,
        then exactly one terminal record: ``{"type":"done",…}`` on success or
        ``{"type":"error",…}`` on failure. Each line is flushed immediately so the
        client renders incrementally; the body is delimited by connection close
        (no Content-Length) — fine for this loopback, single-user server.

        Framing guarantees: at most one terminal record is ever written (a
        second ``write`` after done/error/gone is suppressed), and a client that
        disconnects mid-stream is detected on the next flush — the run is told to
        cancel (no more per-channel transfers) and the handler exits cleanly
        instead of raising a BrokenPipe traceback or trying to write an ``error``
        line into the dead socket.
        """
        with _guarded("test") as acquired:
            if not acquired:
                return self._json(503, {"error": "a speed test is already running"})
            self._run_stream(channel)

    def _run_stream(self, channel: str | None):
        from alle import service
        from alle.providers import ProviderError

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        state = {"terminal": False, "gone": False}

        def write(obj):
            # Exactly-one terminal: once done/error has been sent (or the socket
            # is gone), drop every further write. The client's framed decoder
            # relies on a single terminal record to stop.
            if state["terminal"] or state["gone"]:
                return
            typ = obj.get("type")
            try:
                self.wfile.write(
                    (
                        json.dumps(
                            obj,
                            default=lambda o: getattr(o, "__dict__", None) or str(o),
                        )
                        + "\n"
                    ).encode()
                )
                self.wfile.flush()
            except OSError:
                # client went away — abort the run, write nothing more.
                state["gone"] = True
                raise _StreamClosed()
            if typ in ("done", "error"):
                state["terminal"] = True

        def on_row(row):
            write({"type": "row", "data": row})

        try:
            result = service.test(
                speed=True,
                channel=channel or None,
                on_row=on_row,
                cancel=lambda: state["gone"],
            )
        except _StreamClosed:
            return  # client disconnected — nothing more to write, work cancelled
        except (service.ServiceError, ProviderError) as e:
            write({"type": "error", "data": {"error": str(e)}})
            return
        except Exception:  # keep unexpected failures credential-free on the wire
            request_id = secrets.token_hex(6)
            applog.log(
                f"web request {request_id} failed: "
                f"{self.command} {urlparse(self.path).path}"
            )
            write(
                {
                    "type": "error",
                    "data": {"error": f"internal server error ({request_id})"},
                }
            )
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

    def _api_no_match(self, seg: list[str], method: str):
        """Exact 404/405 result from the body-free route registry."""
        methods = _api_route_methods(seg)
        if methods is not None and method not in methods:
            return self._json(
                405,
                {"error": "method not allowed"},
                {"Allow": ", ".join(sorted(methods))},
            )
        return self._json(404, {"error": "not found"})

    def _api_get(self, path: str):
        from alle import service

        seg = path.strip("/").split("/")[2:]  # drop "api/v1"
        methods = _api_route_methods(seg)
        if methods is None or "GET" not in methods:
            return self._api_no_match(seg, "GET")

        if seg == ["export"]:
            return self._export_get()
        routes_ = {
            ("status",): service.status_snapshot,
            ("providers",): service.provider_list,
            ("providers", "catalog"): service.provider_catalog,
            ("channels",): service.channel_list,
            ("routes",): service.routes_list,
            ("locations",): lambda: _locations(self.path),
            ("metrics",): lambda: service.metrics_totals(_channel_query(self.path)),
            ("logs",): lambda: {"text": service.logs_tail(_log_lines(self.path))},
        }
        fn = routes_.get(tuple(seg))
        if fn is None:
            return self._api_no_match(seg, "GET")
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
        methods = _api_route_methods(seg)
        if methods is None or method not in methods:
            return self._api_no_match(seg, method)
        body = self._json_body()

        if method == "POST" and seg == ["logout"]:
            import time

            _fields(body)
            # Sessions are stateless, so logout is a persisted revocation
            # time: every session issued before it stops verifying (on this
            # and any future daemon). The bearer secret is unaffected.
            revoke_sessions(int(time.time()))
            self._refresh_cookie = None  # never re-issue on the logout response
            applog.log("web ui: logout — all sessions revoked")
            return self._json(
                200,
                {"ok": True},
                {
                    "Set-Cookie": "alle_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
                },
            )
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
            with _guarded("import") as acquired:
                if not acquired:
                    return self._json(
                        503, {"error": "a bundle import/restore is already running"}
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
            with _guarded("refresh") as acquired:
                if not acquired:
                    return self._json(
                        503, {"error": "a provider refresh is already running"}
                    )
                return self._call(
                    service.provider_update_token, seg[1], _dict_field(body, "creds")
                )
        if method == "POST" and seg == ["providers", "remove"]:
            # Batch remove, mirroring the CLI's multi-name form; dry_run
            # returns the removal plan without touching state.
            _fields(body, "names", "dry_run")
            return self._call(
                service.provider_remove_many,
                _str_list_field(body, "names", required=True),
                dry_run=_bool_field(body, "dry_run"),
            )
        if method == "DELETE" and len(seg) == 2 and seg[0] == "providers":
            dry = self._dry_run()
            return self._call(
                lambda: service.provider_remove_many([seg[1]], dry_run=dry)
            )
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
        if (
            method == "POST"
            and len(seg) == 4
            and seg[0] == "channels"
            and seg[3] == "enabled"
        ):
            # `enabled` is required and strictly boolean, same contract as the
            # kill switch: a missing field must never silently flip a channel.
            # Disabling a channel a routing rule targets is refused; the 400
            # error carries the blockers list the UI shows verbatim.
            _fields(body, "enabled")
            return self._call(
                lambda: service.channel_set_enabled_many(
                    [seg[2]],
                    _bool_field(body, "enabled", required=True),
                    provider=seg[1],
                )
            )
        if method == "POST" and seg == ["channels", "enabled"]:
            # Batch enable/disable, the CLI's full grammar: refs (ids,
            # provider/channel refs, globs), a provider scope, or all+provider.
            _fields(body, "refs", "enabled", "provider", "all", "dry_run")
            return self._call(
                service.channel_set_enabled_many,
                _str_list_field(body, "refs"),
                _bool_field(body, "enabled", required=True),
                provider=_opt_str_field(body, "provider"),
                dry_run=_bool_field(body, "dry_run"),
                all_=_bool_field(body, "all"),
            )
        if method == "POST" and seg == ["channels", "remove"]:
            _fields(body, "refs", "provider", "all", "dry_run")
            return self._call(
                service.channel_remove_many,
                _str_list_field(body, "refs"),
                provider=_opt_str_field(body, "provider"),
                dry_run=_bool_field(body, "dry_run"),
                all_=_bool_field(body, "all"),
            )
        if method == "DELETE" and len(seg) == 3 and seg[0] == "channels":
            dry = self._dry_run()
            return self._call(
                lambda: service.channel_remove_many(
                    [seg[2]], provider=seg[1], dry_run=dry
                )
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
            return self._call(
                service.routes_ruleset_remove, seg[2], dry_run=self._dry_run()
            )
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
        if method == "POST" and seg == ["tun"]:
            # `enabled` is required and strictly boolean, same contract as the
            # kill switch: a missing field must never silently flip TUN mode.
            # The service's privilege gate surfaces as a 400 whose message the
            # UI shows verbatim (the documented sudo path).
            _fields(body, "enabled")
            return self._call(
                service.tun_mode, _bool_field(body, "enabled", required=True)
            )
        if method == "DELETE" and len(seg) == 2 and seg[0] == "routes":
            return self._call(service.routes_remove, [seg[1]], dry_run=self._dry_run())
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
        return self._api_no_match(seg, method)


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


def _channel_query(path: str) -> str | None:
    """The optional ``?channel=`` filter (same ref grammar as ``alle test``)."""
    return (parse_qs(urlparse(path).query).get("channel") or [None])[0]


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
    """Create the control server bound to its configured address (not yet
    serving).

    The address and secret come from the loopback contract
    (``control_api.json``) plus the operator's env overrides
    (``ALLE_API_LISTEN``, ``ALLE_API_SECRET[_FILE]``). Wires the handler's
    persisted login-token store (``web_consumed.json`` — consumption survives
    restarts). Raises ``OSError`` if the address cannot be bound and
    :class:`ApiConfigError` on an unusable secret configuration.
    """
    api = control_api()
    lc = _listen_config(api)
    _Handler.secret = _api_secret(api)  # ApiConfigError propagates: fail loud
    _Handler.address = lc["client"]
    _Handler.net = lc["net"]
    _Handler.canonical = _canonical_host(api)
    _Handler.logins = auth.LoginTokenStore(paths.state_dir() / "web_consumed.json")
    host, port = lc["bind"].rsplit(":", 1)
    return _BoundedServer((host, int(port)), _Handler)


def start_in_thread() -> str | None:
    """Start the control server in a daemon thread.

    Returns the plain URL, or None if the server could not start — bad bind or
    an unusable secret injection (logged, never fatal to the daemon; the
    secret case deliberately serves *nothing* rather than fall back to a
    secret the operator did not intend).
    """
    api = control_api()
    lc = _listen_config(api)
    try:
        httpd = build_server()
    except ApiConfigError as e:
        applog.log(f"api: refusing to start — {e}")
        return None
    except OSError as e:
        applog.log(f"api: could not bind {lc['bind']}: {e}")
        return None
    Thread(target=httpd.serve_forever, name="alle-api", daemon=True).start()
    url = ui_url()
    if lc["net"]:
        applog.log(
            f"api: serving on {lc['bind']} (network bind — Bearer required); "
            f"web ui {url}"
        )
    else:
        applog.log(f"api: serving {url}")
    return url
