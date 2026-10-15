"""create_project against a real Plane, through the actual MCP tool.

Needs PLANE_E2E_API_KEY: a Plane API token (Profile settings -> API tokens)
whose user is an Admin or Member of PLANE_E2E_WORKSPACE. Every project created
here is deleted again at the end of its test.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

API_KEY = os.environ.get("PLANE_E2E_API_KEY")
pytestmark = pytest.mark.skipif(not API_KEY, reason="PLANE_E2E_API_KEY not set")


@pytest.fixture
def call_tool(config, monkeypatch):
    """Call an MCP tool the way a client would, via FastMCP's in-memory client."""
    from fastmcp import Client

    from plane_pages_mcp.config import Config
    from plane_pages_mcp.server import AppState, build_mcp

    # REST only, stdio (no OAuth): just enough to register create_project.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("PLANE_BASE_URL", config.web_url)
    monkeypatch.setenv("PLANE_API_KEY", API_KEY)
    state = AppState(Config.from_env())
    mcp = build_mcp(state)

    async def _call(name, arguments):
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments, raise_on_error=False)
            return result.structured_content or result.data

    yield lambda name, arguments: asyncio.run(_call(name, arguments))
    state.close()


@pytest.fixture
def created_projects(config):
    """Collects ids of projects a test created, and deletes them afterwards."""
    from plane_pages_mcp.rest import PlaneREST

    ids: list[str] = []
    yield ids
    rest = PlaneREST(config.web_url, API_KEY)
    for project_id in ids:
        rest._request("DELETE", f"workspaces/{config.workspace_slug}/projects/{project_id}/")
    rest.close()


def _fresh_identifier() -> str:
    return "ZZ" + uuid.uuid4().hex[:6].upper()


def test_create_project_returns_id_and_identifier(call_tool, config, created_projects):
    # What this checks: the create_project tool creates a real project in
    # Plane and returns its id and its identifier as Plane stored it
    # (uppercase, even though we sent lowercase).
    # Why: this is the whole feature. Only a real Plane shows that the
    # request is accepted and the response has the fields we read.
    identifier = _fresh_identifier()
    result = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": f"e2e {identifier}",
        "identifier": identifier.lower(), "description": "created by the e2e harness",
    })
    created_projects.append(result["id"])
    assert result["identifier"] == identifier
    assert uuid.UUID(result["id"])


def test_duplicate_identifier_gets_the_clear_message(call_tool, config, created_projects):
    # What this checks: creating a second project with the same identifier
    # (different name) fails with our message naming the identifier, and not
    # Plane's misleading "The project name is already taken".
    # Why: this is the error mapping the task asks for, checked against the
    # 409 that Plane v1.3.1 actually sends.
    identifier = _fresh_identifier()
    first = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": f"e2e {identifier}", "identifier": identifier,
    })
    created_projects.append(first["id"])

    second = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": f"e2e other {identifier}",
        "identifier": identifier,
    })
    assert "error" in second
    assert f"identifier '{identifier}' is already used" in second["error"]


def test_invalid_identifier_never_reaches_plane(call_tool, config):
    # What this checks: an identifier that breaks the rule ("MY_PROJ" has an
    # underscore) returns our validation error, and no project is created.
    # Why: Plane's API would have accepted it, creating a project that the
    # Plane UI then refuses to save when someone edits its settings.
    result = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": "e2e invalid", "identifier": "MY_PROJ",
    })
    assert "error" in result and "only letters" in result["error"]
