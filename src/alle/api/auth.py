"""Auth primitives for the control API (REST API + Web UI).

Three credentials, all keyed off one generated per-installation secret
(``control_api.json``, 0600 — the same pattern as the Clash API secret):

* **Bearer** — programmatic clients send the raw secret as
  ``Authorization: Bearer <secret>``. This is the persistent credential; it must
  never appear in a URL.
* **Login token** — a short-lived, single-use HMAC minted by ``alle ui`` (which
  can read the secret) and handed to the browser in the URL exactly once. The
  server exchanges it for a session cookie and refuses to accept it twice, so a
  copy left in shell history / browser history is inert. Consumption is
  persisted (:class:`LoginTokenStore`) so a token spent before a daemon restart
  still cannot be replayed within its TTL, and only the token's *digest* is
  stored — never the raw bearer string.
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
import hashlib
import hmac
import json
import re
import time
from hashlib import sha256
from pathlib import Path

from alle import fsio

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


def _check_login_token(secret: str, token: str, now: int) -> tuple[str, int] | None:
    """Validate one login token, returning ``(canonical_token, issued)`` if it is
    authentic, unexpired, and *canonically* encoded; otherwise ``None``.

    Canonical form is required (not just decodable): the received token must
    byte-match the re-derived ``_b64(payload).sig``. A non-canonical encoding
    (``+`` instead of ``-``, stray padding, altered case) decodes to the same
    payload but is rejected — otherwise the canonical and non-canonical spellings
    would be two distinct "valid" tokens, defeating single-use consumption. The
    same comparison also covers the signature, so a bad signature falls out of
    the canonical-token mismatch for free.
    """
    try:
        body, _sig = token.split(".", 1)
        payload = _unb64(body)
    except Exception:  # noqa: BLE001 — any malformed token is invalid
        return None
    parts = payload.split(":")
    if len(parts) != 3 or parts[0] != "login":
        return None
    try:
        issued = int(parts[1])
    except ValueError:
        return None
    canonical = f"{_b64(payload)}.{_sign(secret, payload)}"
    if not hmac.compare_digest(token, canonical):
        return None
    if abs(now - issued) > LOGIN_TTL:
        return None
    return canonical, issued


def verify_login_token(secret: str, token: str, *, now: int | None = None) -> bool:
    """True iff ``token`` is authentic, unexpired, and canonically encoded.

    Pure — does not record consumption. Use :meth:`LoginTokenStore.verify_and_consume`
    for the single-use guarantee (which is what login flows need).
    """
    now = int(time.time()) if now is None else now
    return _check_login_token(secret, token, now) is not None


class LoginTokenStore:
    """Persists consumed one-time login-token digests so a token spent before a
    daemon restart cannot be replayed within its TTL.

    Stores only the SHA-256 digest of the canonical token (never the raw bearer
    string), mapped to its expiry epoch. All access is under an interprocess
    lock, so two concurrent login attempts with the same token cannot both
    succeed, and entries past their TTL are pruned to keep the store bounded.
    """

    def __init__(self, path: Path):
        self._path = path
        self._lock = path.with_name(path.name + ".lock")
        self._consumed: dict[str, int] = {}

    def _load(self) -> dict[str, int]:
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as e:
            raise ValueError(f"cannot read consumed-login store: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("consumed-login store root is not an object")
        if any(
            not isinstance(k, str)
            or re.fullmatch(r"[0-9a-f]{64}", k) is None
            or not isinstance(v, int)
            or isinstance(v, bool)
            for k, v in data.items()
        ):
            raise ValueError("consumed-login store contains an invalid entry")
        return data

    def _persist(self, consumed: dict[str, int]) -> None:
        fsio.write_durably(
            self._path,
            lambda f: json.dump(consumed, f),
            prefix=".web_consumed-",
            suffix=".json",
            mode=0o600,
        )

    @staticmethod
    def _pruned(consumed: dict[str, int], now: int) -> dict[str, int]:
        return {digest: exp for digest, exp in consumed.items() if exp > now}

    def verify_and_consume(
        self, secret: str, token: str, *, now: int | None = None
    ) -> bool:
        """Validate ``token`` and, if good, record its digest as consumed —
        atomically, so a concurrent replay of the same token fails. Returns
        False for an already-consumed, expired, non-canonical, or badly-signed
        token."""
        now = int(time.time()) if now is None else now
        checked = _check_login_token(secret, token, now)
        if checked is None:
            return False
        canonical, issued = checked
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        expiry = issued + LOGIN_TTL + 1
        with fsio.locked(self._lock):
            try:
                consumed = self._pruned(self._load(), now)
            except ValueError:
                return False  # preserve malformed/unreadable evidence; fail closed
            if digest in consumed:
                return False
            candidate = {**consumed, digest: expiry}
            try:
                self._persist(candidate)
            except OSError:
                return False
            self._consumed = candidate
            return True


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
