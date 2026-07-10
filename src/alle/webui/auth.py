"""Auth primitives for the Web UI control API.

Three credentials, all keyed off one generated per-installation secret
(``control_api.json``, 0600 — the same pattern as the Clash API secret):

* **Bearer** — programmatic clients send the raw secret as
  ``Authorization: Bearer <secret>``. This is the persistent credential; it must
  never appear in a URL.
* **Login token** — a short-lived, single-use HMAC minted by ``alle ui`` (which
  can read the secret) and handed to the browser in the URL exactly once. The
  server exchanges it for a session cookie and refuses to accept it twice, so a
  copy left in shell history / browser history is inert.
* **Session cookie** — an HMAC over an expiry, issued after login. Stateless
  (validated with the secret alone, so it survives a daemon restart) and set
  ``HttpOnly; SameSite=Strict`` by the server.

HMACs are compared with ``hmac.compare_digest`` (constant time).
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

LOGIN_TTL = 120  # seconds a one-time login token is valid
SESSION_TTL = 12 * 3600  # seconds a browser session lasts


def _sign(secret: str, msg: str) -> str:
    mac = hmac.new(secret.encode(), msg.encode(), sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


# ---- one-time login token ------------------------------------------------------


def mint_login_token(secret: str, *, now: int | None = None) -> str:
    """A single-use token embedding an issue time and nonce (minted by CLI)."""
    now = int(time.time()) if now is None else now
    nonce = base64.urlsafe_b64encode(_rand(9)).decode().rstrip("=")
    payload = f"login:{now}:{nonce}"
    return f"{_b64(payload)}.{_sign(secret, payload)}"


def verify_login_token(
    secret: str, token: str, consumed: set[str], *, now=None
) -> bool:
    """True iff ``token`` is authentic, unexpired, and not already used.

    Records the token in ``consumed`` on success so a replay fails.
    """
    now = int(time.time()) if now is None else now
    try:
        body, sig = token.split(".", 1)
        payload = _unb64(body)
        kind, issued, _nonce = payload.split(":", 2)
    except Exception:  # noqa: BLE001 — any malformed token is invalid
        return False
    if kind != "login":
        return False
    if not hmac.compare_digest(sig, _sign(secret, payload)):
        return False
    if now - int(issued) > LOGIN_TTL or int(issued) - now > LOGIN_TTL:
        return False
    if token in consumed:
        return False
    consumed.add(token)
    _expire_consumed(consumed, now)
    return True


def _expire_consumed(consumed: set[str], now: int) -> None:
    """Drop consumed tokens whose validity window has passed (bounded memory)."""
    expired = set()
    for tok in consumed:
        try:
            issued = int(_unb64(tok.split(".", 1)[0]).split(":")[1])
        except Exception:  # noqa: BLE001
            expired.add(tok)
            continue
        if now - issued > LOGIN_TTL:
            expired.add(tok)
    consumed -= expired


# ---- session cookie ------------------------------------------------------------


def make_session(secret: str, *, now: int | None = None) -> str:
    """A stateless session token: ``<expiry>.<hmac>``."""
    now = int(time.time()) if now is None else now
    expiry = now + SESSION_TTL
    return f"{expiry}.{_sign(secret, f'session:{expiry}')}"


def verify_session(secret: str, cookie: str | None, *, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    if not cookie:
        return False
    try:
        expiry_s, sig = cookie.split(".", 1)
        expiry = int(expiry_s)
    except (ValueError, AttributeError):
        return False
    if expiry < now:
        return False
    return hmac.compare_digest(sig, _sign(secret, f"session:{expiry}"))


# ---- readiness challenge ---------------------------------------------------


def health_proof(secret: str, nonce: str) -> str:
    """The HMAC answer to a readiness challenge — "the process on this port
    really is alle" — proving possession of the installation secret without
    sending it. The ``health:`` domain separation means a captured answer can
    never be replayed as a login token or session cookie."""
    return _sign(secret, f"health:{nonce}")


# ---- bearer --------------------------------------------------------------------


def check_bearer(secret: str, header: str | None) -> bool:
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer ") :].strip(), secret)


def secret_matches(secret: str, value: str | None) -> bool:
    """Constant-time equality with the raw secret — the manual login fallback
    (paste the ``secret`` from control_api.json when not using ``alle ui``)."""
    return hmac.compare_digest(value or "", secret)


def _rand(n: int) -> bytes:
    import secrets

    return secrets.token_bytes(n)
