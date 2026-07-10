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
* **Session cookie** — an HMAC over an issue time + expiry, issued after
  login. Stateless (validated with the secret alone, so it survives a daemon
  restart) and set ``HttpOnly; SameSite=Strict`` by the server. Sessions are
  *idle-scoped*: each lasts ``SESSION_IDLE`` from the last activity (the
  server re-issues the cookie as it ages), capped at ``SESSION_MAX`` from the
  original sign-in. Logout revokes every session issued before it (the server
  persists the revocation time and passes it to :func:`verify_session`).

HMACs are compared with ``hmac.compare_digest`` (constant time).
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

LOGIN_TTL = 120  # seconds a one-time login token is valid
SESSION_IDLE = 30 * 60  # a session ends after this much inactivity
SESSION_MAX = 12 * 3600  # absolute session cap, regardless of activity


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


def make_session(
    secret: str, *, now: int | None = None, issued: int | None = None
) -> str:
    """A stateless session token: ``<issued>.<expiry>.<hmac>``.

    ``issued`` defaults to now; a re-issue (rolling refresh) passes the
    original sign-in time through so ``SESSION_MAX`` keeps counting from the
    real login, not from the last activity.
    """
    now = int(time.time()) if now is None else now
    issued = now if issued is None else issued
    expiry = min(now + SESSION_IDLE, issued + SESSION_MAX)
    payload = f"session:{issued}:{expiry}"
    return f"{issued}.{expiry}.{_sign(secret, payload)}"


def _parse_session(cookie: str) -> tuple[int, int, str] | None:
    try:
        issued_s, expiry_s, sig = cookie.split(".", 2)
        return int(issued_s), int(expiry_s), sig
    except (ValueError, AttributeError):
        return None


def verify_session(
    secret: str,
    cookie: str | None,
    *,
    now: int | None = None,
    revoked_at: int = 0,
) -> bool:
    """True iff the cookie is authentic, unexpired, and not revoked.

    ``revoked_at`` is the persisted logout time: any session issued at or
    before it is dead, however much idle life it had left (the issuer mints
    post-logout sessions with ``issued > revoked_at``, so a re-login in the
    same second still works).
    """
    now = int(time.time()) if now is None else now
    if not cookie:
        return False
    parsed = _parse_session(cookie)
    if parsed is None:
        return False
    issued, expiry, sig = parsed
    if expiry < now or issued > expiry or expiry - issued > SESSION_MAX:
        return False
    if revoked_at and issued <= revoked_at:
        return False
    return hmac.compare_digest(sig, _sign(secret, f"session:{issued}:{expiry}"))


def refresh_session(secret: str, cookie: str, *, now: int | None = None) -> str | None:
    """A rolled replacement for a valid cookie past half its idle window.

    Keeps the original issue time (so ``SESSION_MAX`` still caps the whole
    session) and returns None when the cookie is still fresh or the cap is
    reached — the caller only sets a new cookie when one is returned.
    """
    now = int(time.time()) if now is None else now
    parsed = _parse_session(cookie)
    if parsed is None:
        return None
    issued, expiry, _sig_ = parsed
    if expiry - now > SESSION_IDLE // 2:
        return None  # fresh enough — don't re-issue on every poll
    if now + SESSION_IDLE // 2 >= issued + SESSION_MAX:
        return None  # absolute cap (nearly) reached — let it expire
    return make_session(secret, now=now, issued=issued)


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
