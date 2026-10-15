"""Open a page in the real Plane web editor and judge whether it loaded.

"Loaded" means all of these hold:
  1. the page's body editor appears (Plane renders it as
     ``<div id="<page id>"><div class="ProseMirror">``; the title is a
     separate editor, so a bare ``.ProseMirror`` match is not enough);
  2. the sentinel text written at the very end of the page shows up inside
     it, so the content really synced from the live server and nothing
     swallowed the end of the document;
  3. the browser tab still answers a trivial script afterwards (a frozen
     editor blocks the page's main thread, so even ``() => true`` times out);
  4. no console or page errors beyond the ones a known-good page also logs
     (Plane's production build logs some React hydration noise on every page;
     see ``baseline_errors``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from playwright.sync_api import Browser, Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

EDITOR_TIMEOUT_MS = 30_000
SETTLE_MS = 2_000
RESPONSIVE_TIMEOUT_MS = 5_000


@dataclass
class ProbeResult:
    page_id: str
    url: str
    problems: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return not self.problems


def page_url(web_url: str, workspace_slug: str, project_id: str, page_id: str) -> str:
    return f"{web_url}/{workspace_slug}/projects/{project_id}/pages/{page_id}"


def error_signature(message: str) -> str:
    """Collapse ids and numbers so the same error from two pages compares equal."""
    return re.sub(r"[0-9a-f-]{8,}|\d+", "#", message)[:200]


def probe_page(
    browser: Browser,
    session_id: str,
    url: str,
    page_id: str,
    sentinel: str,
    baseline_errors: frozenset[str] = frozenset(),
) -> ProbeResult:
    result = ProbeResult(page_id=page_id, url=url)
    context = browser.new_context()
    domain = re.sub(r"^https?://([^/:]+).*$", r"\1", url)
    context.add_cookies([{"name": "session-id", "value": session_id, "domain": domain, "path": "/"}])
    tab = context.new_page()
    tab.on("pageerror", lambda exc: result.errors.append(f"pageerror: {exc}"))
    tab.on(
        "console",
        lambda msg: result.errors.append(f"console: {msg.text}") if msg.type == "error" else None,
    )
    body_editor = f'[id="{page_id}"] > .ProseMirror'
    try:
        tab.goto(url, wait_until="domcontentloaded", timeout=EDITOR_TIMEOUT_MS)
        try:
            tab.wait_for_selector(body_editor, timeout=EDITOR_TIMEOUT_MS)
        except PlaywrightTimeout:
            result.problems.append("body editor never appeared")
            return result
        try:
            tab.wait_for_function(
                "([sel, text]) => document.querySelector(sel)?.innerText.includes(text)",
                arg=[body_editor, sentinel],
                timeout=EDITOR_TIMEOUT_MS,
            )
        except PlaywrightTimeout:
            result.problems.append("content never finished rendering (end-of-page sentinel missing)")
        tab.wait_for_timeout(SETTLE_MS)
        try:
            tab.wait_for_function("() => true", timeout=RESPONSIVE_TIMEOUT_MS)
        except PlaywrightTimeout:
            result.problems.append("tab stopped responding (editor hang)")
    except PlaywrightError as exc:
        result.problems.append(f"browser error: {exc}")
    finally:
        context.close()

    new_errors = [e for e in result.errors if error_signature(e) not in baseline_errors]
    if new_errors:
        result.problems.append(f"{len(new_errors)} new console/page error(s): {new_errors[:3]}")
    return result


def baseline_errors(result: ProbeResult) -> frozenset[str]:
    """Errors a known-good page logs; later probes ignore these."""
    return frozenset(error_signature(e) for e in result.errors)
