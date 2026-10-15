"""Fixtures for the editor harness. Skipped unless PLANE_E2E=1.

Run it against the dev stack with:

    PLANE_E2E=1 PLANE_E2E_WEB_URL=http://localhost:8080 \\
        .venv/bin/python -m pytest tests/e2e -v

See tests/e2e/README.md for every setting.
"""

from __future__ import annotations

import os
import uuid

import pytest

if os.environ.get("PLANE_E2E") != "1":
    # collect_ignore_glob keeps pytest from even importing the test modules,
    # so the normal unit-test run needs neither Playwright nor a Plane stack.
    collect_ignore_glob = ["test_*.py"]

# pytest puts this directory on sys.path (the default "prepend" import mode),
# which is why these are plain, not relative, imports.
from plane_dev import (  # noqa: E402
    TITLE_PREFIX,
    HarnessConfig,
    PlaneAppClient,
    mint_session,
    soft_delete_harness_pages,
    user_id_for_email,
)


@pytest.fixture(scope="session")
def config() -> HarnessConfig:
    return HarnessConfig.from_env()


@pytest.fixture(scope="session")
def database(config):
    from plane_pages_mcp.db import Database

    # Leftovers from the previous run are cleared at the start, not the end,
    # so you can still open this run's pages by hand to look at a failure.
    soft_delete_harness_pages(config.database_url)
    db = Database(config.database_url)
    yield db
    db.close()


@pytest.fixture(scope="session")
def live(config):
    from plane_pages_mcp.live import LiveConverter

    return LiveConverter(config.live_convert_url)


@pytest.fixture(scope="session")
def workspace_id(config, database) -> str:
    return database.resolve_workspace(config.workspace_slug)


@pytest.fixture(scope="session")
def project_id(config, database, workspace_id) -> str:
    return database.resolve_project(workspace_id, config.project_identifier)["id"]


@pytest.fixture(scope="session")
def service_user_id(config) -> str:
    # Pages are attributed to the same user who opens them in the browser, so
    # the harness needs no second account.
    return user_id_for_email(config.database_url, config.browser_user_email)


@pytest.fixture(scope="session")
def session_id(config) -> str:
    return mint_session(config)


@pytest.fixture(scope="session")
def app_client(config, session_id):
    client = PlaneAppClient(config.web_url, session_id)
    yield client
    client.close()


@pytest.fixture(scope="session")
def presence(config):
    import redis

    from plane_pages_mcp.presence import LivePresence

    return LivePresence(redis.Redis.from_url(config.redis_url, socket_connect_timeout=3))


@pytest.fixture(scope="session")
def browser(config):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        launched = playwright.chromium.launch(executable_path=config.chromium_path)
        yield launched
        launched.close()


@pytest.fixture
def unique_title(request) -> str:
    # The pytest node id names the construct and the write path, so a failing
    # page is easy to find in the Plane UI.
    return f"{TITLE_PREFIX}{request.node.name} {uuid.uuid4().hex[:6]}"
