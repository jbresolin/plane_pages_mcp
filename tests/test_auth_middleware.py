"""BearerAuthMiddleware in isolation (no DB / live service needed)."""

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from plane_pages_mcp.server import BearerAuthMiddleware


def _client():
    async def mcp_like(_req):
        return PlainTextResponse("protected")

    async def health(_req):
        return PlainTextResponse("ok")

    app = Starlette(
        routes=[Route("/mcp", mcp_like, methods=["POST"]), Route("/healthz", health)],
        middleware=[Middleware(BearerAuthMiddleware, token="secret-abc")],
    )
    return TestClient(app)


def test_healthz_is_open():
    assert _client().get("/healthz").status_code == 200


def test_missing_token_rejected():
    assert _client().post("/mcp").status_code == 401


def test_wrong_token_rejected():
    r = _client().post("/mcp", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_correct_token_allowed():
    r = _client().post("/mcp", headers={"Authorization": "Bearer secret-abc"})
    assert r.status_code == 200
    assert r.text == "protected"
