"""Cycles and modules ("containers") — full CRUD + issue assignment, via REST.

Cycles and modules are structurally identical (name, dates, issue counts,
issue membership), so the orchestration is parameterized by ``kind`` in
("cycles", "modules"). Unlike relations, these are fully served by the public
REST API — no DB access.

Quirks handled here (verified against CE v1.3.1):
  * cycle create requires ``project_id`` in the BODY (modules infer it from the
    URL); we send it for both — harmless for modules.
  * issue membership is a separate sub-resource, not a field on the issue.
"""

from __future__ import annotations

from typing import Any

from .rest import PlaneREST, RestNotFound

KINDS = ("cycles", "modules")


class ContainerError(RuntimeError):
    """A cycle/module operation could not be completed."""


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ContainerError(f"kind must be one of {KINDS}, got {kind!r}")


def _summary(kind: str, it: dict) -> dict[str, Any]:
    out = {
        "id": str(it["id"]),
        "name": it.get("name"),
        "start_date": it.get("start_date"),
        "total_issues": it.get("total_issues"),
        "completed_issues": it.get("completed_issues"),
    }
    # cycles carry end_date; modules carry target_date + status/lead.
    out["end_date"] = it.get("end_date") if kind == "cycles" else it.get("target_date")
    if kind == "modules":
        out["status"] = it.get("status")
    return out


def list_containers(rest: PlaneREST, slug: str, project: str, kind: str) -> dict[str, Any]:
    _check_kind(kind)
    proj = rest.resolve_project(slug, project)
    items = [_summary(kind, it) for it in rest.list_containers(slug, str(proj["id"]), kind)]
    return {"workspace": slug, "project": proj["identifier"], kind: items, "count": len(items)}


def create_container(
    rest: PlaneREST,
    slug: str,
    project: str,
    kind: str,
    *,
    name: str,
    start_date: str | None = None,
    end_date: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    _check_kind(kind)
    proj = rest.resolve_project(slug, project)
    pid = str(proj["id"])
    payload: dict[str, Any] = {"name": name, "project_id": pid}
    if start_date is not None:
        payload["start_date"] = start_date
    if end_date is not None:
        # cycles use end_date; modules use target_date.
        payload["end_date" if kind == "cycles" else "target_date"] = end_date
    if description is not None:
        payload["description"] = description
    created = rest.create_container(slug, pid, kind, payload)
    return {
        "id": str(created["id"]),
        "name": created.get("name"),
        "kind": kind[:-1],
        "workspace": slug,
        "project": proj["identifier"],
    }


def delete_container(rest: PlaneREST, slug: str, project: str, kind: str, ref: str) -> dict[str, Any]:
    _check_kind(kind)
    proj = rest.resolve_project(slug, project)
    container = rest.resolve_container(slug, str(proj["id"]), kind, ref)
    rest.delete_container(slug, str(proj["id"]), kind, str(container["id"]))
    return {"deleted": kind[:-1], "id": str(container["id"]), "name": container.get("name")}


def assign(
    rest: PlaneREST, slug: str, project: str, kind: str, container_ref: str, items: list[str]
) -> dict[str, Any]:
    _check_kind(kind)
    if not items:
        raise ContainerError("no work items given to assign")
    proj = rest.resolve_project(slug, project)
    pid = str(proj["id"])
    container = rest.resolve_container(slug, pid, kind, container_ref)
    issue_ids = [rest.resolve_issue_id(slug, pid, it) for it in items]
    rest.add_issues_to_container(slug, pid, kind, str(container["id"]), issue_ids)
    return {
        "assigned_to": kind[:-1],
        "name": container.get("name"),
        "work_items": items,
        "count": len(issue_ids),
    }


def unassign(
    rest: PlaneREST, slug: str, project: str, kind: str, container_ref: str, items: list[str]
) -> dict[str, Any]:
    _check_kind(kind)
    if not items:
        raise ContainerError("no work items given to unassign")
    proj = rest.resolve_project(slug, project)
    pid = str(proj["id"])
    container = rest.resolve_container(slug, pid, kind, container_ref)
    removed = 0
    for it in items:
        issue_id = rest.resolve_issue_id(slug, pid, it)
        try:
            rest.remove_issue_from_container(slug, pid, kind, str(container["id"]), issue_id)
            removed += 1
        except RestNotFound:
            pass  # not a member — treat as already-removed
    return {"unassigned_from": kind[:-1], "name": container.get("name"), "removed": removed}
