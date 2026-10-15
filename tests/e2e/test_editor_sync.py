"""Updates must not fight the editor's own copies of a page.

Plane's editor keeps two copies of a page outside the database:
  * the browser caches the page's Yjs document in IndexedDB after a visit;
  * while a page is open, the live server holds it in memory.

Each test below reproduces, in a real browser, a way an update_page call used
to be lost because of one of those copies.
"""

from __future__ import annotations

import time

import pytest

from editor_probe import page_url

from plane_pages_mcp import pipeline

pytestmark = pytest.mark.usefixtures("database")


def _logged_in_context(browser, session_id):
    # One context = one browser profile; its IndexedDB survives closing tabs.
    context = browser.new_context()
    context.add_cookies([{"name": "session-id", "value": session_id,
                          "domain": "localhost", "path": "/"}])
    return context


def _open_and_read(context, url, page_id, expect_text) -> str:
    """Open the page in a new tab, wait for ``expect_text``, return the editor text."""
    tab = context.new_page()
    body = f'[id="{page_id}"] > .ProseMirror'
    tab.goto(url)
    tab.wait_for_function(
        "([sel, text]) => document.querySelector(sel)?.innerText.includes(text)",
        arg=[body, expect_text], timeout=30_000,
    )
    # Give the cached copy time to merge in, which is when the bug showed.
    tab.wait_for_timeout(4_000)
    text = tab.evaluate("(sel) => document.querySelector(sel).innerText", body)
    tab.close()
    return text


def _wait_until_released(presence, page_id, timeout=15.0) -> None:
    """Wait until the live server has unloaded the page after its tab closed.

    Hocuspocus keeps a page loaded for about a second after the last tab
    leaves, and update_page (correctly) refuses during that time.
    """
    deadline = time.monotonic() + timeout
    while presence.is_open(page_id):
        assert time.monotonic() < deadline, "live server never released the page"


def _create(database, live, workspace_id, project_id, service_user_id, title, text):
    return pipeline.create_page(
        database, live, workspace_id=workspace_id, title=title, content=text,
        project=project_id, fmt="markdown", access="public",
        service_user_id=service_user_id,
    )["id"]


def test_browser_that_saw_the_old_version_shows_only_the_update(
    browser, session_id, config, database, live, presence, workspace_id,
    project_id, service_user_id, unique_title,
):
    # What this checks: someone opens a page, closes it, the MCP replaces the
    # content, and that same person opens it again. They must see only the
    # new content, and the database must keep only the new content.
    # Why: the browser had kept the old version. Every MCP write used to be
    # an unrelated new document, so on the next visit the browser merged old
    # and new and saved that mix back for everyone. This was the
    # "create vs update" difference behind the broken pages.
    page_id = _create(database, live, workspace_id, project_id, service_user_id,
                      unique_title, "OLD-VERSION paragraph.")
    url = page_url(config.web_url, config.workspace_slug, project_id, page_id)
    context = _logged_in_context(browser, session_id)
    try:
        _open_and_read(context, url, page_id, "OLD-VERSION")
        _wait_until_released(presence, page_id)

        pipeline.update_page(
            database, live, page_id=page_id, content="NEW-VERSION paragraph.",
            fmt="markdown", title=None, mode="replace",
            service_user_id=service_user_id, presence=presence,
        )

        shown = _open_and_read(context, url, page_id, "NEW-VERSION")
    finally:
        context.close()
    assert "OLD-VERSION" not in shown, f"old content came back in the editor: {shown!r}"
    stored = database.read_page(page_id)["description_html"]
    assert "NEW-VERSION" in stored and "OLD-VERSION" not in stored


def test_update_is_refused_while_the_page_is_open(
    browser, session_id, config, database, live, presence, workspace_id,
    project_id, service_user_id, unique_title,
):
    # What this checks: while a page is open in a tab, update_page refuses
    # with a clear message and leaves the page untouched. Once the tab
    # closes, the same update goes through.
    # Why: while the page is open, the live server's in-memory copy
    # overwrites anything written to the database, so the update used to
    # "succeed" and then silently vanish.
    page_id = _create(database, live, workspace_id, project_id, service_user_id,
                      unique_title, "ORIGINAL paragraph.")
    url = page_url(config.web_url, config.workspace_slug, project_id, page_id)
    update = dict(page_id=page_id, content="CHANGED paragraph.", fmt="markdown",
                  title=None, mode="replace", service_user_id=service_user_id,
                  presence=presence)
    context = _logged_in_context(browser, session_id)
    try:
        tab = context.new_page()
        tab.goto(url)
        tab.wait_for_selector(f'[id="{page_id}"] > .ProseMirror', timeout=30_000)
        tab.wait_for_timeout(2_000)

        with pytest.raises(pipeline.WriteError, match="open in the Plane editor"):
            pipeline.update_page(database, live, **update)
        assert "ORIGINAL" in database.read_page(page_id)["description_html"]

        tab.close()
    finally:
        context.close()
    _wait_until_released(presence, page_id)

    pipeline.update_page(database, live, **update)
    assert "CHANGED" in database.read_page(page_id)["description_html"]
