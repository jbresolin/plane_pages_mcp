"""Issue comment CRUD, via the public REST API (no DB).

Comment content is ``comment_html``; the REST API fills the other
representations itself, so — like work-item descriptions — we send HTML from
markdown and read it back as markdown. Comments are identified by UUID.
"""

from __future__ import annotations

from typing import Any

from . import convert
from .rest import PlaneREST, member_display_map


def _resolve(rest: PlaneREST, slug: str, project: str, item: str) -> tuple[dict, str, str]:
    proj = rest.resolve_project(slug, project)
    pid = str(proj["id"])
    issue_id = rest.resolve_issue_id(slug, pid, item)
    return proj, pid, issue_id


def list_comments(rest: PlaneREST, slug: str, project: str, item: str) -> dict[str, Any]:
    proj, pid, issue_id = _resolve(rest, slug, project, item)
    actors = member_display_map(rest.list_members(slug, pid))
    comments = [
        {
            "id": str(c["id"]),
            "actor": actors.get(str(c.get("actor")), str(c.get("actor"))),
            "created_at": c.get("created_at"),
            "updated_at": c.get("updated_at"),
            "edited": c.get("edited_at") is not None,
            "body": convert.html_to_markdown(c.get("comment_html") or ""),
        }
        for c in rest.list_comments(slug, pid, issue_id)
    ]
    return {
        "workspace": slug,
        "project": proj["identifier"],
        "work_item": str(item),
        "comments": comments,
        "count": len(comments),
    }


def add_comment(
    rest: PlaneREST, slug: str, project: str, item: str, *, body: str, fmt: str = "markdown"
) -> dict[str, Any]:
    _proj, pid, issue_id = _resolve(rest, slug, project, item)
    html = convert.to_html(body, fmt)
    created = rest.create_comment(slug, pid, issue_id, {"comment_html": html})
    return {"id": str(created["id"]), "work_item": item}


def update_comment(
    rest: PlaneREST,
    slug: str,
    project: str,
    item: str,
    comment_id: str,
    *,
    body: str,
    fmt: str = "markdown",
) -> dict[str, Any]:
    _proj, pid, issue_id = _resolve(rest, slug, project, item)
    html = convert.to_html(body, fmt)
    rest.update_comment(slug, pid, issue_id, comment_id, {"comment_html": html})
    return {"id": comment_id, "updated": True}


def delete_comment(
    rest: PlaneREST, slug: str, project: str, item: str, comment_id: str
) -> dict[str, Any]:
    _proj, pid, issue_id = _resolve(rest, slug, project, item)
    rest.delete_comment(slug, pid, issue_id, comment_id)
    return {"id": comment_id, "deleted": True}
