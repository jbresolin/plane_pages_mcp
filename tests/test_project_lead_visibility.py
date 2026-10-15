"""Tests for create_project's lead and visibility (no Plane needed).

Why these exist: a project made by this MCP used to have only the MCP's own
service user as a member. The person who asked for it couldn't see it, and,
because the project is private, nobody else could either. So:
  * a lead (explicit, or DEFAULT_PROJECT_LEAD) is added as project Admin;
  * workspace Guests can't be lead, and that is checked before anything is
    created;
  * if adding the lead fails after the project exists, the project is kept
    and the result says so, naming add_project_member as the fix;
  * visibility is "private" unless asked otherwise. Plane v1.3.1's API
    ignores visibility, so a private project is made private with a direct
    database write after it is created.

project_fakes.py has the fake Plane (it behaves like v1.3.1: projects come
back public whatever we send).
"""

import pytest
from project_fakes import FakeDatabase, FakeREST

from plane_pages_mcp import projects
from plane_pages_mcp.projects import ProjectError
from plane_pages_mcp.rest import RestError


def _create(rest, db=None, **overrides):
    args = {"name": "Home Lab", "identifier": "HOME"}
    args.update(overrides)
    return projects.create_project(rest, "test", db=db, **args)


def _notes(result) -> str:
    return " ".join(result["notes"])


# --- visibility -----------------------------------------------------------------


def test_visibility_defaults_to_private():
    # Checks: without a visibility argument, the project ends up private: we
    # send network=0 to Plane AND write network=0 to the database, and the
    # result says "private".
    # Why: a project should only be seen by the people added to it unless
    # someone asks otherwise. Plane's own default (public) must never decide
    # by accident.
    rest, db = FakeREST(), FakeDatabase()
    result = _create(rest, db, lead="bo")
    assert rest.sent[1]["network"] == 0
    assert db.calls == [("new-id", 0)]
    assert result["visibility"] == "private"


def test_public_visibility_is_sent_explicitly_and_needs_no_database():
    # Checks: visibility="public" sends network=2 and writes nothing to the
    # database (Plane already makes it public).
    # Why: the value is always sent explicitly, and no database write happens
    # when none is needed.
    rest, db = FakeREST(), FakeDatabase()
    result = _create(rest, db, lead="bo", visibility="public")
    assert rest.sent[1]["network"] == 2
    assert db.calls == []
    assert result["visibility"] == "public"


def test_unknown_visibility_is_refused_before_creating_anything():
    # Checks: a value other than "private" / "public" is an error and no
    # project is created.
    # Why: a typo like "privte" must not quietly create a public project.
    rest = FakeREST()
    with pytest.raises(ProjectError, match="visibility must be 'private' or 'public'"):
        _create(rest, FakeDatabase(), visibility="privte")
    assert rest.sent is None


def test_private_project_stays_public_and_says_so_when_the_database_write_fails():
    # Checks: if the grant check passed but the write itself then fails (here:
    # the database raises), the project is kept, the result says "public",
    # and a note explains why.
    # Why: the project already exists in Plane at that point. Claiming it is
    # private when it isn't would be a dangerous lie; deleting it was not
    # asked for.
    rest = FakeREST()
    result = _create(rest, FakeDatabase(error=RuntimeError("permission denied for table projects")),
                     lead="bo")
    assert result["visibility"] == "public"
    assert "PUBLIC" in _notes(result) and "permission denied" in _notes(result)


def test_private_is_refused_without_database_access_and_nothing_is_created():
    # Checks: when the server has no database configured, a "private" request
    # is refused with an error that says how to fix it (set DATABASE_URL, or
    # ask for visibility="public"), and no project is created.
    # Why: Plane's API can't make a project private, so without the database
    # the project would come out public. Finding that out before creating
    # anything means no unwanted public project is ever made.
    rest = FakeREST()
    with pytest.raises(ProjectError, match="DATABASE_URL") as err:
        _create(rest, db=None, lead="bo")
    assert "visibility='public'" in str(err.value)
    assert rest.sent is None


def test_private_is_refused_without_the_update_grant_and_nothing_is_created():
    # Checks: when the database role lacks UPDATE on projects(network,
    # updated_at), a "private" request is refused with the exact GRANT to run
    # (or the visibility="public" alternative), and no project is created.
    # Why: same reason. The write would fail after the project exists,
    # leaving it public, so this is checked up front with the same query that
    # `verify` uses.
    rest, db = FakeREST(), FakeDatabase(has_grant=False)
    with pytest.raises(ProjectError, match=r"GRANT UPDATE \(network, updated_at\) ON projects") as err:
        _create(rest, db, lead="bo")
    assert "visibility='public'" in str(err.value)
    assert rest.sent is None and db.calls == []


