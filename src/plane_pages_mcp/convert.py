"""Pure content conversions: markdown <-> HTML and HTML -> stripped text.

These are deterministic and side-effect-free so they can be unit-tested without
a database or the live service.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from markdownify import markdownify as _markdownify

# CommonMark + GitHub-flavoured tables + strikethrough. CommonMark (unlike
# python-markdown) lets a list interrupt a paragraph, so natural markdown like
# "…and a list:\n- one\n- two" renders as a real <ul> without a blank line.
# Fenced code is native to CommonMark. Linkify is intentionally NOT enabled
# (avoids the optional linkify-it-py dependency and surprise auto-links).
_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])


class ContentError(ValueError):
    """Raised when content cannot be turned into converter-acceptable HTML."""


def markdown_to_html(text: str) -> str:
    """Render markdown to HTML (CommonMark + tables + strikethrough)."""
    return _MD.render(text)


def to_html(content: str, fmt: str) -> str:
    """Normalise user-supplied content to HTML for the write pipeline.

    ``fmt`` is "markdown" (default) or "html". The live convert endpoint's zod
    validation rejects plain text, so we guarantee at least one tag: markdown
    always produces block-level tags, and raw HTML is validated to contain one.
    """
    if fmt == "markdown":
        html = markdown_to_html(content)
    elif fmt == "html":
        html = content
    else:
        raise ContentError(f"format must be 'markdown' or 'html', got {fmt!r}")

    if not _contains_tag(html):
        raise ContentError(
            "content produced no HTML tags; the live converter requires at least "
            "one tag (was the content empty or plain text passed as html?)"
        )
    return html


def html_to_stripped(html: str) -> str:
    """Plain-text projection stored in pages.description_stripped."""
    return BeautifulSoup(html, "html.parser").get_text()


def html_to_markdown(html: str) -> str:
    """Best-effort HTML -> markdown for read_page(format='markdown')."""
    return _markdownify(html or "", heading_style="ATX").strip()


def _contains_tag(html: str) -> bool:
    return BeautifulSoup(html or "", "html.parser").find() is not None
