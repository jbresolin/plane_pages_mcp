"""Runtime configuration, loaded from environment variables.

See README §Configuration for the full table. Nothing here reads page content;
secrets (DATABASE_URL, PLANE_API_KEY, GITHUB_CLIENT_SECRET, JWT_SIGNING_KEY,
STORAGE_ENCRYPTION_KEY) are never logged.

Two independent subsystems, each with its own capability flag:
  * pages  (DB + live converter) — needs DATABASE_URL (+ SERVICE_USER_ID to write).
  * rest   (public REST API)      — needs PLANE_BASE_URL + PLANE_API_KEY.
The server starts if at least one is configured.

Auth (http transport only): GitHub OAuth via FastMCP's GitHubProvider, gated by
an allowlist of GitHub logins. stdio transport is auth-free (on-box use). In http
mode the server fails fast at startup if any required OAuth var is missing — a
public OAuth server must not come up half-armed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ConfigError(f"required environment variable {name} is not set")
    return val


@dataclass(frozen=True)
class Config:
    # pages subsystem (all optional at the config layer; gated by pages_enabled)
    database_url: str | None
    live_convert_url: str
    service_user_id: str | None
    web_url: str
    # rest subsystem
    plane_base_url: str | None
    plane_api_key: str | None
    # auth (http transport only)
    github_client_id: str | None
    github_client_secret: str | None
    public_base_url: str | None
    allowed_github_logins: frozenset[str]
    jwt_signing_key: str | None
    storage_encryption_key: str | None
    redis_url: str | None
    token_expiry_seconds: int
    # shared
    workspace_slug: str | None  # a DEFAULT, not a fixture
    transport: str  # "http" | "stdio"
    host: str
    port: int
    mcp_path: str
    log_level: str

    @property
    def pages_enabled(self) -> bool:
        return bool(self.database_url)

    @property
    def rest_enabled(self) -> bool:
        return bool(self.plane_base_url and self.plane_api_key)

    @property
    def auth_enabled(self) -> bool:
        """OAuth is active only on the http transport (stdio is auth-free)."""
        return self.transport == "http"

    @classmethod
    def from_env(cls) -> "Config":
        transport = os.environ.get("MCP_TRANSPORT", "http").strip().lower()
        if transport not in ("http", "stdio"):
            raise ConfigError(
                f"MCP_TRANSPORT must be 'http' or 'stdio', got {transport!r}"
            )

        database_url = os.environ.get("DATABASE_URL") or None
        service_user_id = os.environ.get("SERVICE_USER_ID") or None
        plane_base_url = os.environ.get("PLANE_BASE_URL")
        plane_base_url = plane_base_url.rstrip("/") if plane_base_url else None
        plane_api_key = os.environ.get("PLANE_API_KEY") or None

        public_base_url = os.environ.get("PUBLIC_BASE_URL")
        public_base_url = public_base_url.rstrip("/") if public_base_url else None
        logins = frozenset(
            s.strip()
            for s in os.environ.get("ALLOWED_GITHUB_LOGINS", "").split(",")
            if s.strip()
        )

        cfg = cls(
            database_url=database_url,
            live_convert_url=os.environ.get(
                "LIVE_CONVERT_URL",
                "http://live:3000/live/convert-document/",
            ),
            service_user_id=service_user_id,
            web_url=os.environ.get("PLANE_WEB_URL", "http://localhost").rstrip("/"),
            plane_base_url=plane_base_url,
            plane_api_key=plane_api_key,
            github_client_id=os.environ.get("GITHUB_CLIENT_ID") or None,
            github_client_secret=os.environ.get("GITHUB_CLIENT_SECRET") or None,
            public_base_url=public_base_url,
            allowed_github_logins=logins,
            jwt_signing_key=os.environ.get("JWT_SIGNING_KEY") or None,
            storage_encryption_key=os.environ.get("STORAGE_ENCRYPTION_KEY") or None,
            redis_url=os.environ.get("REDIS_URL") or None,
            token_expiry_seconds=int(os.environ.get("TOKEN_EXPIRY_SECONDS", "604800")),
            workspace_slug=os.environ.get("WORKSPACE_SLUG") or None,
            transport=transport,
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "8300")),
            mcp_path=os.environ.get("MCP_PATH", "/mcp"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        )

        if not cfg.pages_enabled and not cfg.rest_enabled:
            raise ConfigError(
                "nothing to serve: set DATABASE_URL (pages) and/or "
                "PLANE_BASE_URL + PLANE_API_KEY (work items)"
            )
        if cfg.pages_enabled and not cfg.service_user_id:
            raise ConfigError(
                "SERVICE_USER_ID is required when DATABASE_URL is set "
                "(it owns tool-created pages)"
            )

        if transport == "http":
            cfg._require_oauth_config()
        return cfg

    def _require_oauth_config(self) -> None:
        """Fail fast if the http transport is missing any OAuth requirement."""
        missing = [
            name
            for name, val in {
                "GITHUB_CLIENT_ID": self.github_client_id,
                "GITHUB_CLIENT_SECRET": self.github_client_secret,
                "PUBLIC_BASE_URL": self.public_base_url,
                "JWT_SIGNING_KEY": self.jwt_signing_key,
                "REDIS_URL": self.redis_url,
                "STORAGE_ENCRYPTION_KEY": self.storage_encryption_key,
            }.items()
            if not val
        ]
        if not self.allowed_github_logins:
            missing.append("ALLOWED_GITHUB_LOGINS")
        if missing:
            raise ConfigError(
                "http transport uses GitHub OAuth and is missing required "
                f"configuration: {', '.join(sorted(missing))}. "
                "(Use MCP_TRANSPORT=stdio for auth-free on-box access.)"
            )
        if self.jwt_signing_key and len(self.jwt_signing_key) < 12:
            raise ConfigError("JWT_SIGNING_KEY must be at least 12 characters")
