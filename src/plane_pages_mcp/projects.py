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
  * ``project_lead`` on create is broken (it leaves a half-created project),
    so we never send it. The lead is added through the project members
    endpoint instead (members.py).
  * ``network`` (visibility) is ignored by the API, so private projects are
    made private with a direct DB write.
See docs/decisions.md (2026-10-15) for all of these.
"""

from __future__ import annotations

import re
from typing import Any

from . import members
from .rest import PlaneREST, RestConflict, RestError

IDENTIFIER_MAX_LENGTH = 10  # the web UI's limit (the column allows 12)
_IDENTIFIER_PATTERN = re.compile(r"^[ÇŞĞIİÖÜA-Z0-9]+$")  # copied from the UI form

NAME_MAX_LENGTH = 255
# Plane's Project.FORBIDDEN_IDENTIFIER_CHARS_PATTERN, as a set of characters.
_FORBIDDEN_NAME_CHARS = "&+,:;$^}{*=?@#|'<>.()%!-"


# projects.network: 0 is "Secret" (private), 2 is public. The model's default
# is 2, and the v1.3.1 REST API ignores the field (see _make_private).
NETWORK = {"private": 0, "public": 2}


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
    lead: str | None = None,
    default_lead: str | None = None,
    visibility: str = "private",
    db: Any = None,
) -> dict[str, Any]:
    """Create a project, make it private if asked, and add its lead as Admin.

    Order: validate everything (including the lead) -> create via REST ->
    make private in the DB -> add the lead. Anything that can be refused is
    refused before the project exists. A failure after that keeps the project
    and is reported in ``notes``, because the project is real by then.

    ``db`` is the pages Database (or None when DATABASE_URL isn't set); it is
    only used for the private-visibility write.
    """
    identifier = normalize_identifier(identifier)
    name = normalize_name(name)
    if visibility not in NETWORK:
        raise ProjectError(f"visibility must be 'private' or 'public', got {visibility!r}")
    lead_member = _resolve_lead(rest, slug, lead or default_lead)

    # Sent explicitly even though v1.3.1 ignores it, so that if a later Plane
    # starts honouring it, Plane's own default still never decides.
    payload: dict[str, Any] = {
        "name": name, "identifier": identifier, "network": NETWORK[visibility],
    }
    if description is not None:
        payload["description"] = description

    try:
        created = rest.create_project(slug, payload)
    except RestConflict as exc:
        raise ProjectError(_explain_conflict(rest, slug, name, identifier)) from exc

    project_id = str(created["id"])
    identifier = created.get("identifier", identifier)
    notes: list[str] = []

    actual = _visibility_of(created)
    if visibility == "private" and actual != "private":
        actual = _make_private(db, project_id, notes)

    lead_email = _add_lead(rest, slug, project_id, identifier, lead_member, notes)
    if lead_email is None:
        notes.append(_who_can_see_it(rest, slug, identifier, actual))

    return {
        "id": project_id,
        "identifier": identifier,
        "name": created.get("name", name),
        "workspace": slug,
        "visibility": actual,
        "lead": lead_email,
        "notes": notes,
    }


def _resolve_lead(rest: PlaneREST, slug: str, ref: str | None) -> dict | None:
    """Find the lead and apply the Guest rule, before anything is created."""
    if not ref:
        return None
    try:
        person = members.find_workspace_member(rest.list_workspace_members(slug), ref)
        members.require_admin_or_member(person)
    except members.MemberError as exc:
        raise ProjectError(f"cannot use {ref!r} as project lead: {exc}") from exc
    return person


def _visibility_of(project: dict) -> str:
    return "private" if project.get("network") == NETWORK["private"] else "public"


def _make_private(db: Any, project_id: str, notes: list[str]) -> str:
    """Plane v1.3.1's API ignores ``network``, so write it directly. Returns the result."""
    if db is None:
        notes.append(
            "The project is PUBLIC: Plane's API can't create private projects, and "
            "this server has no database access (DATABASE_URL) to make it private."
        )
        return "public"
    try:
        if db.set_project_network(project_id, NETWORK["private"]) != 1:
            raise RuntimeError("the project row was not found")
    except Exception as exc:  # any DB problem: the project exists, so report it
        notes.append(f"The project is PUBLIC: making it private failed ({exc}).")
        return "public"
    return "private"


def _add_lead(rest: PlaneREST, slug: str, project_id: str, identifier: str,
              lead: dict | None, notes: list[str]) -> str | None:
    """Add the lead as project Admin. Returns their email, or None if not added."""
    if lead is None:
        return None
    email = lead.get("email")
    # The API key's user already became project Admin when it created the
    # project; adding them again makes Plane fail.
    if str(lead.get("id")) == str(rest.get_me().get("id")):
        return email
    try:
        rest.add_project_member(slug, project_id, str(lead["id"]), members.ROLE_ADMIN)
    except RestError as exc:
        notes.append(
            f"The lead {email} was NOT added to the project ({exc}). Fix it with "
            f"add_project_member(workspace={slug!r}, project={identifier!r}, "
            f"member={email!r}, role='admin')."
        )
        return None
    return email


def _who_can_see_it(rest: PlaneREST, slug: str, identifier: str, visibility: str) -> str:
    service = rest.get_me().get("email")
    who = (
        "nobody else can see it" if visibility == "private"
        else "other workspace members can find it under Projects and join it"
    )
    return (
        f"Only the service user ({service}) is a member of this project, so {who}. "
        f"Add people with add_project_member(workspace={slug!r}, project={identifier!r}, "
        "member=..., role='admin' or 'member')."
    )


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
