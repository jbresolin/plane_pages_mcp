import pytest

from plane_pages_mcp import convert


def test_markdown_to_html_tables_and_fenced_code():
    md = "## Heading\n\n- a\n- b\n\n| x | y |\n| - | - |\n| 1 | 2 |\n\n```py\nprint(1)\n```"
    html = convert.markdown_to_html(md)
    assert "<h2>" in html
    assert "<ul>" in html and "<li>" in html
    assert "<table>" in html and "<td>" in html
    assert "<pre><code" in html


def test_to_html_markdown_default():
    assert convert.to_html("# Hi", "markdown").startswith("<h1>")


def test_to_html_raw_html_passthrough():
    assert convert.to_html("<p>hi</p>", "html") == "<p>hi</p>"


def test_to_html_rejects_plain_text_as_html():
    # The live converter's zod validation rejects tag-less input; catch it early.
    with pytest.raises(convert.ContentError):
        convert.to_html("just words", "html")


def test_to_html_rejects_empty_markdown():
    with pytest.raises(convert.ContentError):
        convert.to_html("", "markdown")


def test_to_html_bad_format():
    with pytest.raises(convert.ContentError):
        convert.to_html("hi", "rst")


def test_html_to_stripped():
    assert convert.html_to_stripped("<h1>Title</h1><p>body text</p>") == "Titlebody text"


def test_html_to_markdown_roundtrip_ish():
    md = convert.html_to_markdown("<h2>Title</h2><p>hello</p>")
    assert "Title" in md and "hello" in md
    assert "##" in md
