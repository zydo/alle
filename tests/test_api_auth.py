"""Web UI auth primitives: single-use login tokens, stateless session cookies,
bearer + secret checks — all keyed off the one control_api secret."""

from __future__ import annotations

from alle import paths
from alle.api import auth

SECRET = "s3cr3t-abc"


def _store():
    """A fresh LoginTokenStore on the isolated state dir."""
    return auth.LoginTokenStore(paths.state_dir() / "web_consumed.json")


def test_login_token_roundtrip_is_single_use():
    store = _store()
    tok = auth.mint_login_token(SECRET)
    assert store.verify_and_consume(SECRET, tok) is True
    # replaying the same token fails — it's been consumed
    assert store.verify_and_consume(SECRET, tok) is False


def test_login_token_rejects_wrong_secret_and_garbage():
    tok = auth.mint_login_token(SECRET)
    assert auth.verify_login_token("other", tok) is False
    assert auth.verify_login_token(SECRET, "not-a-token") is False
    assert auth.verify_login_token(SECRET, "") is False


def test_login_token_expires():
    tok = auth.mint_login_token(SECRET, now=1000)
    assert auth.verify_login_token(SECRET, tok, now=1000 + auth.LOGIN_TTL) is True
    fresh = auth.mint_login_token(SECRET, now=1000)
    assert (
        auth.verify_login_token(SECRET, fresh, now=1000 + auth.LOGIN_TTL + 5) is False
    )


def _tamper_body(tok: str) -> str:
    """Return the token with one body byte altered so it is no longer the
    canonical form (a different valid base64url char → a different decoded
    byte → signature/payload mismatch). Deterministic: the payload always
    base64-encodes to at least one letter we can flip."""
    body, sig = tok.split(".", 1)
    for i, ch in enumerate(body):
        if ch.isalpha():
            body = body[:i] + ch.swapcase() + body[i + 1 :]
            break
    else:  # all digits/symbols (astronomically unlikely) — truncate instead
        body = body[:-1]
    return f"{body}.{sig}"


def test_non_canonical_token_encoding_is_rejected():
    tok = auth.mint_login_token(SECRET)
    assert auth.verify_login_token(SECRET, tok) is True  # canonical accepted
    # a tampered encoding is never a second valid spelling — the canonical form
    # is required, so single-use consumption keys on exactly one byte string
    assert auth.verify_login_token(SECRET, _tamper_body(tok)) is False


def test_consumed_token_survives_a_daemon_restart():
    tok = auth.mint_login_token(SECRET)
    store = _store()
    assert store.verify_and_consume(SECRET, tok) is True
    # a brand-new store (simulating a daemon restart, same state dir) still
    # remembers the token was spent — no replay within its TTL
    reopened = _store()
    assert reopened.verify_and_consume(SECRET, tok) is False


def test_consumed_token_is_pruned_after_ttl():
    tok = auth.mint_login_token(SECRET, now=1000)
    store = _store()
    assert store.verify_and_consume(SECRET, tok, now=1000) is True
    # past the TTL the digest is pruned on the next access (bounded store), and
    # the token is expired anyway — still rejected, now for both reasons
    assert store.verify_and_consume(SECRET, tok, now=1000 + auth.LOGIN_TTL + 5) is False


def test_only_the_token_digest_is_persisted():
    tok = auth.mint_login_token(SECRET)
    store = _store()
    store.verify_and_consume(SECRET, tok)
    raw = (paths.state_dir() / "web_consumed.json").read_text()
    # the raw bearer token (which carries the signed payload) is not on disk —
    # only its digest
    assert tok not in raw
    assert tok.split(".")[0] not in raw  # the payload half neither


def test_session_cookie_roundtrip_and_idle_expiry():
    cookie = auth.make_session(SECRET, now=1000)
    assert auth.verify_session(SECRET, cookie, now=1000) is True
    assert auth.verify_session(SECRET, cookie, now=1000 + auth.SESSION_IDLE - 1) is True
    assert (
        auth.verify_session(SECRET, cookie, now=1000 + auth.SESSION_IDLE + 1) is False
    )
    assert auth.verify_session("other", cookie, now=1000) is False
    assert auth.verify_session(SECRET, None) is False
    assert auth.verify_session(SECRET, "garbage") is False
    # the pre-idle-sessions format ("<expiry>.<sig>") is simply invalid now
    assert auth.verify_session(SECRET, "2000.abcdef", now=1000) is False


def test_session_refresh_rolls_but_caps_at_session_max():
    cookie = auth.make_session(SECRET, now=1000)
    # still fresh: no re-issue churn on every poll
    assert auth.refresh_session(SECRET, cookie, now=1001) is None
    # past half the idle window: rolled, and the roll extends validity …
    later = 1000 + auth.SESSION_IDLE - 10
    rolled = auth.refresh_session(SECRET, cookie, now=later)
    assert rolled is not None
    assert (
        auth.verify_session(SECRET, rolled, now=later + auth.SESSION_IDLE - 1) is True
    )
    # … but the original issue time rides along, so SESSION_MAX still caps
    assert rolled.split(".", 1)[0] == "1000"
    near_cap = 1000 + auth.SESSION_MAX - 5
    capped = auth.make_session(SECRET, now=near_cap, issued=1000)
    assert auth.verify_session(SECRET, capped, now=near_cap) is True
    assert auth.refresh_session(SECRET, capped, now=near_cap) is None  # no roll
    assert auth.verify_session(SECRET, capped, now=1000 + auth.SESSION_MAX + 1) is False


def test_session_revocation_kills_older_sessions_only():
    old = auth.make_session(SECRET, now=1000)
    assert auth.verify_session(SECRET, old, now=1100, revoked_at=1050) is False
    # issued in the same second as the logout: also dead (the issuer mints
    # post-logout sessions with issued = revoked_at + 1)
    same = auth.make_session(SECRET, now=1050)
    assert auth.verify_session(SECRET, same, now=1100, revoked_at=1050) is False
    fresh = auth.make_session(SECRET, now=1051, issued=1051)
    assert auth.verify_session(SECRET, fresh, now=1100, revoked_at=1050) is True
    # a forged cookie claiming a post-revocation issue time still fails the HMAC
    forged = "1060." + old.split(".", 1)[1]
    assert auth.verify_session(SECRET, forged, now=1100, revoked_at=1050) is False


def test_bearer_and_secret_checks_are_constant_style():
    assert auth.check_bearer(SECRET, f"Bearer {SECRET}") is True
    assert auth.check_bearer(SECRET, "Bearer wrong") is False
    assert auth.check_bearer(SECRET, SECRET) is False  # missing "Bearer " prefix
    assert auth.check_bearer(SECRET, None) is False
    assert auth.secret_matches(SECRET, SECRET) is True
    assert auth.secret_matches(SECRET, "nope") is False
