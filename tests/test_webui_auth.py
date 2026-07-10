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
