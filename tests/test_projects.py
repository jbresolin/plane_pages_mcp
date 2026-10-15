"""Tests for projects.create_project (no Plane needed).

The rules being tested come from Plane v1.3.1's own source:
  * identifier: the web UI's create form only allows 1-10 characters from
    A-Z, 0-9 and the Turkish letters ÇŞĞIİÖÜ (checked after uppercasing). The
    API itself would accept more, but a project made with such an identifier
    would fail the UI's own form when someone later edits it, so we follow
    the UI. See docs/decisions.md.
  * name: Plane rejects names containing any of & + , : ; $ ^ } { * = ? @ #
    | ' < > . ( ) % ! - , and the column holds 255 characters.
  * duplicates: Plane answers HTTP 409 {"name": "The project name is already
    taken"} for BOTH a duplicate name and a duplicate identifier, so we look
    at the existing projects to say which one it was.

A fake REST client stands in for Plane and records what would have been sent.
"""

import pytest

from plane_pages_mcp import projects
from plane_pages_mcp.projects import ProjectError
from plane_pages_mcp.rest import PlaneREST, RestConflict

PLANE_DUPLICATE_BODY = '{"name":"The project name is already taken"}'


class FakeREST(PlaneREST):
    def __init__(self, existing=None, conflict=False):  # no real HTTP client
        self.existing = existing or []
        self.conflict = conflict
        self.sent = None

    def create_project(self, slug, payload):
        self.sent = (slug, payload)
        if self.conflict:
            raise RestConflict(f"POST .../projects/ -> 409: {PLANE_DUPLICATE_BODY}")
        return {"id": "new-id", "identifier": payload["identifier"], "name": payload["name"]}

    def list_projects(self, slug):
        return self.existing


def _create(rest, **overrides):
    args = {"name": "Home Lab", "identifier": "HOME", "description": None}
    args.update(overrides)
    return projects.create_project(rest, "test", **args)


# --- identifier validation (nothing is sent to Plane when it fails) -----------


@pytest.mark.parametrize("identifier", ["HOME", "home", "A", "ABCDEFGHIJ", "PRJ2026", "ÇŞĞ", " home "])
def test_valid_identifiers_are_accepted(identifier):
    # Checks: identifiers within the rule pass, in any letter case and with
    # stray spaces around them; the 10-character maximum is allowed.
    # Why: an agent may write "home" or " HOME ". Plane uppercases and
    # trims, so we do the same rather than rejecting a usable value.
    rest = FakeREST()
    _create(rest, identifier=identifier)
    assert rest.sent is not None


def test_identifier_is_sent_trimmed_and_uppercased():
    # Checks: " home " is sent to Plane as "HOME".
    # Why: what we validate must be exactly what Plane stores. Otherwise a
    # value could pass our check and then mean something else to Plane.
    rest = FakeREST()
    result = _create(rest, identifier=" home ")
    assert rest.sent[1]["identifier"] == "HOME"
    assert result["identifier"] == "HOME"


@pytest.mark.parametrize(
    "identifier, reason",
    [
        ("", "required"),
        ("   ", "required"),
        ("ABCDEFGHIJK", "at most 10"),     # 11 characters
        ("MY-PROJ", "only letters"),       # dash
        ("MY_PROJ", "only letters"),       # underscore: the API allows it, the UI doesn't
        ("MY PROJ", "only letters"),       # inner space
        ("ÉCOLE", "only letters"),         # accented letter outside the UI's list
        ("A.B", "only letters"),
    ],
)
def test_invalid_identifiers_are_rejected_before_calling_plane(identifier, reason):
    # Checks: each bad identifier raises ProjectError with a message saying
    # what is wrong, and nothing is sent to Plane.
    # Why: catching it here gives the caller a clear reason up front, instead
    # of a raw Plane error, or worse, a project that exists but can't be
    # edited in the Plane UI.
    rest = FakeREST()
    with pytest.raises(ProjectError, match=reason):
        _create(rest, identifier=identifier)
    assert rest.sent is None


