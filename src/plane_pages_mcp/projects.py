"""Create projects via the public REST API.

Verified against Plane CE v1.3.1 (apps/api):
  * ``POST /api/v1/workspaces/<slug>/projects/`` accepts API-key auth. The
    key's user must be a workspace Admin or Member, and becomes the new
    project's admin. Plane also creates the default states.
  * The model stores ``identifier.strip().upper()`` in a 12-character column.
    The web UI's create form is stricter: 1-10 characters matching
    ``^[ÇŞĞIİÖÜA-Z0-9]+$`` after uppercasing. We enforce the UI rule, so every
    project created here is one the UI could have created, and can still edit
    (see docs/decisions.md, 2026-10-15).
  * Names are rejected if they contain any of Plane's
    FORBIDDEN_IDENTIFIER_CHARS (the same set applies to names), and the column
    is 255 characters.
  * A duplicate name AND a duplicate identifier both come back as HTTP 409
    ``{"name": "The project name is already taken"}``. We work out which one
    it was by listing the workspace's projects, and only after a 409, so a
    normal create stays a single request.
"""

from __future__ import annotations

import re
from typing import Any

from .rest import PlaneREST, RestConflict

IDENTIFIER_MAX_LENGTH = 10  # the web UI's limit (the column allows 12)
_IDENTIFIER_PATTERN = re.compile(r"^[ÇŞĞIİÖÜA-Z0-9]+$")  # copied from the UI form

NAME_MAX_LENGTH = 255
# Plane's Project.FORBIDDEN_IDENTIFIER_CHARS_PATTERN, as a set of characters.
_FORBIDDEN_NAME_CHARS = "&+,:;$^}{*=?@#|'<>.()%!-"


class ProjectError(RuntimeError):
    """A project could not be created; the message says why."""


def normalize_identifier(identifier: str) -> str:
    """Trim and uppercase (as Plane stores it), then apply the UI's rule."""
    value = (identifier or "").strip().upper()
    if not value:
        raise ProjectError("project identifier is required")
    if len(value) > IDENTIFIER_MAX_LENGTH:
        raise ProjectError(
            f"project identifier {value!r} is {len(value)} characters; "
            f"Plane allows at most {IDENTIFIER_MAX_LENGTH}"
        )
    if not _IDENTIFIER_PATTERN.match(value):
        raise ProjectError(
            f"project identifier {value!r} may contain only letters A-Z (plus "
            "ÇŞĞIİÖÜ) and digits 0-9: no spaces, dashes or underscores"
        )
    return value


def normalize_name(name: str) -> str:
    value = (name or "").strip()
    if not value:
        raise ProjectError("project name is required")
    if len(value) > NAME_MAX_LENGTH:
        raise ProjectError(f"project name must be at most {NAME_MAX_LENGTH} characters")
    bad = sorted({ch for ch in value if ch in _FORBIDDEN_NAME_CHARS})
    if bad:
        raise ProjectError(
            f"project name {value!r} cannot contain {' '.join(bad)} "
            f"(Plane forbids these in names: {' '.join(_FORBIDDEN_NAME_CHARS)})"
        )
    return value


def create_project(
    rest: PlaneREST,
    slug: str,
    *,
    name: str,
    identifier: str,
    description: str | None = None,
) -> dict[str, Any]:
    # Validate both before sending anything, so a bad value never reaches Plane.
    identifier = normalize_identifier(identifier)
    name = normalize_name(name)

    payload: dict[str, Any] = {"name": name, "identifier": identifier}
    if description is not None:
        payload["description"] = description

    try:
        created = rest.create_project(slug, payload)
    except RestConflict as exc:
        raise ProjectError(_explain_conflict(rest, slug, name, identifier)) from exc

    return {
        "id": str(created["id"]),
        "identifier": created.get("identifier", identifier),
        "name": created.get("name", name),
        "workspace": slug,
    }


def _explain_conflict(rest: PlaneREST, slug: str, name: str, identifier: str) -> str:
    """Turn Plane's ambiguous 409 into "the identifier is taken" or "the name is taken"."""
    existing = rest.list_projects(slug)
    for project in existing:
        if str(project.get("identifier", "")).upper() == identifier:
            return (
                f"project identifier {identifier!r} is already used by project "
                f"{project.get('name')!r} in workspace {slug!r}; choose another identifier"
            )
    # Names, unlike identifiers, are unique case-sensitively in Plane
    # ("Lab" and "lab" can coexist; verified on v1.3.1), so compare exactly.
    for project in existing:
        if str(project.get("name", "")).strip() == name:
            return (
                f"project name {name!r} is already used in workspace {slug!r} "
                f"(by {project.get('identifier')}); choose another name"
            )
    # The clash is with a project this API key can't list (archived, or one
    # its user isn't a member of). Plane doesn't say which field it was.
    return (
        f"Plane says the project name or identifier is already in use in workspace "
        f"{slug!r} (name {name!r}, identifier {identifier!r}), possibly by an archived "
        "project or one this API key's user can't see; try another of each"
    )
