"""A fake Plane for the create_project tests: records calls, behaves like v1.3.1.

Behaviour copied from the real Plane v1.3.1 (checked on the dev stack):
  * project create ignores "network" and always makes a public project (2);
  * a duplicate name or identifier is HTTP 409 "The project name is already taken".
"""

from plane_pages_mcp.rest import PlaneREST, RestConflict

PLANE_DUPLICATE_BODY = '{"name":"The project name is already taken"}'

SERVICE_USER = {"id": "u-svc", "email": "svc@example.com", "display_name": "svc", "role": 20}
WORKSPACE_MEMBERS = [
    SERVICE_USER,
    {"id": "u-ana", "email": "ana@example.com", "display_name": "ana", "role": 20},
    {"id": "u-bo", "email": "bo@example.com", "display_name": "bo", "role": 15},
    {"id": "u-gus", "email": "gus@example.com", "display_name": "gus", "role": 5},
]


class FakeREST(PlaneREST):
    def __init__(self, existing=None, conflict=False, add_member_error=None):  # no HTTP client
        self.existing = existing or []
        self.conflict = conflict
        self.add_member_error = add_member_error
        self.sent = None
        self.members_added: list[tuple] = []

    def create_project(self, slug, payload):
        self.sent = (slug, payload)
        if self.conflict:
            raise RestConflict(f"POST .../projects/ -> 409: {PLANE_DUPLICATE_BODY}")
        return {"id": "new-id", "identifier": payload["identifier"], "name": payload["name"],
                "network": 2}

    def list_projects(self, slug):
        return self.existing

    def list_workspace_members(self, slug):
        return WORKSPACE_MEMBERS

    def get_me(self):
        return SERVICE_USER

    def add_project_member(self, slug, project_id, member_id, role):
        if self.add_member_error:
            raise self.add_member_error
        self.members_added.append((project_id, member_id, role))
        return {"member": member_id, "role": role}


class FakeDatabase:
    """Stands in for the Database's private-visibility check and write."""

    def __init__(self, error=None, has_grant=True):
        self.error = error            # raised by the write itself
        self.has_grant = has_grant    # what the up-front grant check reports
        self.calls: list[tuple] = []

    def can_set_project_network(self):
        return self.has_grant

    def set_project_network(self, project_id, network):
        if self.error:
            raise self.error
        self.calls.append((project_id, network))
        return 1
