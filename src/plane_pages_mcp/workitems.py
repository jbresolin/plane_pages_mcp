"""Work-item operations composed from the REST client.

This is the REST analogue of pipeline.py: it resolves human-readable
names (project identifier, state name, priority, assignee/label names) to the
UUIDs the API wants, shapes responses so UUIDs come back as names, and never
reuses the pages DB-write path. Descriptions are sent as ``description_html``
and read back via the shared HTML->markdown helper.
"""

from __future__ import annotations

from typing import Any

from . import convert
from .rest import (
    PRIORITIES,
    PlaneREST,
    RestNotFound,
    member_display_map,
    resolve_member,
    resolve_named,
)


class WorkItemError(RuntimeError):
    """A work-item operation could not be completed."""


def list_projects(rest: PlaneREST, slug: str) -> dict[str, Any]:
    projects = [
        {
            "id": str(p["id"]),
            "name": p.get("name"),
            "identifier": p.get("identifier"),
            "description": p.get("description") or "",
        }
        for p in rest.list_projects(slug)
    ]
    return {"workspace": slug, "projects": projects, "count": len(projects)}


def list_work_items(
    rest: PlaneREST,
    slug: str,
    project: str,
    *,
    state: str | None = None,
    assignee: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    proj = rest.resolve_project(slug, project)
    pid = str(proj["id"])
    states = {str(s["id"]): s.get("name") for s in rest.list_states(slug, pid)}
    members = member_display_map(rest.list_members(slug, pid))

    state_filter = _lower_or_none(state)
    assignee_filter = _lower_or_none(assignee)

    items = []
    for issue in rest.list_issues(slug, pid):
        state_name = states.get(str(issue.get("state"))) if issue.get("state") else None
        assignee_names = [members.get(str(a), str(a)) for a in (issue.get("assignees") or [])]

        if state_filter and (state_name or "").lower() != state_filter:
            continue
        if assignee_filter and assignee_filter not in [a.lower() for a in assignee_names]:
            continue

        items.append(
            {
                "id": str(issue["id"]),
                "sequence_id": f"{proj['identifier']}-{issue.get('sequence_id')}",
                "name": issue.get("name"),
                "state": state_name,
                "assignees": assignee_names,
                "priority": issue.get("priority"),
                "updated_at": issue.get("updated_at"),
            }
        )
        if len(items) >= limit:
            break

    return {"workspace": slug, "project": proj["identifier"], "work_items": items, "count": len(items)}


def get_work_item(rest: PlaneREST, slug: str, project: str, item: str) -> dict[str, Any]:
    proj = rest.resolve_project(slug, project)
    pid = str(proj["id"])
    issue_id = rest.resolve_issue_id(slug, pid, item)
    issue = rest.get_issue(slug, pid, issue_id)

    states = {str(s["id"]): s.get("name") for s in rest.list_states(slug, pid)}
    labels = {str(lbl["id"]): lbl.get("name") for lbl in rest.list_labels(slug, pid)}
    members = member_display_map(rest.list_members(slug, pid))

    # A sub-work-item points at a parent; show it as a sequence ref (TEST-42).
    parent_ref = None
    if issue.get("parent"):
        parent_issue = rest.get_issue(slug, pid, str(issue["parent"]))
        parent_ref = f"{proj['identifier']}-{parent_issue.get('sequence_id')}"

    return {
        "id": str(issue["id"]),
        "sequence_id": f"{proj['identifier']}-{issue.get('sequence_id')}",
        "name": issue.get("name"),
        "workspace": slug,
        "project": proj["identifier"],
        "state": states.get(str(issue.get("state"))) if issue.get("state") else None,
        "priority": issue.get("priority"),
        "assignees": [members.get(str(a), str(a)) for a in (issue.get("assignees") or [])],
        "labels": [labels.get(str(x), str(x)) for x in (issue.get("labels") or [])],
        "parent": parent_ref,
        "sub_issues_count": issue.get("sub_issues_count"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "description": convert.html_to_markdown(issue.get("description_html") or ""),
    }


def _build_payload(
    rest: PlaneREST,
    slug: str,
    pid: str,
    *,
    title: str | None,
    description: str | None,
    state: str | None,
    priority: str | None,
    assignees: list[str] | None,
    labels: list[str] | None,
    parent: str | None,
) -> dict[str, Any]:
    """Resolve human names to UUIDs; only include fields that were supplied."""
    payload: dict[str, Any] = {}
    if title is not None:
        payload["name"] = title
    if parent is not None:
        # A sub-work-item: parent may be a sequence ref (TEST-42) or a UUID.
        payload["parent"] = rest.resolve_issue_id(slug, pid, parent)
    if description is not None:
        # Same multi-representation problem as pages, but the REST API fills the
        # other representations itself — we only send HTML.
        payload["description_html"] = convert.to_html(description, "markdown")
    if state is not None:
        payload["state"] = resolve_named(
            rest.list_states(slug, pid), state, what="state"
        )
    if priority is not None:
        p = priority.strip().lower()
        if p not in PRIORITIES:
            raise WorkItemError(
                f"unknown priority {priority!r}; valid options: {list(PRIORITIES)}"
            )
        payload["priority"] = p
    if assignees is not None:
        members = rest.list_members(slug, pid)
        payload["assignees"] = [resolve_member(members, a) for a in assignees]
    if labels is not None:
        available = rest.list_labels(slug, pid)
        payload["labels"] = [resolve_named(available, lbl, what="label") for lbl in labels]
    return payload


def create_work_item(
    rest: PlaneREST,
    slug: str,
    project: str,
    *,
    title: str,
    description: str | None = None,
    state: str | None = None,
    priority: str | None = None,
    assignees: list[str] | None = None,
    labels: list[str] | None = None,
    parent: str | None = None,
) -> dict[str, Any]:
    proj = rest.resolve_project(slug, project)
    pid = str(proj["id"])
    payload = _build_payload(
        rest, slug, pid,
        title=title, description=description, state=state,
        priority=priority, assignees=assignees, labels=labels, parent=parent,
    )
    issue = rest.create_issue(slug, pid, payload)
    return {
        "id": str(issue["id"]),
        "sequence_id": f"{proj['identifier']}-{issue.get('sequence_id')}",
        "name": issue.get("name"),
        "workspace": slug,
        "project": proj["identifier"],
    }


def update_work_item(
    rest: PlaneREST,
    slug: str,
    project: str,
    item: str,
    *,
    title: str | None = None,
    description: str | None = None,
    state: str | None = None,
    priority: str | None = None,
    assignees: list[str] | None = None,
    labels: list[str] | None = None,
    parent: str | None = None,
) -> dict[str, Any]:
    proj = rest.resolve_project(slug, project)
    pid = str(proj["id"])
    issue_id = rest.resolve_issue_id(slug, pid, item)
    payload = _build_payload(
        rest, slug, pid,
        title=title, description=description, state=state,
        priority=priority, assignees=assignees, labels=labels, parent=parent,
    )
    if not payload:
        raise WorkItemError("no fields supplied to update")
    issue = rest.update_issue(slug, pid, issue_id, payload)
    return {
        "id": str(issue["id"]),
        "sequence_id": f"{proj['identifier']}-{issue.get('sequence_id')}",
        "updated_fields": sorted(payload.keys()),
    }


def list_states(rest: PlaneREST, slug: str, project: str) -> dict[str, Any]:
    proj = rest.resolve_project(slug, project)
    states = [
        {"id": str(s["id"]), "name": s.get("name"), "group": s.get("group")}
        for s in rest.list_states(slug, str(proj["id"]))
    ]
    return {"workspace": slug, "project": proj["identifier"], "states": states}


def list_labels(rest: PlaneREST, slug: str, project: str) -> dict[str, Any]:
    proj = rest.resolve_project(slug, project)
    labels = [
        {"id": str(lbl["id"]), "name": lbl.get("name")}
        for lbl in rest.list_labels(slug, str(proj["id"]))
    ]
    return {"workspace": slug, "project": proj["identifier"], "labels": labels}


def _lower_or_none(v: str | None) -> str | None:
    return v.strip().lower() if v else None
