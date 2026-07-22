"""Runtime configuration, loaded from environment variables.

See README §Configuration for the full table. Nothing here reads page content;
secrets (DATABASE_URL, PLANE_API_KEY, MCP_AUTH_TOKEN) are never logged.

Two independent subsystems, each with its own capability flag:
  * pages  (DB + live converter) — needs DATABASE_URL (+ SERVICE_USER_ID to write).
  * rest   (public REST API)      — needs PLANE_BASE_URL + PLANE_API_KEY.
The server starts if at least one is configured; a missing PAT never breaks
pages and a missing DB never breaks work items.
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
    # shared
    workspace_slug: str | None  # now a DEFAULT, not a fixture
    mcp_auth_token: str | None
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

    @classmethod
    def from_env(cls) -> "Config":
        transport = os.environ.get("MCP_TRANSPORT", "http").strip().lower()
        if transport not in ("http", "stdio"):
            raise ConfigError(
                f"MCP_TRANSPORT must be 'http' or 'stdio', got {transport!r}"
            )

        auth_token = os.environ.get("MCP_AUTH_TOKEN") or None
        if transport == "http" and not auth_token:
            raise ConfigError(
                "MCP_AUTH_TOKEN is required in http mode "
                "(all requests must present a matching Bearer token)"
            )

        database_url = os.environ.get("DATABASE_URL") or None
        service_user_id = os.environ.get("SERVICE_USER_ID") or None
        plane_base_url = os.environ.get("PLANE_BASE_URL")
        plane_base_url = plane_base_url.rstrip("/") if plane_base_url else None
        plane_api_key = os.environ.get("PLANE_API_KEY") or None

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
            workspace_slug=os.environ.get("WORKSPACE_SLUG") or None,
            mcp_auth_token=auth_token,
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
        return cfg
