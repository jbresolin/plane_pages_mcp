"""Helpers that talk to a running Plane dev stack for the editor harness.

Everything here is configured by environment variables (see ``HarnessConfig``)
so the harness can be pointed at any Plane instance you control, e.g. after a
Plane upgrade. It must never be pointed at production: it creates and deletes
pages.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

import httpx
import psycopg

# Every page the harness creates starts with this, so a later run can find and
# soft-delete leftovers without touching anything a person wrote.
TITLE_PREFIX = "[e2e] "


@dataclass(frozen=True)
class HarnessConfig:
    database_url: str
    live_convert_url: str
    web_url: str
    workspace_slug: str
    project_identifier: str
    browser_user_email: str
    session_id: str | None
    api_container: str
    chromium_path: str | None
    redis_url: str

    @classmethod
    def from_env(cls) -> "HarnessConfig":
        env = os.environ.get
        return cls(
            database_url=env("PLANE_E2E_DATABASE_URL", "postgresql://plane:plane@localhost:5432/plane"),
            live_convert_url=env("PLANE_E2E_LIVE_URL", "http://localhost:3300/live/convert-document/"),
            web_url=env("PLANE_E2E_WEB_URL", "http://localhost").rstrip("/"),
            workspace_slug=env("PLANE_E2E_WORKSPACE", "test"),
            project_identifier=env("PLANE_E2E_PROJECT", "TEST"),
            browser_user_email=env("PLANE_E2E_USER_EMAIL", "test@bresolinrs.com.br"),
            session_id=env("PLANE_E2E_SESSION_ID"),
            api_container=env("PLANE_E2E_API_CONTAINER", "plane-dev-api-1"),
            chromium_path=env("PLANE_E2E_CHROMIUM"),
            # Plane's Redis, for the open-page check. The stock compose file
            # doesn't publish it; see README for exposing it on 6380.
            redis_url=env("PLANE_E2E_REDIS_URL", "redis://localhost:6380/0"),
        )


# Run inside the Plane api container. It creates a normal Django login session
# for the browser user, exactly what Plane's own login view stores, so the
# harness can open the editor without knowing (or changing) anyone's password.
_MINT_SESSION_SNIPPET = """
from django.contrib.auth import get_user_model, SESSION_KEY, BACKEND_SESSION_KEY, HASH_SESSION_KEY
from plane.db.models.session import SessionStore
user = get_user_model().objects.get(email={email!r})
store = SessionStore()
store[SESSION_KEY] = str(user.pk)
store[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
store[HASH_SESSION_KEY] = user.get_session_auth_hash()
store["device_info"] = {{"user_agent": "plane_pages_mcp e2e harness", "ip_address": "127.0.0.1", "domain": "localhost"}}
store.create()
print("SESSION=" + store.session_key)
"""


def mint_session(config: HarnessConfig) -> str:
    """Return a Plane ``session-id`` cookie value for the browser user."""
    if config.session_id:
        return config.session_id
    result = subprocess.run(
        [
            "docker", "exec", config.api_container, "python", "manage.py", "shell",
            "-c", _MINT_SESSION_SNIPPET.format(email=config.browser_user_email),
        ],
        capture_output=True, text=True, timeout=120, check=True,
    )
    for line in result.stdout.splitlines():
        if line.startswith("SESSION="):
            return line.removeprefix("SESSION=")
    raise RuntimeError(f"could not mint a session; api container said: {result.stderr[-500:]}")


def user_id_for_email(database_url: str, email: str) -> str:
    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
    if row is None:
        raise RuntimeError(f"no Plane user with email {email!r}")
    return str(row[0])


def soft_delete_harness_pages(database_url: str) -> int:
    """Soft-delete pages left by earlier harness runs (the way Plane deletes)."""
    with psycopg.connect(database_url) as conn:
        cur = conn.execute(
            "UPDATE pages SET deleted_at = now() WHERE name LIKE %s AND deleted_at IS NULL",
            (TITLE_PREFIX + "%",),
        )
        return cur.rowcount


class PlaneAppClient:
    """Plane's internal (session-authenticated) app API, as the web UI uses it.

    Used only to create a blank page the same way clicking "New page" does, so
    the update path is tested against a UI-born page and not one we made.
    """

    def __init__(self, web_url: str, session_id: str) -> None:
        self._client = httpx.Client(
            base_url=web_url, cookies={"session-id": session_id}, timeout=30
        )
        token = self._client.get("/auth/get-csrf-token/").json()["csrf_token"]
        self._headers = {"X-CSRFToken": token, "Referer": web_url + "/"}

    def create_blank_page(self, workspace_slug: str, project_id: str, title: str) -> str:
        response = self._client.post(
            f"/api/workspaces/{workspace_slug}/projects/{project_id}/pages/",
            json={"name": title, "access": 0},
            headers=self._headers,
        )
        response.raise_for_status()
        return response.json()["id"]

    def close(self) -> None:
        self._client.close()
