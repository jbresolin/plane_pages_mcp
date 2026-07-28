"""Comment orchestration against a fake REST client."""

from plane_pages_mcp import comments
from plane_pages_mcp.rest import PlaneREST


class FakeREST(PlaneREST):
    def __init__(self, comment_rows=None):
        self.created = None
        self.updated = None
        self.deleted = None
        self._rows = comment_rows or []

    def resolve_project(self, slug, project):
        return {"id": "p", "identifier": "TEST", "name": "Test"}

    def resolve_issue_id(self, slug, pid, item):
        return "issue-uuid"

    def list_members(self, slug, pid):
        return [{"member": "u1", "display_name": "Alice"}]

    def list_comments(self, slug, pid, issue_id):
        return self._rows

    def create_comment(self, slug, pid, issue_id, payload):
        self.created = payload
        return {"id": "new-comment"}

    def update_comment(self, slug, pid, issue_id, comment_id, payload):
        self.updated = (comment_id, payload)
        return {"id": comment_id}

    def delete_comment(self, slug, pid, issue_id, comment_id):
        self.deleted = comment_id


def test_list_comments_shapes_body_actor_edited():
    rows = [{"id": "c1", "actor": "u1", "comment_html": "<h2>Hi</h2><p>there</p>",
             "created_at": "t0", "updated_at": "t1", "edited_at": "t1"}]
    out = comments.list_comments(FakeREST(rows), "test", "TEST", "TEST-1")
    c = out["comments"][0]
    assert c["actor"] == "Alice"            # resolved, not "u1"
    assert c["edited"] is True
    assert "## Hi" in c["body"] and "there" in c["body"]
    assert out["count"] == 1


def test_add_comment_sends_html_from_markdown():
    fake = FakeREST()
    out = comments.add_comment(fake, "test", "TEST", "TEST-1", body="hello **world**")
    assert "<strong>world</strong>" in fake.created["comment_html"]
    assert out["id"] == "new-comment"


def test_add_comment_html_passthrough():
    fake = FakeREST()
    comments.add_comment(fake, "test", "TEST", "TEST-1", body="<p>raw</p>", fmt="html")
    assert fake.created["comment_html"] == "<p>raw</p>"


def test_update_comment_targets_id_with_new_html():
    fake = FakeREST()
    comments.update_comment(fake, "test", "TEST", "TEST-1", "c9", body="edited")
    cid, payload = fake.updated
    assert cid == "c9" and "<p>edited</p>" in payload["comment_html"]


def test_delete_comment():
    fake = FakeREST()
    out = comments.delete_comment(fake, "test", "TEST", "TEST-1", "c9")
    assert fake.deleted == "c9" and out["deleted"] is True
