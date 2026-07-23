"""Relation direction mapping — the subtle part — against a fake DB."""

import contextlib

import pytest

from plane_pages_mcp import relations
from plane_pages_mcp.relations import RelationError


class FakeDB:
    """Resolves I->'i', R->'r'; records the stored relation triple."""

    def __init__(self, read_rows=None):
        self.inserted = None
        self.deleted = None
        self._read_rows = read_rows or []
        self._refs = {"i": "TEST-1", "r": "TEST-2", "o": "TEST-9"}

    def resolve_workspace(self, slug):
        return "ws"

    def resolve_project(self, ws_id, project):
        return {"id": "p", "identifier": "TEST", "name": "Test"}

    def resolve_issue(self, ws_id, pid, ref):
        mapping = {"TEST-1": ("i", 1), "TEST-2": ("r", 2), "I": ("i", 1), "R": ("r", 2)}
        iid, seq = mapping[ref]
        return {"id": iid, "sequence_id": seq}

    @contextlib.contextmanager
    def transaction(self):
        yield None

    def insert_issue_relation(self, conn, *, workspace_id, project_id, issue_id,
                              related_issue_id, relation_type, service_user_id):
        self.inserted = (issue_id, related_issue_id, relation_type)
        return "rel-id"

    def delete_issue_relation(self, conn, *, issue_id, related_issue_id, relation_type):
        self.deleted = (issue_id, related_issue_id, relation_type)
        return 1

    def read_issue_relations(self, issue_id):
        return self._read_rows

    def issue_ref(self, issue_id):
        return self._refs.get(issue_id)


def _link(db, rtype):
    return relations.link(db, workspace="w", project="TEST", item="I",
                          related_item="R", relation_type=rtype, service_user_id="u")


@pytest.mark.parametrize("friendly,expected", [
    ("blocks",        ("r", "i", "blocked_by")),   # I blocks R -> R blocked_by I
    ("blocking",      ("r", "i", "blocked_by")),   # alias of blocks
    ("blocked_by",    ("i", "r", "blocked_by")),
    ("relates_to",    ("i", "r", "relates_to")),
    ("duplicate",     ("i", "r", "duplicate")),
    ("start_before",  ("i", "r", "start_before")),
    ("start_after",   ("r", "i", "start_before")),
    ("finish_before", ("i", "r", "finish_before")),
    ("finish_after",  ("r", "i", "finish_before")),
    ("implements",    ("r", "i", "implemented_by")),  # I implements R -> R implemented_by I
    ("implemented_by", ("i", "r", "implemented_by")),
])
def test_link_stores_canonical_direction(friendly, expected):
    db = FakeDB()
    _link(db, friendly)
    assert db.inserted == expected


def test_link_unknown_type_lists_options():
    with pytest.raises(RelationError) as exc:
        _link(FakeDB(), "supersedes")
    assert "supersedes" in str(exc.value) and "blocked_by" in str(exc.value)


def test_link_self_relation_rejected():
    db = FakeDB()
    with pytest.raises(RelationError) as exc:
        relations.link(db, workspace="w", project="TEST", item="I",
                       related_item="I", relation_type="blocks", service_user_id="u")
    assert "cannot be related to itself" in str(exc.value)


def test_unlink_matches_stored_direction():
    db = FakeDB()
    relations.unlink(db, workspace="w", project="TEST", item="I", related_item="R",
                     relation_type="blocks", service_user_id="u")
    assert db.deleted == ("r", "i", "blocked_by")


def test_for_issue_forward_and_reverse_labels():
    # subject "s": one row where s is the `issue` (forward) and one where s is
    # the `related_issue` (reverse label).
    rows = [
        {"issue_id": "s", "related_issue_id": "o", "relation_type": "blocked_by"},
        {"issue_id": "o", "related_issue_id": "s", "relation_type": "blocked_by"},
        {"issue_id": "o", "related_issue_id": "s", "relation_type": "implemented_by"},
        {"issue_id": "s", "related_issue_id": "o", "relation_type": "relates_to"},
    ]
    db = FakeDB(read_rows=rows)
    db._refs["s"] = "TEST-5"
    out = relations.for_issue(db, "s")
    labels = {(r["relation"], r["related_item"]) for r in out}
    assert ("blocked_by", "TEST-9") in labels   # s blocked_by o  (forward)
    assert ("blocking", "TEST-9") in labels      # o blocked_by s -> s blocking o (reverse)
    assert ("implements", "TEST-9") in labels     # o implemented_by s -> s implements o
    assert ("relates_to", "TEST-9") in labels     # symmetric
