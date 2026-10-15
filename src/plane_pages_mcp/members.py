"""Workspace members: find one by email or display name, and add one to a project.

Plane roles (v1.3.1, ``plane.app.permissions.ROLE``): Admin 20, Member 15,
Guest 5. The same numbers are used for workspace and project membership.

Rules (agreed with the operator, see docs/decisions.md 2026-10-15):
  * a person is matched exactly: email ignoring letter case (addresses
    aren't case-sensitive), or display name as written. No partial or
    "closest" matches, because a wrong guess hands someone project rights;
  * only workspace Admins and Members may join a project through this MCP.
    Guests are refused, because Plane itself would let a Guest become a
    project Admin;
  * a project member added here is an Admin or a Member, never a Guest.
"""

from __future__ import annotations

from typing import Any

from .rest import PlaneREST, RestError

ROLE_ADMIN = 20
ROLE_MEMBER = 15
ROLE_GUEST = 5
PROJECT_ROLES = {"admin": ROLE_ADMIN, "member": ROLE_MEMBER}

_LIST_LIMIT = 20  # how many members an error message lists at most


class MemberError(RuntimeError):
    """A member couldn't be found, isn't allowed, or couldn't be added."""


def describe(member: dict) -> str:
    return f"{member.get('display_name')} <{member.get('email')}>"


def find_workspace_member(workspace_members: list[dict], ref: str) -> dict:
    """The one member whose email or display name is exactly ``ref``."""
    wanted = (ref or "").strip()
    if not wanted:
        raise MemberError("a member (email or display name) is required")
    matches = [
        m for m in workspace_members
        if str(m.get("email", "")).lower() == wanted.lower()
        or str(m.get("display_name", "")) == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = ", ".join(describe(m) for m in workspace_members[:_LIST_LIMIT])
        raise MemberError(
            f"no workspace member matches {wanted!r} exactly (by email or display "
            f"name); members: {known}"
        )
    raise MemberError(
        f"{wanted!r} matches {len(matches)} workspace members "
        f"({', '.join(describe(m) for m in matches)}); use the email address instead"
    )


def require_admin_or_member(member: dict) -> None:
    """Refuse workspace Guests (and anything that isn't Admin or Member)."""
    if member.get("role") == ROLE_GUEST:
        raise MemberError(
            f"{describe(member)} is a workspace Guest; only workspace Admins and "
            "Members can be added to a project (or be its lead)"
        )
    if member.get("role") not in (ROLE_ADMIN, ROLE_MEMBER):
        raise MemberError(f"{describe(member)} has an unknown workspace role {member.get('role')!r}")


def project_role_code(role: str) -> int:
    code = PROJECT_ROLES.get((role or "").strip().lower())
    if code is None:
        raise MemberError(f"role must be 'admin' or 'member', got {role!r}")
    return code


def add_project_member(
    rest: PlaneREST, slug: str, project: str, member: str, role: str
) -> dict[str, Any]:
    """Add a workspace Admin/Member to a project as Admin or Member."""
    role_code = project_role_code(role)
    person = find_workspace_member(rest.list_workspace_members(slug), member)
    require_admin_or_member(person)
    proj = rest.resolve_project(slug, project)
    try:
        rest.add_project_member(slug, str(proj["id"]), str(person["id"]), role_code)
    except RestError as exc:
        raise MemberError(
            f"could not add {person.get('email')} to project {proj['identifier']}: {exc}"
        ) from exc
    return {
        "project": proj["identifier"],
        "member": person.get("email"),
        "role": role.strip().lower(),
        "workspace": slug,
    }