def test_public_projects_need_neither_database_nor_grant():
    # Checks: visibility="public" works with no database at all.
    # Why: the up-front check is only about making projects private. A public
    # project must keep working on a REST-only server.
    result = _create(FakeREST(), db=None, lead="bo", visibility="public")
    assert result["visibility"] == "public" and result["notes"] == []


# --- lead -----------------------------------------------------------------------


def test_lead_is_added_as_project_admin():
    # Checks: the lead, given by email, is looked up and added to the new
    # project as Admin (Plane role 20), using their user id.
    # Why: this is what makes the project show up for the person who asked
    # for it.
    rest = FakeREST()
    result = _create(rest, FakeDatabase(), lead="bo@example.com")
    assert rest.members_added == [("new-id", "u-bo", 20)]
    assert result["lead"] == "bo@example.com"
    assert result["notes"] == []


def test_default_lead_is_used_when_no_lead_is_given():
    # Checks: with no lead argument, DEFAULT_PROJECT_LEAD (passed in as
    # default_lead) becomes the lead.
    # Why: the operator sets this once so every new project is visible to them
    # without the agent having to remember.
    rest = FakeREST()
    _create(rest, FakeDatabase(), default_lead="ana@example.com")
    assert rest.members_added == [("new-id", "u-ana", 20)]


def test_explicit_lead_wins_over_the_default():
    # Checks: when both are given, the explicit lead is used and the default
    # is not added.
    # Why: the caller's specific request beats a general setting.
    rest = FakeREST()
    _create(rest, FakeDatabase(), lead="bo", default_lead="ana@example.com")
    assert rest.members_added == [("new-id", "u-bo", 20)]


def test_no_lead_means_a_note_that_only_the_service_user_can_see_it():
    # Checks: with neither a lead nor a default, the project is still created,
    # nobody is added, and the note says only the service user is a member,
    # that nobody else can see the (private) project, and how to fix it.
    # Why: otherwise the project silently "disappears" for everyone except
    # the MCP's own account.
    rest = FakeREST()
    result = _create(rest, FakeDatabase())
    assert rest.members_added == []
    assert result["lead"] is None
    notes = _notes(result)
    assert "Only the service user (svc@example.com)" in notes
    assert "nobody else can see it" in notes and "add_project_member" in notes


def test_guest_lead_is_refused_before_creating_anything():
    # Checks: a lead who is a workspace Guest is refused, and no project is
    # created at all.
    # Why: Plane would otherwise make the Guest a project Admin. Checking
    # first means a refusal never leaves a half-made project behind.
    rest, db = FakeREST(), FakeDatabase()
    with pytest.raises(ProjectError, match="is a workspace Guest"):
        _create(rest, db, lead="gus")
    assert rest.sent is None and db.calls == []


def test_unknown_lead_is_refused_before_creating_anything():
    # Checks: a lead that matches no workspace member is refused before the
    # project is created.
    # Why: same reason. A typo in a name must not leave a lead-less project.
    rest = FakeREST()
    with pytest.raises(ProjectError, match="no workspace member matches"):
        _create(rest, FakeDatabase(), lead="bob")
    assert rest.sent is None


def test_failure_to_add_the_lead_keeps_the_project_and_explains_the_fix():
    # Checks: if Plane refuses to add the lead after the project exists, the
    # result still returns the project, says the lead was NOT added and why,
    # and gives the add_project_member call that fixes it.
    # Why: the project is real at that point. The caller needs its id, plus
    # an honest account and a one-step fix, not an error that hides that a
    # project was made.
    rest = FakeREST(add_member_error=RestError("POST .../members/ -> 400: payload is not valid"))
    result = _create(rest, FakeDatabase(), lead="bo")  # private, with DB + grant
    assert result["id"] == "new-id" and result["lead"] is None
    notes = _notes(result)
    assert "bo@example.com was NOT added" in notes and "payload is not valid" in notes
    assert "add_project_member(workspace='test', project='HOME', member='bo@example.com', role='admin')" in notes
    assert "nobody else can see it" in notes


def test_lead_who_is_the_service_user_is_not_added_twice():
    # Checks: if the lead is the API key's own user, nothing more is sent
    # (they're already the project's Admin), and there is no warning.
    # Why: adding them again makes Plane fail with an error, which would
    # produce a false "lead was not added" note.
    rest = FakeREST()
    result = _create(rest, FakeDatabase(), lead="svc@example.com")
    assert rest.members_added == []
    assert result["lead"] == "svc@example.com" and result["notes"] == []
