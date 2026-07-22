"""FastMCP server: 5 page tools + /healthz, with Bearer auth in http mode.

Transport:
  * http  — streamable HTTP on 0.0.0.0:8300/mcp, every request must carry a
            matching ``Authorization: Bearer <MCP_AUTH_TOKEN>`` header.
  * stdio — for local dev (Claude Code, etc.); no network auth.
"""

from __future__ import annotations

import logging

import uvicorn
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from . import convert, pipeline
from .config import Config
from .db import Database, NotFoundError
from .live import ConvertError, LiveConverter
from .pipeline import WriteError

log = logging.getLogger("plane_pages_mcp")


class AppState:
    """Shared handles created once at startup."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.db = Database(cfg.database_url, cfg.workspace_slug)
        self.live = LiveConverter(cfg.live_convert_url)

    def page_url(self, page_id: str, project_id: str | None) -> str:
        base = f"{self.cfg.web_url}/{self.cfg.workspace_slug}"
        if project_id:
            return f"{base}/projects/{project_id}/pages/{page_id}"
        return f"{base}/pages/{page_id}"


def build_mcp(state: AppState) -> FastMCP:
    mcp = FastMCP("plane_pages_mcp")

    @mcp.tool
    def list_pages(
        project: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
    ) -> dict:
        """List Plane pages, newest first.

        Args:
            project: project identifier (e.g. "ENG") or UUID; omit to list the
                whole workspace.
            include_archived: include archived pages when True.
            limit: max pages to return (default 50).
        """
        project_id = None
        if project:
            try:
                project_id = state.db.resolve_project(project)["id"]
            except NotFoundError as exc:
                return _error(str(exc))
        rows = state.db.list_pages(project_id, include_archived, _clamp(limit, 200))
        return {"pages": rows, "count": len(rows)}

    @mcp.tool
    def search_pages(query: str, limit: int = 20) -> dict:
        """Case-insensitive search over page name and body text.

        Returns each match with a ±120-char snippet around the first hit.
        """
        if not query.strip():
            return _error("query must not be empty")
        rows = state.db.search_pages(query, _clamp(limit, 100))
        return {"pages": rows, "count": len(rows)}

    @mcp.tool
    def read_page(page_id: str, format: str = "markdown") -> dict:
        """Read one page's metadata and content.

        Args:
            page_id: the page UUID.
            format: "markdown" (default) or "html".
        """
        if format not in ("markdown", "html"):
            return _error("format must be 'markdown' or 'html'")
        row = state.db.read_page(page_id)
        if row is None:
            return _error(f"page {page_id} not found in workspace")
        html = row["description_html"] or ""
        content = html if format == "html" else convert.html_to_markdown(html)
        return {
            "id": str(row["id"]),
            "name": row["name"],
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
        format: str = "markdown",
        access: str = "public",
    ) -> dict:
        """Create a new page in a project.

        Args:
            title: page title.
            content: markdown (default) or HTML body — see ``format``.
            project: project identifier or UUID the page belongs to.
            format: "markdown" or "html".
            access: "public" or "private".
        """
        try:
            result = pipeline.create_page(
                state.db,
                state.live,
                title=title,
                content=content,
                project=project,
                fmt=format,
                access=access,
                service_user_id=state.cfg.service_user_id,
            )
        except (WriteError, ConvertError, convert.ContentError) as exc:
            return _error(str(exc))
        project_id = state.db.resolve_project(project)["id"]
        result["url"] = state.page_url(result["id"], project_id)
        log.info("created page %s in project %s", result["id"], result["project"])
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

        Args:
            page_id: the page UUID.
            content: markdown (default) or HTML — see ``format``.
            format: "markdown" or "html".
            title: new title, or omit to keep the current one.
            mode: "replace" (default) overwrites the body; "append" adds to it.
        """
        try:
            result = pipeline.update_page(
                state.db,
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

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp


def _clamp(limit: int, hard_max: int) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return 1
    return max(1, min(limit, hard_max))


def _error(message: str) -> dict:
    return {"error": message}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject any request whose Bearer token doesn't match. /healthz is open."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        if header != self._expected:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def run(cfg: Config) -> None:
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state = AppState(cfg)
    mcp = build_mcp(state)

    if cfg.transport == "stdio":
        log.info("starting plane_pages_mcp on stdio")
        try:
            mcp.run(transport="stdio")
        finally:
            state.db.close()
        return

    middleware = [Middleware(BearerAuthMiddleware, token=cfg.mcp_auth_token)]
    app = mcp.http_app(path=cfg.mcp_path, middleware=middleware)
    log.info("starting plane_pages_mcp on http://%s:%s%s", cfg.host, cfg.port, cfg.mcp_path)
    try:
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=cfg.log_level.lower())
    finally:
        state.db.close()
