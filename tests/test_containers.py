"""Cycle/module orchestration against a fake REST client (real resolve logic)."""

import pytest

from plane_pages_mcp import containers
from plane_pages_mcp.containers import ContainerError
from plane_pages_mcp.rest import PlaneREST, RestNotFound


class FakeREST(PlaneREST):
    def __init__(self):  # skip super().__init__ (no httpx client)
        self.created = None
        self.deleted = None
        self.added = None
        self.removed = []

    def resolve_project(self, slug, project):
        return {"id": "p", "identifier": "TEST", "name": "Test"}

    def list_containers(self, slug, pid, kind):
        if kind == "cycles":
            return [{"id": "c1", "name": "Sprint 1", "start_date": "2026-08-01",
                     "end_date": "2026-08-14", "total_issues": 3, "completed_issues": 1}]
        return [{"id": "m1", "name": "Auth", "start_date": None, "target_date": "2026-09-01",
                 "status": "in-progress", "total_issues": 5, "completed_issues": 2}]

    def create_container(self, slug, pid, kind, payload):
        self.created = (kind, payload)
        return {"id": "new", "name": payload["name"]}

    def delete_container(self, slug, pid, kind, cid):
        self.deleted = (kind, cid)

    def add_issues_to_container(self, slug, pid, kind, cid, issue_ids):
        self.added = (kind, cid, issue_ids)
        return {}

    def remove_issue_from_container(self, slug, pid, kind, cid, issue_id):
        self.removed.append((kind, cid, issue_id))

    def resolve_issue_id(self, slug, pid, ref):
        return {"TEST-1": "i1", "TEST-2": "i2"}[ref]


def test_list_cycles_shape():
    out = containers.list_containers(FakeREST(), "test", "TEST", "cycles")
    c = out["cycles"][0]
    assert c["name"] == "Sprint 1" and c["end_date"] == "2026-08-14"
    assert out["count"] == 1


def test_list_modules_maps_target_date_and_status():
    out = containers.list_containers(FakeREST(), "test", "TEST", "modules")
    m = out["modules"][0]
    assert m["end_date"] == "2026-09-01"   # target_date surfaced as end_date
    assert m["status"] == "in-progress"


def test_create_cycle_sends_project_id_and_end_date():
    fake = FakeREST()
    containers.create_container(fake, "test", "TEST", "cycles",
                               name="S2", start_date="2026-08-01", end_date="2026-08-14")
    kind, payload = fake.created
    assert kind == "cycles"
    assert payload["project_id"] == "p"       # cycle body quirk
    assert payload["end_date"] == "2026-08-14"


def test_create_module_maps_end_date_to_target_date():
    fake = FakeREST()
    containers.create_container(fake, "test", "TEST", "modules", name="M2", end_date="2026-09-01")
    _, payload = fake.created
    assert payload["target_date"] == "2026-09-01"
    assert "end_date" not in payload
    assert payload["project_id"] == "p"


def test_delete_resolves_by_name():
    fake = FakeREST()
    containers.delete_container(fake, "test", "TEST", "cycles", "Sprint 1")
    assert fake.deleted == ("cycles", "c1")


def test_delete_unknown_lists_names():
    with pytest.raises(RestNotFound) as exc:
        containers.delete_container(FakeREST(), "test", "TEST", "cycles", "Nope")
    assert "Sprint 1" in str(exc.value)


def test_assign_resolves_container_and_items():
    fake = FakeREST()
    out = containers.assign(fake, "test", "TEST", "modules", "Auth", ["TEST-1", "TEST-2"])
    kind, cid, ids = fake.added
    assert kind == "modules" and cid == "m1" and ids == ["i1", "i2"]
    assert out["count"] == 2


def test_assign_empty_items_errors():
    with pytest.raises(ContainerError):
        containers.assign(FakeREST(), "test", "TEST", "cycles", "Sprint 1", [])


def test_unassign_removes_each():
    fake = FakeREST()
    containers.unassign(fake, "test", "TEST", "cycles", "Sprint 1", ["TEST-1", "TEST-2"])
    assert {r[2] for r in fake.removed} == {"i1", "i2"}


def test_bad_kind_errors():
    with pytest.raises(ContainerError):
        containers.list_containers(FakeREST(), "test", "TEST", "sprints")
