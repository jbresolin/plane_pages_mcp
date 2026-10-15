"""Does every kind of markdown we write open cleanly in Plane's web editor?

For each sample in constructs.py, and for each way this MCP writes a page,
the test writes the page, opens it in headless Chromium, and checks that the
editor loaded (see editor_probe.py for exactly what "loaded" means). Then it
reads the page back the way the read_page tool does and checks that the
sample's key strings survived.

The three write paths:
  * create          -- create_page writes a brand-new page;
  * update_replace  -- a blank page made through Plane's own app API (what
                       clicking "New page" does) is filled by update_page;
  * update_append   -- a page made by create_page gets the sample appended,
                       which glues two HTML documents together.
"""

from __future__ import annotations

import pytest

from constructs import ALL_CONSTRUCTS, Construct
from editor_probe import baseline_errors, page_url, probe_page

from plane_pages_mcp import convert, pipeline

WRITE_PATHS = ["create", "update_replace", "update_append"]


def _sentinel(title: str) -> str:
    return f"END-OF-PAGE {title[-6:]}"


def _write_page(path, construct_markdown, title, *, database, live, app_client,
                config, workspace_id, project_id, service_user_id, presence=None) -> str:
    """Write ``construct_markdown`` through one write path; return the page id."""
    body = f"{construct_markdown}\n\n{_sentinel(title)}\n"
    if path == "create":
        return pipeline.create_page(
            database, live, workspace_id=workspace_id, title=title, content=body,
            project=project_id, fmt="markdown", access="public",
            service_user_id=service_user_id,
        )["id"]
    if path == "update_replace":
        page_id = app_client.create_blank_page(config.workspace_slug, project_id, title)
        mode = "replace"
    else:
        page_id = pipeline.create_page(
            database, live, workspace_id=workspace_id, title=title,
            content="Existing paragraph before the append.", project=project_id,
            fmt="markdown", access="public", service_user_id=service_user_id,
        )["id"]
        mode = "append"
    pipeline.update_page(
        database, live, page_id=page_id, content=body, fmt="markdown", title=None,
        mode=mode, service_user_id=service_user_id, presence=presence,
    )
    return page_id


@pytest.fixture(scope="session")
def known_good_errors(browser, session_id, config, database, live, workspace_id,
                      project_id, service_user_id, app_client) -> frozenset[str]:
    """Console noise that Plane logs even for a plain, known-good page.

    Plane's production build logs some React errors on every page load. We open
    one plain page first and ignore exactly those messages afterwards, so the
    harness only flags errors that the content itself causes.
    """
    title = "[e2e] baseline plain page"
    page_id = _write_page(
        "update_replace", "Just a plain paragraph.", title, database=database, live=live,
        app_client=app_client, config=config, workspace_id=workspace_id,
        project_id=project_id, service_user_id=service_user_id,
    )
    url = page_url(config.web_url, config.workspace_slug, project_id, page_id)
    result = probe_page(browser, session_id, url, page_id, _sentinel(title))
    # Errors are expected here (they are what we are collecting); anything
    # else means the harness or the stack is broken, not the content.
    structural = [p for p in result.problems if "console/page error" not in p]
    assert not structural, f"even the plain baseline page failed to load: {structural}"
    return baseline_errors(result)


@pytest.mark.parametrize("path", WRITE_PATHS)
@pytest.mark.parametrize("construct", ALL_CONSTRUCTS, ids=lambda c: c.name)
def test_page_opens_in_editor_and_round_trips(
    construct: Construct, path: str, unique_title, browser, session_id, config,
    database, live, app_client, workspace_id, project_id, service_user_id,
    known_good_errors, presence,
):
    # What this checks: one markdown sample, written one way, must (a) open in
    # the real Plane editor without freezing or erroring, and (b) still say the
    # same thing when read back.
    # Why: pages written by this MCP have opened broken in the editor even
    # though reading them back through the API looked fine. Only a real
    # browser shows that, so we load each page the way a person would.
    page_id = _write_page(
        path, construct.markdown, unique_title, database=database, live=live,
        app_client=app_client, config=config, workspace_id=workspace_id,
        project_id=project_id, service_user_id=service_user_id, presence=presence,
    )
    url = page_url(config.web_url, config.workspace_slug, project_id, page_id)

    result = probe_page(browser, session_id, url, page_id, _sentinel(unique_title),
                        baseline_errors=known_good_errors)
    assert result.loaded, f"editor did not load cleanly at {url}: {result.problems}"

    # Opening the page lets Plane's live server write its own version of the
    # HTML back to the database, so this reads what the editor actually kept,
    # converted to markdown exactly as the read_page tool does.
    stored_html = database.read_page(page_id)["description_html"]
    read_back = convert.html_to_markdown(stored_html)
    missing = [s for s in construct.must_survive if s not in read_back]
    assert not missing, f"lost {missing} on the round trip; read back:\n{read_back}"
