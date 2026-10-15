"""Pure content conversions: markdown <-> HTML and HTML -> stripped text.

These are deterministic and side-effect-free so they can be unit-tested without
a database or the live service.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from markdownify import MarkdownConverter

# CommonMark + GitHub-flavoured tables + strikethrough. CommonMark (unlike
# python-markdown) lets a list interrupt a paragraph, so natural markdown like
# "…and a list:\n- one\n- two" renders as a real <ul> without a blank line.
# Fenced code is native to CommonMark. Linkify is intentionally NOT enabled
# (avoids the optional linkify-it-py dependency and surprise auto-links).
#
# Raw HTML is switched OFF ("html": False): anything that looks like a tag in
# markdown input is escaped and shown as text. Agents write placeholders such
# as <box> or Record<string, X> as prose, and as raw HTML they were silently
# deleted by Plane's editor, or (for <pre>, <script>, <textarea>, or a tag
# alone on a line) swallowed the content after them. Callers who really mean
# HTML use format="html". Autolinks (<https://...>) are a separate markdown
# rule and keep working. See docs/decisions.md, 2026-10-15.
_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


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


class _PlaneMarkdownConverter(MarkdownConverter):
    """markdownify, plus escaping of "|" inside table cells.

    In a markdown table an unescaped "|" starts a new column, so a cell that
    reads "a | b" must come back as "a \\| b" (GFM allows the escape inside
    code spans in a cell too). Without this, reading a page and writing it
    back would quietly add a column.
    """

    def convert_td(self, el, text, parent_tags):
        return super().convert_td(el, text.replace("|", "\\|"), parent_tags)

    def convert_th(self, el, text, parent_tags):
        return super().convert_th(el, text.replace("|", "\\|"), parent_tags)


def html_to_markdown(html: str) -> str:
    """Best-effort HTML -> markdown for read_page(format='markdown')."""
    return _PlaneMarkdownConverter(heading_style="ATX").convert(html or "").strip()


def _contains_tag(html: str) -> bool:
    return BeautifulSoup(html or "", "html.parser").find() is not None
