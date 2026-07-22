"""Runtime configuration, loaded from environment variables.

See README §Configuration for the full table. Nothing here reads page content;
secrets (DATABASE_URL, MCP_AUTH_TOKEN) are never logged.
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
    database_url: str
    live_convert_url: str
    workspace_slug: str
    service_user_id: str
    web_url: str
    mcp_auth_token: str | None
    transport: str  # "http" | "stdio"
    host: str
    port: int
    mcp_path: str
    log_level: str

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

        return cls(
            database_url=_require("DATABASE_URL"),
            live_convert_url=os.environ.get(
                "LIVE_CONVERT_URL",
                "http://live:3000/live/convert-document/",
            ),
            workspace_slug=_require("WORKSPACE_SLUG"),
            service_user_id=_require("SERVICE_USER_ID"),
            web_url=os.environ.get("PLANE_WEB_URL", "http://localhost").rstrip("/"),
            mcp_auth_token=auth_token,
            transport=transport,
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            port=int(os.environ.get("MCP_PORT", "8300")),
            mcp_path=os.environ.get("MCP_PATH", "/mcp"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        )
