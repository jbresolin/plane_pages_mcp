"""Work-item relations (issue_relations), via direct DB writes.

Relations are absent from Plane CE's public REST API, so — like pages — they go
straight to Postgres. Gated on DATABASE_URL.

Plane stores a relation as ONE directed row ``(issue, related_issue,
relation_type)`` using a canonical enum; the UI computes the inverse label for
the other issue. Only these canonical values are storable:
``blocked_by, relates_to, duplicate, start_before, finish_before,
implemented_by`` — the reverse labels (``blocking, start_after, finish_after,
implements``) are display-only. This module lets callers use the full, natural
vocabulary and maps it to the correct canonical value + direction.
"""

from __future__ import annotations

from typing import Any

from .db import Database, DuplicateRelationError, NotFoundError

# friendly type (for "item <type> related_item") -> (canonical stored type, swap)
# swap=True means store the pair reversed: issue=related_item, related=item.
_FRIENDLY: dict[str, tuple[str, bool]] = {
    "blocks": ("blocked_by", True),
    "blocking": ("blocked_by", True),      # alias of "blocks"
    "blocked_by": ("blocked_by", False),
    "relates_to": ("relates_to", False),
    "duplicate": ("duplicate", False),
    "duplicate_of": ("duplicate", False),  # alias
    "start_before": ("start_before", False),
    "start_after": ("start_before", True),
    "finish_before": ("finish_before", False),
    "finish_after": ("finish_before", True),
    "implements": ("implemented_by", True),
    "implemented_by": ("implemented_by", False),
}

# Display labels for a stored canonical type, by which side the subject is on.
# forward = subject is the row's `issue`; reverse = subject is `related_issue`.
_FORWARD_LABEL = {
    "blocked_by": "blocked_by", "relates_to": "relates_to", "duplicate": "duplicate",
    "start_before": "start_before", "finish_before": "finish_before",
    "implemented_by": "implemented_by",
}
_REVERSE_LABEL = {
    "blocked_by": "blocking", "relates_to": "relates_to", "duplicate": "duplicate",
    "start_before": "start_after", "finish_before": "finish_after",
    "implemented_by": "implements",
}

VALID_RELATION_TYPES = sorted(_FRIENDLY)


class RelationError(RuntimeError):
    """A relation operation could not be completed."""


def _canonical(relation_type: str) -> tuple[str, bool]:
    key = relation_type.strip().lower()
    if key not in _FRIENDLY:
        raise RelationError(
            f"unknown relation type {relation_type!r}; valid options: {VALID_RELATION_TYPES}"
        )
    return _FRIENDLY[key]


def _resolve(db: Database, workspace: str, project: str, item: str, related_item: str):
    try:
        ws_id = db.resolve_workspace(workspace)
        proj = db.resolve_project(ws_id, project)
        subject = db.resolve_issue(ws_id, proj["id"], item)
        obj = db.resolve_issue(ws_id, proj["id"], related_item)
    except NotFoundError as exc:
        raise RelationError(str(exc)) from exc
    if subject["id"] == obj["id"]:
        raise RelationError("a work item cannot be related to itself")
    return ws_id, proj, subject, obj


def link(
    db: Database,
    *,
    workspace: str,
    project: str,
    item: str,
    related_item: str,
    relation_type: str,
    service_user_id: str,
) -> dict[str, Any]:
    """Create the relation "item <relation_type> related_item"."""
    canonical, swap = _canonical(relation_type)
    ws_id, proj, subject, obj = _resolve(db, workspace, project, item, related_item)
    a, b = (obj, subject) if swap else (subject, obj)  # (issue, related_issue)
    try:
        with db.transaction() as conn:
            db.insert_issue_relation(
                conn,
                workspace_id=ws_id,
                project_id=proj["id"],
                issue_id=a["id"],
                related_issue_id=b["id"],
                relation_type=canonical,
                service_user_id=service_user_id,
            )
    except DuplicateRelationError as exc:
        raise RelationError(str(exc)) from exc
    return {
        "item": f"{proj['identifier']}-{subject['sequence_id']}",
        "relation": relation_type.strip().lower(),
        "related_item": f"{proj['identifier']}-{obj['sequence_id']}",
        "stored_as": {"relation_type": canonical, "issue": a["id"], "related_issue": b["id"]},
    }


def unlink(
    db: Database,
    *,
    workspace: str,
    project: str,
    item: str,
    related_item: str,
    relation_type: str,
    service_user_id: str,
) -> dict[str, Any]:
    """Remove the relation "item <relation_type> related_item" (hard delete)."""
    canonical, swap = _canonical(relation_type)
    _ws_id, proj, subject, obj = _resolve(db, workspace, project, item, related_item)
    a, b = (obj, subject) if swap else (subject, obj)
    with db.transaction() as conn:
        rowcount = db.delete_issue_relation(
            conn, issue_id=a["id"], related_issue_id=b["id"], relation_type=canonical
        )
    if rowcount == 0:
        raise RelationError(
            f"no {relation_type!r} relation exists from "
            f"{proj['identifier']}-{subject['sequence_id']} to "
            f"{proj['identifier']}-{obj['sequence_id']}"
        )
    return {"removed": rowcount,
            "item": f"{proj['identifier']}-{subject['sequence_id']}",
            "relation": relation_type.strip().lower(),
            "related_item": f"{proj['identifier']}-{obj['sequence_id']}"}


def for_issue(db: Database, issue_id: str) -> list[dict[str, Any]]:
    """Relations touching an issue, as friendly {relation, related_item} entries.

    The label reflects direction: if the issue is the row's `issue` we use the
    forward label; if it is the `related_issue`, the reverse (display) label.
    """
    out = []
    for row in db.read_issue_relations(issue_id):
        rtype = row["relation_type"]
        if str(row["issue_id"]) == str(issue_id):
            other, label = row["related_issue_id"], _FORWARD_LABEL.get(rtype, rtype)
        else:
            other, label = row["issue_id"], _REVERSE_LABEL.get(rtype, rtype)
        out.append({"relation": label, "related_item": db.issue_ref(str(other))})
    out.sort(key=lambda r: (r["relation"], r["related_item"] or ""))
    return out