# --- name validation -----------------------------------------------------------


@pytest.mark.parametrize("name", ["Home Lab (2026)", "R&D", "a-b", "v1.0", "What?"])
def test_names_with_characters_plane_forbids_are_rejected(name):
    # Checks: names containing characters Plane rejects (brackets, &, -, ., ?)
    # fail here with a message listing the forbidden characters.
    # Why: Plane's own error for this is a generic "cannot contain special
    # characters" without saying which ones; ours names them.
    rest = FakeREST()
    with pytest.raises(ProjectError, match="cannot contain"):
        _create(rest, name=name)
    assert rest.sent is None


def test_empty_or_too_long_names_are_rejected():
    # Checks: a blank name and a 256-character name are both refused.
    # Why: Plane requires a name, and its database column holds 255 characters.
    with pytest.raises(ProjectError, match="name is required"):
        _create(FakeREST(), name="  ")
    with pytest.raises(ProjectError, match="at most 255"):
        _create(FakeREST(), name="x" * 256)


# --- the request and the result -----------------------------------------------


def test_returns_the_new_projects_id_and_identifier():
    # Checks: a successful create returns the new project's id and identifier
    # (plus its name and workspace for context).
    # Why: the id and identifier are what the caller needs next, e.g. to
    # create work items in the new project.
    result = _create(FakeREST())
    assert result == {"id": "new-id", "identifier": "HOME", "name": "Home Lab", "workspace": "test"}


def test_description_is_sent_only_when_given():
    # Checks: the description field is left out unless the caller passes one.
    # Why: sending "description": null could overwrite Plane's default ("").
    # Only fields the caller actually set are sent.
    rest = FakeREST()
    _create(rest)
    assert "description" not in rest.sent[1]
    _create(rest, description="Self-hosted services")
    assert rest.sent[1]["description"] == "Self-hosted services"


# --- duplicates ----------------------------------------------------------------


def test_duplicate_identifier_gets_a_clear_message():
    # Checks: when Plane answers 409 and a project with that identifier
    # already exists, the error names the identifier and the project that
    # holds it.
    # Why: Plane's own message for this case is "The project name is already
    # taken", which is wrong (it was the identifier) and would send the
    # caller to change the wrong field.
    rest = FakeREST(conflict=True, existing=[{"identifier": "HOME", "name": "Old Home"}])
    with pytest.raises(ProjectError, match=r"identifier 'HOME' is already used by project 'Old Home'"):
        _create(rest, identifier="home")


def test_duplicate_name_gets_a_clear_message():
    # Checks: when the identifier is free but the exact name is taken, the
    # error says the name is the problem.
    # Why: same 409 from Plane; this is the other case it covers.
    rest = FakeREST(conflict=True, existing=[{"identifier": "LAB", "name": "Home Lab"}])
    with pytest.raises(ProjectError, match=r"name 'Home Lab' is already used"):
        _create(rest)


def test_name_differing_only_in_case_is_not_blamed():
    # Checks: an existing "home lab" is NOT reported as the cause of a 409
    # for "Home Lab".
    # Why: Plane compares names case-sensitively (on v1.3.1, "ZZ PROBE" and
    # "zz probe" were both accepted), so that project can't be the clash.
    # Blaming it would send the caller to change a name that was fine.
    rest = FakeREST(conflict=True, existing=[{"identifier": "LAB", "name": "home lab"}])
    with pytest.raises(ProjectError, match="name or identifier is already in use"):
        _create(rest)


def test_conflict_with_a_project_we_cannot_see_still_explains_itself():
    # Checks: if Plane says 409 but no project we can list matches (it may be
    # archived, or one the API key's user isn't a member of), the error still
    # says that the name or identifier is taken.
    # Why: we must never claim success or show a raw 409. The best honest
    # answer is "one of these two is taken".
    rest = FakeREST(conflict=True, existing=[])
    with pytest.raises(ProjectError, match="name or identifier is already in use"):
        _create(rest)
