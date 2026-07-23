"""FastMCP server: pages tools (DB) + work-item tools (REST) + /healthz.

Two subsystems register independently based on configuration:
  * pages  — needs DATABASE_URL; multi-workspace, DB + live converter.
  * items  — needs PLANE_BASE_URL + PLANE_API_KEY; public REST API.
A missing PAT never breaks pages and a missing DB never breaks work items;
degraded capability is logged at startup and reported by ``verify``.

Transport:
  * http  — streamable HTTP on 0.0.0.0:8300/mcp, protected by GitHub OAuth
            (FastMCP GitHubProvider) + a GitHub-login allowlist. Unauthenticated
            requests get a spec-shaped 401 with a WWW-Authenticate header.
  * stdio — for on-box use (docker exec, Claude Code); no network auth.
"""

from __future__ import annotations

import logging

import uvicorn
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from . import auth, convert, pipeline, workitems
from .config import Config
from .db import Database, NotFoundError
from .live import ConvertError, LiveConverter
from .pipeline import WriteError
from .rest import PlaneREST, RestError
from .workitems import WorkItemError

log = logging.getLogger("plane_pages_mcp")


class WorkspaceUnset(ValueError):
    """Neither an explicit workspace argument nor WORKSPACE_SLUG was provided."""


class AppState:
    """Shared handles created once at startup (only for enabled subsystems)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db: Database | None = None
        self.live: LiveConverter | None = None
        self.rest: PlaneREST | None = None
        if cfg.pages_enabled:
            self.db = Database(cfg.database_url)
            self.live = LiveConverter(cfg.live_convert_url)
        if cfg.rest_enabled:
            self.rest = PlaneREST(cfg.plane_base_url, cfg.plane_api_key)

    def workspace_slug(self, explicit: str | None) -> str:
        """Resolution order: explicit argument -> WORKSPACE_SLUG env -> error."""
        slug = explicit or self.cfg.workspace_slug
        if not slug:
            raise WorkspaceUnset(
                "no workspace given: pass the `workspace` argument or set the "
                "WORKSPACE_SLUG default"
            )
        return slug

    def page_url(self, slug: str, page_id: str, project_id: str | None) -> str:
        base = f"{self.cfg.web_url}/{slug}"
        if project_id:
            return f"{base}/projects/{project_id}/pages/{page_id}"
        return f"{base}/pages/{page_id}"

    def close(self) -> None:
        if self.db:
            self.db.close()
        if self.rest:
            self.rest.close()


def build_mcp(state: AppState, auth_provider=None) -> FastMCP:
    mcp = FastMCP("plane_pages_mcp", auth=auth_provider)
    if state.db is not None:
        _register_page_tools(mcp, state)
    if state.rest is not None:
        _register_work_item_tools(mcp, state)

    # Identity allowlist gate (http/OAuth only). GitHubProvider proves *who* the
    # caller is; this decides *whether* they're allowed — applied globally.
    if auth_provider is not None:
        mcp.add_middleware(auth.AllowlistMiddleware(state.cfg.allowed_github_logins))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


# --- pages (DB) --------------------------------------------------------


def _register_page_tools(mcp: FastMCP, state: AppState) -> None:
    db = state.db

    @mcp.tool
    def list_pages(
        workspace: str | None = None,
        project: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> dict:
        """List Plane pages in a workspace, newest first.

        Args:
            workspace: workspace slug to read from (accepts a slug like "eng").
                Defaults to the server's configured WORKSPACE_SLUG when omitted;
                results are scoped to this workspace only.
            project: project identifier (e.g. "ENG") or UUID; omit for the whole
                workspace.
            include_archived: include archived pages when True.
            limit: max pages to return (default 50).
        """
        try:
            ws_id = db.resolve_workspace(state.workspace_slug(workspace))
            project_id = db.resolve_project(ws_id, project)["id"] if project else None
        except (NotFoundError, WorkspaceUnset) as exc:
            return _error(str(exc))
        rows = db.list_pages(ws_id, project_id, include_archived, _clamp(limit, 200))
        return {"pages": rows, "count": len(rows)}

    @mcp.tool
    def search_pages(query: str, workspace: str | None = None, limit: int = 20) -> dict:
        """Search page name + body text within one workspace (case-insensitive).

        Args:
            query: text to match on page name or body.
            workspace: workspace slug to search (defaults to the server's
                configured WORKSPACE_SLUG). Results are scoped to this workspace
                only — a search never returns another workspace's pages.
            limit: max hits (default 20).

        Each hit includes a ±120-char snippet around the first match.
        """
        if not query.strip():
            return _error("query must not be empty")
        try:
            ws_id = db.resolve_workspace(state.workspace_slug(workspace))
        except (NotFoundError, WorkspaceUnset) as exc:
            return _error(str(exc))
        rows = db.search_pages(ws_id, query, _clamp(limit, 100))
        return {"pages": rows, "count": len(rows)}

    @mcp.tool
    def read_page(page_id: str, format: str = "markdown") -> dict:
        """Read one page's metadata and content.

        The page id is globally unique, so no workspace is needed; the owning
        workspace slug is returned so you can tell where it came from.

        Args:
            page_id: the page UUID.
            format: "markdown" (default) or "html".
        """
        if format not in ("markdown", "html"):
            return _error("format must be 'markdown' or 'html'")
        row = db.read_page(page_id)
        if row is None:
            return _error(f"page {page_id} not found")
        html = row["description_html"] or ""
        content = html if format == "html" else convert.html_to_markdown(html)
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "workspace": row["workspace_slug"],
            "project_identifiers": list(row.get("project_identifiers") or []),
            "access": "private" if row["access"] == 1 else "public",
            "is_locked": row["is_locked"],
            "archived": row["archived_at"] is not None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "format": format,
            "content": content,
        }

    @mcp.tool
    def create_page(
        title: str,
        content: str,
        project: str,
        workspace: str,
        format: str = "markdown",
        access: str = "public",
    ) -> dict:
        """Create a new page in a project.

        Args:
            title: page title.
            content: markdown (default) or HTML body — see ``format``.
            project: project identifier or UUID the page belongs to.
            workspace: workspace slug — REQUIRED (no default, to avoid a page
                silently landing in the wrong workspace).
            format: "markdown" or "html".
            access: "public" or "private".
        """
        if not workspace:
            return _error("workspace is required for create_page (no default is applied)")
        try:
            ws_id = db.resolve_workspace(workspace)
            result = pipeline.create_page(
                db,
                state.live,
                workspace_id=ws_id,
                title=title,
                content=content,
                project=project,
                fmt=format,
                access=access,
                service_user_id=state.cfg.service_user_id,
            )
            project_id = db.resolve_project(ws_id, project)["id"]
        except (WriteError, ConvertError, convert.ContentError, NotFoundError) as exc:
            return _error(str(exc))
        result["workspace"] = workspace
        result["url"] = state.page_url(workspace, result["id"], project_id)
        log.info("created page %s in %s/%s", result["id"], workspace, result["project"])
        return result

    @mcp.tool
    def update_page(
        page_id: str,
        content: str,
        format: str = "markdown",
        title: str | None = None,
        mode: str = "replace",
    ) -> dict:
        """Update a page's content (and optionally its title).

        Targets a globally-unique page id and inherits that page's workspace, so
        no workspace argument is needed.

        Args:
            page_id: the page UUID.
            content: markdown (default) or HTML — see ``format``.
            format: "markdown" or "html".
            title: new title, or omit to keep the current one.
            mode: "replace" (default) overwrites the body; "append" adds to it.
        """
        try:
            result = pipeline.update_page(
                db,
                state.live,
                page_id=page_id,
                content=content,
                fmt=format,
                title=title,
                mode=mode,
                service_user_id=state.cfg.service_user_id,
            )
        except (WriteError, ConvertError, convert.ContentError) as exc:
            return _error(str(exc))
        log.info("updated page %s (mode=%s)", page_id, mode)
        return result


# --- work items (REST) -------------------------------------------------


def _register_work_item_tools(mcp: FastMCP, state: AppState) -> None:
    rest = state.rest

    def _slug(workspace: str | None) -> str:
        return state.workspace_slug(workspace)

    @mcp.tool
    def list_projects(workspace: str | None = None) -> dict:
        """List projects in a workspace (REST).

        Args:
            workspace: workspace slug (accepts a slug like "eng"); defaults to
                the server's configured WORKSPACE_SLUG when omitted.

        Returns id, name, identifier, description.
        """
        try:
            return workitems.list_projects(rest, _slug(workspace))
        except (RestError, WorkspaceUnset) as exc:
            return _error(str(exc))

    @mcp.tool
    def list_work_items(
        project: str,
        workspace: str | None = None,
        state_name: str | None = None,
        assignee: str | None = None,
        limit: int = 50,
    ) -> dict:
        """List work items (issues) in a project (REST).

        Args:
            project: project identifier (e.g. "TEST") or UUID.
            workspace: workspace slug; defaults to the server's configured
                WORKSPACE_SLUG when omitted.
            state_name: optional state name filter (e.g. "In Progress").
            assignee: optional assignee display-name/email filter.
            limit: max items (default 50).

        States and assignees are returned as names, not UUIDs.
        """
        try:
            return workitems.list_work_items(
                rest, _slug(workspace), project,
                state=state_name, assignee=assignee, limit=_clamp(limit, 200),
            )
        except (RestError, WorkItemError, WorkspaceUnset) as exc:
            return _error(str(exc))

    @mcp.tool
    def get_work_item(item: str, project: str, workspace: str | None = None) -> dict:
        """Get one work item's full detail (REST).

        Args:
            item: sequence id (e.g. "TEST-42") or issue UUID.
            project: project identifier or UUID.
            workspace: workspace slug; defaults to the server's configured
                WORKSPACE_SLUG when omitted.

        Description is returned as markdown; state/assignees/labels as names.
        """
        try:
            return workitems.get_work_item(rest, _slug(workspace), project, item)
        except (RestError, WorkItemError, WorkspaceUnset) as exc:
            return _error(str(exc))

    @mcp.tool
    def create_work_item(
        project: str,
        title: str,
        workspace: str,
        description: str | None = None,
        state_name: str | None = None,
        priority: str | None = None,
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
        parent: str | None = None,
    ) -> dict:
        """Create a work item (issue) via REST.

        Args:
            project: project identifier or UUID.
            title: work item title.
            workspace: workspace slug — REQUIRED (no default, to avoid creating
                in the wrong workspace).
            description: optional markdown body (Plane fills the rich/binary reps).
            state_name: optional state name (e.g. "Todo"); resolved to its id.
            priority: one of urgent/high/medium/low/none.
            assignees: optional list of member display names or emails.
            labels: optional list of label names.
            parent: optional parent work item (sequence ref like "TEST-42" or a
                UUID) — set it to create this item as a sub-work-item.

        Unknown state/priority/assignee/label/parent names return an error
        listing the valid options.
        """
        if not workspace:
            return _error("workspace is required for create_work_item (no default is applied)")
        try:
            return workitems.create_work_item(
                rest, workspace, project,
                title=title, description=description, state=state_name,
                priority=priority, assignees=assignees, labels=labels, parent=parent,
            )
        except (RestError, WorkItemError, convert.ContentError) as exc:
            return _error(str(exc))

    @mcp.tool
    def update_work_item(
        item: str,
        project: str,
        workspace: str | None = None,
        title: str | None = None,
        description: str | None = None,
        state_name: str | None = None,
        priority: str | None = None,
        assignees: list[str] | None = None,
        labels: list[str] | None = None,
        parent: str | None = None,
    ) -> dict:
        """Partially update a work item (REST).

        Only the fields you supply are sent; others are left untouched.

        Args:
            item: sequence id ("TEST-42") or issue UUID.
            project: project identifier or UUID.
            workspace: workspace slug; defaults to the server's configured
                WORKSPACE_SLUG when omitted.
            title / description / state_name / priority / assignees / labels:
                any subset to change. Unknown names error with valid options.
            parent: re-parent under this work item (sequence ref or UUID) to make
                it a sub-work-item.
        """
        try:
            return workitems.update_work_item(
                rest, _slug(workspace), project, item,
                title=title, description=description, state=state_name,
                priority=priority, assignees=assignees, labels=labels, parent=parent,
            )
        except (RestError, WorkItemError, WorkspaceUnset, convert.ContentError) as exc:
            return _error(str(exc))

    @mcp.tool
    def list_states(project: str, workspace: str | None = None) -> dict:
        """List a project's states so you can pick valid state names (REST).

        Args:
            project: project identifier or UUID.
            workspace: workspace slug; defaults to the server's configured
                WORKSPACE_SLUG when omitted.
        """
        try:
            return workitems.list_states(rest, _slug(workspace), project)
        except (RestError, WorkItemError, WorkspaceUnset) as exc:
            return _error(str(exc))

    @mcp.tool
    def list_labels(project: str, workspace: str | None = None) -> dict:
        """List a project's labels so you can pick valid label names (REST).

        Args:
            project: project identifier or UUID.
            workspace: workspace slug; defaults to the server's configured
                WORKSPACE_SLUG when omitted.
        """
        try:
            return workitems.list_labels(rest, _slug(workspace), project)
        except (RestError, WorkItemError, WorkspaceUnset) as exc:
            return _error(str(exc))


def _clamp(limit: int, hard_max: int) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return 1
    return max(1, min(limit, hard_max))


def _error(message: str) -> dict:
    return {"error": message}


def _capability_line(cfg: Config) -> str:
    pages = "on" if cfg.pages_enabled else "OFF (no DATABASE_URL)"
    rest = "on" if cfg.rest_enabled else "OFF (no PLANE_BASE_URL/PLANE_API_KEY)"
    auth_str = (
        f"github-oauth ({len(cfg.allowed_github_logins)} allowed logins)"
        if cfg.auth_enabled
        else "none (stdio)"
    )
    return f"capabilities: pages={pages}, work_items={rest}, auth={auth_str}"


def run(cfg: Config) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state = AppState(cfg)

    if cfg.transport == "stdio":
        # Auth-free on-box transport; FastMCP skips auth checks over stdio.
        mcp = build_mcp(state)
        log.info(_capability_line(cfg))
        log.info("starting plane_pages_mcp on stdio")
        try:
            mcp.run(transport="stdio")
        finally:
            state.close()
        return

    # http: GitHub OAuth + identity allowlist. FastMCP installs the auth ASGI
    # middleware (spec-shaped 401 + WWW-Authenticate, discovery routes); our
    # AllowlistMiddleware gates tool list/call by GitHub login.
    auth_provider = auth.build_auth(cfg)
    mcp = build_mcp(state, auth_provider=auth_provider)
    log.info(_capability_line(cfg))
    app = mcp.http_app(path=cfg.mcp_path)
    log.info("starting plane_pages_mcp on http://%s:%s%s (public: %s)",
             cfg.host, cfg.port, cfg.mcp_path, cfg.public_base_url)
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())
    finally:
        state.close()
