import pytest
from bs4 import BeautifulSoup

from plane_pages_mcp import convert


def _tags(html: str) -> list[str]:
    """Names of every HTML element in ``html``, e.g. ['p', 'code']."""
    return [el.name for el in BeautifulSoup(html, "html.parser").find_all()]


# --- markdown -> HTML (the write side) ---------------------------------------


def test_markdown_to_html_tables_and_fenced_code():
    # Checks: the common markdown building blocks (heading, bullet list, table,
    # fenced code) each turn into the matching HTML element.
    # Why: Plane only understands HTML, so if any of these came out as plain
    # text the page would lose its structure.
    md = "## Heading\n\n- a\n- b\n\n| x | y |\n| - | - |\n| 1 | 2 |\n\n```py\nprint(1)\n```"
    html = convert.markdown_to_html(md)
    assert "<h2>" in html
    assert "<ul>" in html and "<li>" in html
    assert "<table>" in html and "<td>" in html
    assert "<pre><code" in html


def test_angle_bracket_placeholder_in_text_stays_literal_text():
    # Checks: writing "<box>" in ordinary text produces the visible characters
    # "<box>" (escaped as &lt;box&gt;), not an HTML element called <box>.
    # Why: people write placeholders like <box> or Record<string, X> all the
    # time. Treated as HTML, the editor silently deleted them from the page.
    html = convert.markdown_to_html("Put it in <box> as Record<string, X> or Array<T>.")
    assert "&lt;box&gt;" in html
    assert "&lt;T&gt;" in html
    assert _tags(html) == ["p"]


def test_placeholder_named_like_a_real_html_tag_is_not_an_element():
    # Checks: placeholders that happen to be real HTML tag names (<img>,
    # <iframe>, <input>, <script>) also stay text and create no element.
    # Why: these are the dangerous ones. A real <img> with no source, or a
    # <script>, means something quite different to a browser than to a reader.
    html = convert.markdown_to_html("Tags: <img>, <iframe>, <input>, <script>.")
    assert _tags(html) == ["p"]
    assert "&lt;img&gt;" in html and "&lt;script&gt;" in html


def test_placeholder_on_its_own_line_does_not_swallow_the_table_below():
    # Checks: a line holding only "<box>", directly above a table, still lets
    # the table render as a real table.
    # Why: markdown treats a line that starts with an HTML tag as the start of
    # a raw HTML block that runs to the next blank line. The table rows were
    # being copied as raw text and showed up in Plane as "| a | b |" garbage.
    html = convert.markdown_to_html("<box>\n| a | b |\n| - | - |\n| 1 | 2 |\n")
    assert "&lt;box&gt;" in html
    assert "<table>" in html and "<td>2</td>" in html


def test_unclosed_raw_block_tags_do_not_hide_the_rest_of_the_page():
    # Checks: an unclosed "<pre>" or "<textarea>" in the text doesn't swallow
    # the paragraphs that follow it.
    # Why: in raw HTML these tags capture everything up to their closing tag.
    # One stray "<pre>" in a sentence hid the whole rest of the document.
    html = convert.markdown_to_html("<pre>\n\nMiddle.\n\n<textarea>\n\nEnd.")
    assert "<p>Middle.</p>" in html and "<p>End.</p>" in html
    assert "pre" not in _tags(html) and "textarea" not in _tags(html)


def test_angle_brackets_inside_code_are_still_shown_as_written():
    # Checks: angle brackets inside inline code and fenced code keep working
    # exactly as before.
    # Why: guards against "fixing" the text case by double-escaping code,
    # which would show "&lt;" to the reader instead of "<".
    html = convert.markdown_to_html("Use `Record<string, X>`.\n\n```ts\nconst b = <box/>;\n```")
    assert "<code>Record&lt;string, X&gt;</code>" in html
    assert "const b = &lt;box/&gt;;" in html


