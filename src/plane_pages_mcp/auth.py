"""GitHub OAuth for the http transport.

FastMCP's ``GitHubProvider`` (an OAuth proxy) authenticates *identity*; it does
not decide *authorization*. Without a further gate, any GitHub account on Earth
could complete the flow and reach the tools. ``AllowlistMiddleware`` is that
gate: it runs on every tool list/call, extracts the authenticated GitHub login,
and fails closed unless the login is in ``ALLOWED_GITHUB_LOGINS``.

Persistence (WP requirement): FastMCP's Linux defaults are an ephemeral signing
key and disk storage under a key-derived path — restarts invalidate tokens. We
pin the signing key (``JWT_SIGNING_KEY``) and use the stack's Redis, encrypted
at rest with a Fernet wrapper keyed by ``STORAGE_ENCRYPTION_KEY``.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp.exceptions import AuthorizationError
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from key_value.aio.stores.redis import RedisStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

from .config import Config

log = logging.getLogger("plane_pages_mcp.auth")

# Client redirect URIs allowed to receive the authorization code. Defense in
# depth — the identity allowlist is the real gate (a Jan 2026 upstream issue
# showed DCR clients bypassing this list in older, now-hardened releases).
ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]

# Cache GitHub /user validations briefly so we don't hit GitHub on every call
# (rate limits). Identity rarely changes within this window.
_USER_VALIDATION_CACHE_TTL = 300


def build_storage(cfg: Config) -> FernetEncryptionWrapper:
    """Persistent, encrypted-at-rest client storage backed by the stack's Redis."""
    store = RedisStore(url=cfg.redis_url)
    return FernetEncryptionWrapper(
        key_value=store,
        source_material=cfg.storage_encryption_key,
        salt="plane-pages-mcp-oauth-storage",
    )


def build_auth(cfg: Config) -> GitHubProvider:
    """Construct the GitHubProvider with persistent, encrypted storage."""
    return GitHubProvider(
        client_id=cfg.github_client_id,
        client_secret=cfg.github_client_secret,
        base_url=cfg.public_base_url,
        allowed_client_redirect_uris=ALLOWED_CLIENT_REDIRECT_URIS,
        client_storage=build_storage(cfg),
        jwt_signing_key=cfg.jwt_signing_key,
        fastmcp_access_token_expiry_seconds=cfg.token_expiry_seconds,
        cache_ttl_seconds=_USER_VALIDATION_CACHE_TTL,
        # require_authorization_consent=True and enable_cimd=True are the
        # defaults — kept for the consent screen (part of the security model)
        # and Claude Code's Client ID Metadata Document flow.
    )


def extract_github_login(claims: dict[str, Any] | None) -> str | None:
    """Pull the GitHub login from an access token's claims, defensively.

    Primary location (verified against FastMCP 3.4.4): claims["login"], set by
    GitHubTokenVerifier from the GitHub /user API on each request. We also try
    the nested fallbacks so a future FastMCP change in claim shaping degrades to
    a clear denial rather than a crash.
    """
    if not claims:
        return None
    login = claims.get("login")
    if not login and isinstance(claims.get("upstream_claims"), dict):
        login = claims["upstream_claims"].get("login")
    if not login and isinstance(claims.get("github_user_data"), dict):
        login = claims["github_user_data"].get("login")
    return login


class AllowlistMiddleware(Middleware):
    """Fail-closed identity gate applied to every tool list and call."""

    def __init__(self, allowed_logins: frozenset[str]) -> None:
        # GitHub logins are case-insensitive (you can't register both "Alice"
        # and "alice"), so compare case-insensitively to avoid locking out a
        # legitimate user over allowlist casing.
        self._allowed = {login.lower() for login in allowed_logins}
        self._logged_claims_once = False

    def _authorize(self) -> str:
        token = get_access_token()
        claims = getattr(token, "claims", None) if token else None

        # One-time DEBUG dump so an operator can confirm the claim shape without
        # guessing (WP Phase-0 discipline). Never logged above DEBUG.
        if not self._logged_claims_once:
            log.debug("access token claims keys: %s", sorted((claims or {}).keys()))
            self._logged_claims_once = True

        login = extract_github_login(claims)
        if not login:
            raise AuthorizationError(
                "could not determine GitHub identity from the access token"
            )
        if login.lower() not in self._allowed:
            log.warning("denied GitHub login %r (not in allowlist)", login)
            raise AuthorizationError(
                f"GitHub account {login!r} is not authorized for this server"
            )
        return login

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        self._authorize()
        return await call_next(context)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        self._authorize()
        return await call_next(context)
