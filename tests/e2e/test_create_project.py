"""create_project against a real Plane, through the actual MCP tool.

Needs PLANE_E2E_API_KEY: a Plane API token (Profile settings -> API tokens)
whose user is an Admin or Member of PLANE_E2E_WORKSPACE. Every project created
here is deleted again at the end of its test.

Two dev-only users are created once in the workspace (plane_dev.py): a plain
Member (e2e-member@plane.local) and a Guest (e2e-guest@plane.local).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

API_KEY = os.environ.get("PLANE_E2E_API_KEY")
pytestmark = pytest.mark.skipif(not API_KEY, reason="PLANE_E2E_API_KEY not set")


@pytest.fixture
def call_tool(config, monkeypatch, service_user_id):
    """Call an MCP tool the way a client would, via FastMCP's in-memory client.

    ``call_tool(name, args, default_lead=...)`` builds the server fresh, with
    REST and the database (needed to make projects private), on stdio so no
    OAuth is involved. ``default_lead`` sets DEFAULT_PROJECT_LEAD.
    """
    from fastmcp import Client

    from plane_pages_mcp.config import Config
    from plane_pages_mcp.server import AppState, build_mcp

    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("PLANE_BASE_URL", config.web_url)
    monkeypatch.setenv("PLANE_API_KEY", API_KEY)
    monkeypatch.setenv("DATABASE_URL", config.database_url)
    monkeypatch.setenv("SERVICE_USER_ID", service_user_id)
    monkeypatch.delenv("REDIS_URL", raising=False)

    async def _call(mcp, name, arguments):
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments, raise_on_error=False)
            return result.structured_content or result.data

    def call(name, arguments, *, default_lead=None):
        if default_lead:
            monkeypatch.setenv("DEFAULT_PROJECT_LEAD", default_lead)
        else:
            monkeypatch.delenv("DEFAULT_PROJECT_LEAD", raising=False)
        state = AppState(Config.from_env())
        try:
            return asyncio.run(_call(build_mcp(state), name, arguments))
        finally:
            state.close()

    return call


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


# --- lead, Guest rule, visibility -------------------------------------------------

MEMBER_EMAIL = "e2e-member@plane.local"
GUEST_EMAIL = "e2e-guest@plane.local"


@pytest.fixture(scope="module")
def dev_users(config):
    from plane_dev import ensure_workspace_user

    return {
        "member": ensure_workspace_user(config, MEMBER_EMAIL, "e2e-member", 15),
        "guest": ensure_workspace_user(config, GUEST_EMAIL, "e2e-guest", 5),
    }


def _project_role(config, project_id: str, user_id: str) -> int | None:
    """The user's role in the project, straight from Plane's database."""
    import psycopg

    with psycopg.connect(config.database_url) as conn:
        row = conn.execute(
            "SELECT role FROM project_members WHERE project_id = %s AND member_id = %s "
            "AND deleted_at IS NULL AND is_active", (project_id, user_id),
        ).fetchone()
    return row[0] if row else None


def _network(config, project_id: str) -> int:
    import psycopg

    with psycopg.connect(config.database_url) as conn:
        return conn.execute("SELECT network FROM projects WHERE id = %s", (project_id,)).fetchone()[0]


def _visible_to(config, email: str, project_id: str) -> bool:
    """Does the project appear in this user's project list in the Plane web app?"""
    import httpx
    from plane_dev import mint_session

    with httpx.Client(base_url=config.web_url,
                      cookies={"session-id": mint_session(config, email)}) as web:
        data = web.get(f"/api/workspaces/{config.workspace_slug}/projects/").json()
    rows = data if isinstance(data, list) else data.get("results", [])
    return any(p["id"] == project_id for p in rows)


def test_lead_becomes_project_admin_of_a_private_project(call_tool, config, created_projects, dev_users):
    # What this checks: create_project with a lead (a plain workspace Member)
    # makes that person a project Admin (role 20 in Plane's database), the
    # project is private (network 0), and there are no warning notes.
    # Why: this is the fix for "the person who asked can't see the project".
    identifier = _fresh_identifier()
    result = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": f"e2e {identifier}",
        "identifier": identifier, "lead": MEMBER_EMAIL,
    })
    created_projects.append(result["id"])
    assert result["lead"] == MEMBER_EMAIL and result["notes"] == []
    assert _project_role(config, result["id"], dev_users["member"]) == 20
    assert result["visibility"] == "private" and _network(config, result["id"]) == 0


def test_default_project_lead_is_used(call_tool, config, created_projects, dev_users):
    # What this checks: with DEFAULT_PROJECT_LEAD set and no lead argument,
    # the default person becomes project Admin.
    # Why: this is how the operator makes every new project visible to
    # themselves without the agent naming them each time.
    identifier = _fresh_identifier()
    result = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": f"e2e {identifier}", "identifier": identifier,
    }, default_lead=MEMBER_EMAIL)
    created_projects.append(result["id"])
    assert _project_role(config, result["id"], dev_users["member"]) == 20


def test_guest_lead_is_refused_and_no_project_is_created(call_tool, config, dev_users):
    # What this checks: asking for a workspace Guest as lead returns an error,
    # and no project with that identifier exists afterwards (not even a
    # deleted one).
    # Why: the refusal must come before Plane is asked to create anything, so
    # it never leaves a half-made project behind.
    import psycopg

    identifier = _fresh_identifier()
    result = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": f"e2e {identifier}",
        "identifier": identifier, "lead": GUEST_EMAIL,
    })
    assert "error" in result and "workspace Guest" in result["error"]
    with psycopg.connect(config.database_url) as conn:
        count = conn.execute("SELECT count(*) FROM projects WHERE identifier = %s",
                             (identifier,)).fetchone()[0]
    assert count == 0


def test_private_project_is_hidden_until_the_member_is_added(call_tool, config, created_projects, dev_users):
    # What this checks: a private project created without a lead does not
    # appear in a workspace member's project list in the Plane web app; the
    # result says only the service user can see it; and after
    # add_project_member it does appear.
    # Why: this is the exact situation the notes warn about, and the fix they
    # recommend. Checked through the web app's own API, as that person.
    identifier = _fresh_identifier()
    result = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": f"e2e {identifier}", "identifier": identifier,
    })
    created_projects.append(result["id"])
    assert "nobody else can see it" in " ".join(result["notes"])
    assert not _visible_to(config, MEMBER_EMAIL, result["id"])

    added = call_tool("add_project_member", {
        "workspace": config.workspace_slug, "project": identifier,
        "member": MEMBER_EMAIL, "role": "member",
    })
    assert added.get("role") == "member", added
    assert _visible_to(config, MEMBER_EMAIL, result["id"])
    assert _project_role(config, result["id"], dev_users["member"]) == 15


def test_add_project_member_refuses_a_guest(call_tool, config, created_projects, dev_users):
    # What this checks: add_project_member won't add a workspace Guest.
    # Why: otherwise it would be an easy way around the Guest rule.
    identifier = _fresh_identifier()
    result = call_tool("create_project", {
        "workspace": config.workspace_slug, "name": f"e2e {identifier}", "identifier": identifier,
    })
    created_projects.append(result["id"])
    refused = call_tool("add_project_member", {
        "workspace": config.workspace_slug, "project": identifier,
        "member": GUEST_EMAIL, "role": "member",
    })
    assert "workspace Guest" in refused.get("error", "")
    assert _project_role(config, result["id"], dev_users["guest"]) is None