def test_autolinks_still_become_links():
    # Checks: "<https://example.com>" (the markdown way to write a bare link)
    # still becomes a clickable link.
    # Why: autolinks also use angle brackets, so turning off raw HTML must not
    # turn them into text by accident.
    html = convert.markdown_to_html("See <https://example.com/x>.")
    assert '<a href="https://example.com/x">' in html


def test_to_html_markdown_default():
    # Checks: markdown is the default input format.
    # Why: most callers send markdown and don't pass a format at all.
    assert convert.to_html("# Hi", "markdown").startswith("<h1>")


def test_to_html_raw_html_passthrough():
    # Checks: with format="html" the caller's HTML is passed on untouched.
    # Why: the "markdown means text" rule applies only to markdown input. A
    # caller who sends HTML on purpose gets HTML.
    assert convert.to_html("<p>hi</p>", "html") == "<p>hi</p>"


def test_to_html_rejects_plain_text_as_html():
    # Checks: plain text sent as format="html" is rejected with a clear error.
    # Why: Plane's converter refuses input without any tag; failing here gives
    # a better message than the converter's.
    with pytest.raises(convert.ContentError):
        convert.to_html("just words", "html")


def test_to_html_rejects_empty_markdown():
    # Checks: empty markdown is rejected.
    # Why: it would produce no HTML at all, which the converter also refuses.
    with pytest.raises(convert.ContentError):
        convert.to_html("", "markdown")


def test_to_html_bad_format():
    # Checks: an unknown format name is rejected.
    # Why: a typo should fail loudly, not be guessed at.
    with pytest.raises(convert.ContentError):
        convert.to_html("hi", "rst")


def test_html_to_stripped():
    # Checks: the plain-text copy Plane uses for search is just the text.
    # Why: search_pages matches against it.
    assert convert.html_to_stripped("<h1>Title</h1><p>body text</p>") == "Titlebody text"


# --- HTML -> markdown (the read side) ----------------------------------------


def test_html_to_markdown_roundtrip_ish():
    # Checks: headings and paragraphs come back as markdown.
    # Why: read_page returns markdown by default.
    md = convert.html_to_markdown("<h2>Title</h2><p>hello</p>")
    assert "Title" in md and "hello" in md
    assert "##" in md


def test_pipe_inside_a_table_cell_is_escaped_when_read_back():
    # Checks: a cell containing "a | b" comes back as "a \| b", including
    # inside inline code, and the table keeps exactly two columns.
    # Why: an unescaped "|" starts a new column. Read and written back, the
    # table would quietly gain a column and the meaning would change. The
    # HTML here is the shape Plane's editor stores (a <p> inside each cell).
    html = (
        "<table><tbody>"
        "<tr><th><p>Expr</p></th><th><p>Code</p></th></tr>"
        "<tr><td><p>a | b</p></td><td><p><code>x | y</code></p></td></tr>"
        "</tbody></table>"
    )
    md = convert.html_to_markdown(html)
    assert "| a \\| b | `x \\| y` |" in md


@pytest.mark.parametrize(
    "markdown, must_contain",
    [
        ("Put it in <box> as Record<string, X>.", ["<box>", "Record<string, X>"]),
        ("| k | v |\n| - | - |\n| a \\| b | — |", ["a \\| b", "—"]),
        ("Code `Map<K, V>` here.", ["`Map<K, V>`"]),
        ("<pre>\n\nafter", ["<pre>", "after"]),
    ],
)
def test_markdown_survives_a_write_and_read_round_trip(markdown, must_contain):
    # Checks: markdown -> HTML (what we write) -> markdown (what read_page
    # returns) keeps the parts that carry the meaning.
    # Why: the acceptance criterion is "write markdown, read it back, and the
    # meaning is intact". This is the offline version of that check; the
    # e2e harness repeats it through the real Plane editor.
    read_back = convert.html_to_markdown(convert.markdown_to_html(markdown))
    for fragment in must_contain:
        assert fragment in read_back, f"{fragment!r} lost; read back: {read_back!r}"
