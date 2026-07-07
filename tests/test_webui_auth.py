"""Web UI auth primitives: single-use login tokens, stateless session cookies,
bearer + secret checks — all keyed off the one control_api secret."""

from __future__ import annotations

from alle.webui import auth

SECRET = "s3cr3t-abc"


def test_login_token_roundtrip_is_single_use():
    consumed: set[str] = set()
    tok = auth.mint_login_token(SECRET)
    assert auth.verify_login_token(SECRET, tok, consumed) is True
    # replaying the same token fails — it's been consumed
    assert auth.verify_login_token(SECRET, tok, consumed) is False


def test_login_token_rejects_wrong_secret_and_garbage():
    consumed: set[str] = set()
    tok = auth.mint_login_token(SECRET)
    assert auth.verify_login_token("other", tok, consumed) is False
    assert auth.verify_login_token(SECRET, "not-a-token", consumed) is False
    assert auth.verify_login_token(SECRET, "", consumed) is False


def test_login_token_expires():
    consumed: set[str] = set()
    tok = auth.mint_login_token(SECRET, now=1000)
    assert (
        auth.verify_login_token(SECRET, tok, consumed, now=1000 + auth.LOGIN_TTL)
        is True
    )
    fresh = auth.mint_login_token(SECRET, now=1000)
    assert (
        auth.verify_login_token(SECRET, fresh, set(), now=1000 + auth.LOGIN_TTL + 5)
        is False
    )


def test_session_cookie_roundtrip_and_expiry():
    cookie = auth.make_session(SECRET, now=1000)
    assert auth.verify_session(SECRET, cookie, now=1000) is True
    assert auth.verify_session(SECRET, cookie, now=1000 + auth.SESSION_TTL - 1) is True
    assert auth.verify_session(SECRET, cookie, now=1000 + auth.SESSION_TTL + 1) is False
    assert auth.verify_session("other", cookie, now=1000) is False
    assert auth.verify_session(SECRET, None) is False
    assert auth.verify_session(SECRET, "garbage") is False


def test_bearer_and_secret_checks_are_constant_style():
    assert auth.check_bearer(SECRET, f"Bearer {SECRET}") is True
    assert auth.check_bearer(SECRET, "Bearer wrong") is False
    assert auth.check_bearer(SECRET, SECRET) is False  # missing "Bearer " prefix
    assert auth.check_bearer(SECRET, None) is False
    assert auth.secret_matches(SECRET, SECRET) is True
    assert auth.secret_matches(SECRET, "nope") is False
