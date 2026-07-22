"""Work-item shaping/resolution tested against a fake REST client.

FakeREST overrides only the network methods, so the real resolve_project /
resolve_issue_id / name->UUID logic in rest.py and workitems.py is exercised.
"""

import pytest

from plane_pages_mcp import workitems
from plane_pages_mcp.rest import PlaneREST, RestNotFound


class FakeREST(PlaneREST):
    def __init__(self):  # deliberately skip super().__init__ (no httpx client)
        self.created = None
        self.updated = None

    # canned data
    def list_projects(self, slug):
        return [{"id": "p-uuid", "identifier": "TEST", "name": "Test", "description": "d"}]

    def list_states(self, slug, pid):
        return [{"id": "s-todo", "name": "Todo", "group": "backlog"},
                {"id": "s-prog", "name": "In Progress", "group": "started"}]

    def list_labels(self, slug, pid):
        return [{"id": "l-bug", "name": "bug"}]

    def list_members(self, slug, pid):
        return [{"member": "m-alice", "display_name": "Alice", "email": "alice@x.com"}]

    def list_issues(self, slug, pid):
        return [
            {"id": "i1", "sequence_id": 42, "name": "First", "state": "s-prog",
             "assignees": ["m-alice"], "priority": "high", "updated_at": "t1"},
            {"id": "i2", "sequence_id": 43, "name": "Second", "state": "s-todo",
             "assignees": [], "priority": "none", "updated_at": "t2"},
        ]

    def get_issue(self, slug, pid, issue_id):
        return {"id": issue_id, "sequence_id": 42, "name": "First", "state": "s-prog",
                "assignees": ["m-alice"], "labels": ["l-bug"], "priority": "high",
                "description_html": "<h2>Body</h2><p>text</p>", "updated_at": "t1"}

    def create_issue(self, slug, pid, payload):
        self.created = payload
        return {"id": "new-uuid", "sequence_id": 99, "name": payload.get("name")}

    def update_issue(self, slug, pid, issue_id, payload):
        self.updated = (issue_id, payload)
        return {"id": issue_id, "sequence_id": 42}


def test_list_work_items_resolves_names_not_uuids():
    out = workitems.list_work_items(FakeREST(), "test", "TEST")
    assert out["count"] == 2
    first = out["work_items"][0]
    assert first["sequence_id"] == "TEST-42"
    assert first["state"] == "In Progress"          # not "s-prog"
    assert first["assignees"] == ["Alice"]          # not "m-alice"


def test_list_work_items_state_filter():
    out = workitems.list_work_items(FakeREST(), "test", "TEST", state="todo")
    assert [w["name"] for w in out["work_items"]] == ["Second"]


def test_list_work_items_assignee_filter():
    out = workitems.list_work_items(FakeREST(), "test", "TEST", assignee="alice")
    assert [w["name"] for w in out["work_items"]] == ["First"]


def test_get_work_item_by_sequence_ref_markdown_desc():
    out = workitems.get_work_item(FakeREST(), "test", "TEST", "TEST-42")
    assert out["sequence_id"] == "TEST-42"
    assert out["state"] == "In Progress"
    assert out["labels"] == ["bug"]
    assert "## Body" in out["description"] and "text" in out["description"]


def test_create_resolves_state_priority_label_assignee():
    fake = FakeREST()
    workitems.create_work_item(
        fake, "test", "TEST", title="New", description="hello",
        state="In Progress", priority="high", assignees=["Alice"], labels=["bug"],
    )
    p = fake.created
    assert p["name"] == "New"
    assert p["state"] == "s-prog"
    assert p["priority"] == "high"
    assert p["assignees"] == ["m-alice"]
    assert p["labels"] == ["l-bug"]
    assert "<p>hello</p>" in p["description_html"]


def test_create_unknown_state_errors_with_options():
    with pytest.raises(RestNotFound) as exc:
        workitems.create_work_item(FakeREST(), "test", "TEST", title="x", state="Done")
    assert "Todo" in str(exc.value)


def test_create_unknown_priority_errors():
    with pytest.raises(workitems.WorkItemError) as exc:
        workitems.create_work_item(FakeREST(), "test", "TEST", title="x", priority="critical")
    assert "urgent" in str(exc.value)


def test_update_sends_only_supplied_fields():
    fake = FakeREST()
    out = workitems.update_work_item(fake, "test", "TEST", "TEST-42", state="Todo")
    issue_id, payload = fake.updated
    assert issue_id == "i1"                 # resolved from TEST-42
    assert payload == {"state": "s-todo"}   # nothing else touched
    assert out["updated_fields"] == ["state"]


def test_update_no_fields_errors():
    with pytest.raises(workitems.WorkItemError):
        workitems.update_work_item(FakeREST(), "test", "TEST", "TEST-42")


def test_create_sub_work_item_resolves_parent_seq_ref():
    fake = FakeREST()
    workitems.create_work_item(fake, "test", "TEST", title="child", parent="TEST-43")
    assert fake.created["parent"] == "i2"  # TEST-43 -> issue i2


def test_create_sub_work_item_parent_uuid_passthrough():
    fake = FakeREST()
    workitems.create_work_item(fake, "test", "TEST", title="child",
                               parent="123e4567-e89b-12d3-a456-426614174000")
    assert fake.created["parent"] == "123e4567-e89b-12d3-a456-426614174000"
