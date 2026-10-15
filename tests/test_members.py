"""Tests for members.py: finding a workspace member, and who may join a project.

Background: Plane gives each workspace member a role: Admin (20), Member (15)
or Guest (5). People are referred to by email or by display name. These
tests use a fixed list shaped like Plane's GET /workspaces/<slug>/members/
response, and a fake REST client, so no Plane is needed.
"""

import pytest

from plane_pages_mcp import members
from plane_pages_mcp.members import MemberError
from plane_pages_mcp.rest import PlaneREST, RestError

WORKSPACE_MEMBERS = [
    {"id": "u-ana", "email": "ana@example.com", "display_name": "ana", "role": 20},
    {"id": "u-bo", "email": "bo@example.com", "display_name": "bo", "role": 15},
    {"id": "u-gus", "email": "gus@example.com", "display_name": "gus", "role": 5},
    # Two people who chose the same display name.
    {"id": "u-sam1", "email": "sam.one@example.com", "display_name": "sam", "role": 15},
    {"id": "u-sam2", "email": "sam.two@example.com", "display_name": "sam", "role": 15},
]


# --- finding a member -------------------------------------------------------


@pytest.mark.parametrize("ref", ["bo@example.com", "BO@Example.com", "bo", " bo "])
def test_member_is_found_by_exact_email_or_display_name(ref):
    # Checks: a member is found by email (in any letter case, because email
    # addresses don't depend on case) or by exact display name, ignoring
    # spaces around the value.
    # Why: the caller is usually an agent passing on what a person said, e.g.
    # "make Bo the lead". Both forms must work.
    assert members.find_workspace_member(WORKSPACE_MEMBERS, ref)["id"] == "u-bo"


@pytest.mark.parametrize("ref", ["b", "bo@example", "Bo", "nobody@example.com"])
def test_no_match_is_a_clear_error_listing_who_exists(ref):
    # Checks: a partial or wrong value ("b", a truncated email, the display
    # name in a different case) matches nobody, and the error lists the
    # members that do exist.
    # Why: guessing "the closest" person could give the wrong human admin
    # rights on a project. Only exact matches count, and the list helps the
    # caller pick the right one.
    with pytest.raises(MemberError, match="no workspace member matches") as err:
        members.find_workspace_member(WORKSPACE_MEMBERS, ref)
    assert "ana@example.com" in str(err.value)


def test_ambiguous_display_name_is_an_error_naming_the_candidates():
    # Checks: "sam" matches two people, so the lookup refuses and names both
    # emails so the caller can be specific.
    # Why: picking one of two people at random would be wrong half the time.
    with pytest.raises(MemberError, match="matches 2 workspace members") as err:
        members.find_workspace_member(WORKSPACE_MEMBERS, "sam")
    assert "sam.one@example.com" in str(err.value) and "sam.two@example.com" in str(err.value)


def test_empty_reference_is_an_error():
    # Checks: an empty value is refused instead of matching someone by accident.
    # Why: "" or "  " must never resolve to a member.
    with pytest.raises(MemberError, match="required"):
        members.find_workspace_member(WORKSPACE_MEMBERS, "  ")


# --- the Guest rule -----------------------------------------------------------


def test_workspace_guest_cannot_join_a_project_as_admin_or_member():
    # Checks: a workspace Guest is refused with an error that says why.
    # Why: Plane itself would make a Guest a project Admin if asked. Guests
    # are limited on purpose, so neither this tool nor create_project's lead
    # may promote one.
    guest = members.find_workspace_member(WORKSPACE_MEMBERS, "gus")
    with pytest.raises(MemberError, match="is a workspace Guest"):
        members.require_admin_or_member(guest)


@pytest.mark.parametrize("ref", ["ana", "bo"])
def test_workspace_admins_and_members_qualify(ref):
    # Checks: workspace Admins and Members pass the rule.
    # Why: these are the people who are allowed to be project members.
    members.require_admin_or_member(members.find_workspace_member(WORKSPACE_MEMBERS, ref))


# --- add_project_member ----------------------------------------------------------


class FakeREST(PlaneREST):
    def __init__(self, fail_with=None):  # no real HTTP client
        self.fail_with = fail_with
        self.added = None

    def list_workspace_members(self, slug):
        return WORKSPACE_MEMBERS

    def resolve_project(self, slug, project):
        return {"id": "p-1", "identifier": "HOME", "name": "Home Lab"}

    def add_project_member(self, slug, project_id, member_id, role):
        if self.fail_with:
            raise self.fail_with
        self.added = (slug, project_id, member_id, role)
        return {"id": "pm-1", "member": member_id, "role": role}


@pytest.mark.parametrize("role, code", [("admin", 20), ("Member", 15)])
def test_add_project_member_sends_the_member_id_and_role_code(role, code):
    # Checks: "admin" / "member" (any case) become Plane's role numbers 20 /
    # 15, and the person is sent by their user id.
    # Why: Plane's endpoint only understands ids and role numbers; callers
    # should never need to know either.
    rest = FakeREST()
    result = members.add_project_member(rest, "ws", "HOME", "bo@example.com", role)
    assert rest.added == ("ws", "p-1", "u-bo", code)
    assert result == {"project": "HOME", "member": "bo@example.com", "role": role.lower(), "workspace": "ws"}


def test_add_project_member_refuses_the_guest_role_and_unknown_roles():
    # Checks: role must be "admin" or "member"; "guest" or a typo is refused
    # before anything is sent.
    # Why: the task allows adding Admins and Members only. A typo must not
    # quietly become some default role.
    for role in ("guest", "owner", ""):
        rest = FakeREST()
        with pytest.raises(MemberError, match="role must be 'admin' or 'member'"):
            members.add_project_member(rest, "ws", "HOME", "bo", role)
        assert rest.added is None


def test_add_project_member_refuses_a_workspace_guest():
    # Checks: the Guest rule applies here too, before anything is sent.
    # Why: otherwise this tool would be an easy way around the rule that
    # create_project enforces.
    rest = FakeREST()
    with pytest.raises(MemberError, match="is a workspace Guest"):
        members.add_project_member(rest, "ws", "HOME", "gus", "member")
    assert rest.added is None


def test_add_project_member_reports_plane_errors_clearly():
    # Checks: if Plane rejects the request (e.g. the person is already a
    # member), the error names who and which project, plus Plane's reason.
    # Why: a bare HTTP 400 would leave the caller guessing what went wrong.
    rest = FakeREST(fail_with=RestError('POST ... -> 400: {"error":"The payload is not valid"}'))
    with pytest.raises(MemberError, match=r"could not add bo@example.com to project HOME.*400"):
        members.add_project_member(rest, "ws", "HOME", "bo", "admin")
