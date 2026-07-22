"""Resolution order: explicit arg -> WORKSPACE_SLUG default -> error.

Uses a rest-only config so AppState builds without a DB connection.
"""

import pytest

from plane_pages_mcp.config import Config
from plane_pages_mcp.server import AppState, WorkspaceUnset


def _cfg(monkeypatch, default_slug):
    for v in ["DATABASE_URL", "SERVICE_USER_ID", "MCP_AUTH_TOKEN"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    if default_slug is None:
        monkeypatch.delenv("WORKSPACE_SLUG", raising=False)
    else:
        monkeypatch.setenv("WORKSPACE_SLUG", default_slug)
    return Config.from_env()


def test_explicit_wins(monkeypatch):
    state = AppState(_cfg(monkeypatch, "default-ws"))
    try:
        assert state.workspace_slug("explicit-ws") == "explicit-ws"
    finally:
        state.close()


def test_falls_back_to_default(monkeypatch):
    state = AppState(_cfg(monkeypatch, "default-ws"))
    try:
        assert state.workspace_slug(None) == "default-ws"
    finally:
        state.close()


def test_error_when_neither(monkeypatch):
    state = AppState(_cfg(monkeypatch, None))
    try:
        with pytest.raises(WorkspaceUnset):
            state.workspace_slug(None)
    finally:
        state.close()
