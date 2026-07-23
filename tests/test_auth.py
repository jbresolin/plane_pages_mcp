"""Identity allowlist logic: login extraction + fail-closed gate."""

import pytest

from fastmcp.exceptions import AuthorizationError

from plane_pages_mcp import auth


# --- extract_github_login ------------------------------------------------


def test_extract_login_primary_claim():
    assert auth.extract_github_login({"login": "octocat"}) == "octocat"


def test_extract_login_from_upstream_claims():
    assert auth.extract_github_login({"upstream_claims": {"login": "octocat"}}) == "octocat"


def test_extract_login_from_github_user_data():
    assert auth.extract_github_login({"github_user_data": {"login": "octocat"}}) == "octocat"


def test_extract_login_primary_wins():
    claims = {"login": "real", "github_user_data": {"login": "other"}}
    assert auth.extract_github_login(claims) == "real"


def test_extract_login_none_when_absent():
    assert auth.extract_github_login({"sub": "123"}) is None
    assert auth.extract_github_login(None) is None
    assert auth.extract_github_login({}) is None


# --- AllowlistMiddleware._authorize (fail-closed) ------------------------


class _Tok:
    def __init__(self, claims):
        self.claims = claims


def _mw(monkeypatch, token):
    mw = auth.AllowlistMiddleware(frozenset({"alice", "bob"}))
    monkeypatch.setattr(auth, "get_access_token", lambda: token)
    return mw


def test_authorize_allows_listed_login(monkeypatch):
    mw = _mw(monkeypatch, _Tok({"login": "alice"}))
    assert mw._authorize() == "alice"


def test_authorize_denies_unlisted_login(monkeypatch):
    mw = _mw(monkeypatch, _Tok({"login": "mallory"}))
    with pytest.raises(AuthorizationError) as exc:
        mw._authorize()
    assert "mallory" in str(exc.value) and "not authorized" in str(exc.value)


def test_authorize_denies_when_no_token(monkeypatch):
    mw = _mw(monkeypatch, None)
    with pytest.raises(AuthorizationError):
        mw._authorize()


def test_authorize_denies_when_no_login_claim(monkeypatch):
    mw = _mw(monkeypatch, _Tok({"sub": "123"}))
    with pytest.raises(AuthorizationError) as exc:
        mw._authorize()
    assert "could not determine GitHub identity" in str(exc.value)


def test_authorize_login_is_case_insensitive(monkeypatch):
    # GitHub logins are case-insensitive; "Alice" must match allowlisted "alice"
    # (else a casing mismatch would lock out a legitimate user).
    mw = _mw(monkeypatch, _Tok({"login": "Alice"}))
    assert mw._authorize() == "Alice"
