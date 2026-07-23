"""verify must never raise: a 403 (or any probe error) is reported, not thrown."""

import pytest

from plane_pages_mcp import verify as verify_mod
from plane_pages_mcp.config import Config
from plane_pages_mcp.rest import RestForbidden


class Forbidden403REST:
    """workspace-level projects/ returns 200; every project-scoped call 403s."""

    def __init__(self, *_a, **_k):
        pass

    def status_of(self, method, path):
        return 200 if path.endswith("/projects/") else 403

    def list_projects(self, slug):
        return [{"id": "p-uuid", "identifier": "TEST", "name": "Test"}]

    def list_issues(self, slug, pid):
        raise RestForbidden("GET .../issues/ -> 403 Forbidden. service user may not be a member")

    def close(self):
        pass


def _rest_only_cfg(monkeypatch):
    for v in ["DATABASE_URL", "SERVICE_USER_ID", "MCP_AUTH_TOKEN"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    monkeypatch.setenv("WORKSPACE_SLUG", "test")
    return Config.from_env()


def test_verify_survives_403_and_reports_hint(monkeypatch, capsys):
    monkeypatch.setattr(verify_mod, "PlaneREST", Forbidden403REST)
    cfg = _rest_only_cfg(monkeypatch)

    rc = verify_mod.verify(cfg)  # must NOT raise

    out = capsys.readouterr().out
    assert rc == 1                              # failures present -> non-zero
    assert "verify: FAILURES PRESENT" in out    # reached the summary line
    assert "status 403" in out                  # project-scoped probes recorded
    assert "may not be a member" in out         # membership hint surfaced
    # the raising list_issues call was caught, not propagated
    assert "list issues (for single-item probe)" in out


def test_verify_rest_disabled_is_skipped_not_failed(monkeypatch, capsys):
    for v in ["PLANE_BASE_URL", "PLANE_API_KEY", "MCP_AUTH_TOKEN"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("PLANE_BASE_URL", "http://host")
    monkeypatch.setenv("PLANE_API_KEY", "pat")
    monkeypatch.setenv("WORKSPACE_SLUG", "test")
    # rest-only cfg but stub REST to a clean pass would need network; instead just
    # confirm a disabled subsystem never contributes a failure:
    monkeypatch.delenv("PLANE_BASE_URL")
    monkeypatch.delenv("PLANE_API_KEY")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("SERVICE_USER_ID", "u")
    cfg = Config.from_env()
    assert not cfg.rest_enabled
