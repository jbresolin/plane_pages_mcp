"""The markdown samples the editor harness writes into Plane, one page each.

Each sample is a ``Construct``: a short name, the markdown to write, and the
strings that must still be present when the page is read back as markdown
(``must_survive``). Those strings are what "the meaning is intact" means for
that construct, for example that a ``<box>`` placeholder is still a literal
``<box>`` and not silently dropped.

The list covers every construct the acceptance criteria name, each on its own,
followed by mixed documents (including the shape of the pages that broke in
production: tables + fenced code + angle-bracket placeholders).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Construct:
    name: str
    markdown: str
    must_survive: tuple[str, ...]


SINGLE_CONSTRUCTS: list[Construct] = [
    Construct(
        "plain_text",
        "# Heading\n\nA paragraph with **bold** and *italic*.\n\n- one\n- two\n",
        ("Heading", "**bold**", "one"),
    ),
    Construct(
        "table_basic",
        "| Name | Value |\n| --- | --- |\n| alpha | 1 |\n| beta | 2 |\n",
        ("alpha", "beta"),
    ),
    Construct(
        "table_pipe_in_cell",
        "| Expr | Meaning |\n| --- | --- |\n| a \\| b | either |\n| `x \\| y` | code pipe |\n",
        # Read back escaped, which is what keeps them inside one cell.
        ("a \\| b", "either", "x \\| y"),
    ),
    Construct(
        "table_em_dash_cell",
        "| Key | Value |\n| --- | --- |\n| missing | — |\n| — | — |\n",
        ("missing", "—"),
    ),
    Construct(
        "table_empty_cell",
        "| A | B | C |\n| --- | --- | --- |\n| 1 |  | 3 |\n|  |  |  |\n",
        ("A", "3"),
    ),
    Construct(
        "table_alignment",
        "| Left | Center | Right |\n| :--- | :---: | ---: |\n| l | c | r |\n",
        ("Left", "Center", "Right"),
    ),
    Construct(
        "table_wide",
        "| " + " | ".join(f"c{i}" for i in range(8)) + " |\n"
        + "| " + " | ".join("---" for _ in range(8)) + " |\n"
        + "".join(
            "| " + " | ".join(f"r{r}c{i}" for i in range(8)) + " |\n" for r in range(12)
        ),
        ("c7", "r11c7"),
    ),
    Construct(
        "code_fence_with_language",
        "```python\ndef add(a, b):\n    return a + b\n```\n",
        ("def add(a, b):", "return a + b"),
    ),
    Construct(
        "code_fence_without_language",
        "```\nplain code block\n  indented line\n```\n",
        ("plain code block", "indented line"),
    ),
    Construct(
        "code_fence_with_angle_brackets",
        "```ts\nconst map: Record<string, X> = {};\nconst el = <box>hi</box>;\n```\n",
        ("Record<string, X>", "<box>hi</box>"),
    ),
    Construct(
        "code_fence_empty",
        "```\n```\n\nText after an empty code block.\n",
        ("Text after an empty code block.",),
    ),
    Construct(
        "inline_code",
        "Call `do_thing()` with `--flag`.\n",
        ("`do_thing()`", "`--flag`"),
    ),
    Construct(
        "inline_code_with_angle_brackets",
        "The type is `Record<string, X>` and the tag is `<box>`.\n",
        ("Record<string, X>", "<box>"),
    ),
    Construct(
        "angle_brackets_in_text",
        "Put the value in <box> and type it as Record<string, X> or Array<T>.\n",
        ("<box>", "Record<string, X>", "Array<T>"),
    ),
    Construct(
        "angle_bracket_line_before_table",
        # A placeholder alone on a line used to open a raw-HTML block that
        # swallowed the table below it.
        "<box>\n| a | b |\n| - | - |\n| 1 | 2 |\n",
        ("<box>", "a", "2"),
    ),
    Construct(
        "angle_brackets_that_are_real_tag_names",
        "Placeholders: <img>, <image>, <input>, <br>, <hr>, <iframe>, <video>, <svg>.\n",
        ("<img>", "<image>", "<input>", "<iframe>", "<svg>"),
    ),
    Construct(
        "angle_brackets_unclosed_raw_blocks",
        # <pre>, <script>, <style> and <textarea> start raw-HTML blocks that run
        # until their closing tag, so an unclosed one hides everything after it.
        "<pre>\n\nMiddle paragraph.\n\n<script>\n\n<textarea>\n\n<style>\n\nLast paragraph.\n",
        ("<pre>", "Middle paragraph.", "<script>", "<textarea>", "Last paragraph."),
    ),
    Construct(
        "html_comment",
        "Before <!-- a note --> after.\n",
        ("Before", "after."),
    ),
    Construct(
        "nested_lists",
        "1. first\n   - nested bullet\n     - deeper bullet\n2. second\n   1. nested number\n",
        ("first", "deeper bullet", "nested number"),
    ),
    Construct(
        "task_list_syntax",
        "- [ ] open task\n- [x] done task\n",
        ("open task", "done task"),
    ),
    Construct(
        "links",
        "See [the docs](https://example.com/docs) and <https://example.com/auto>.\n",
        ("https://example.com/docs", "https://example.com/auto"),
    ),
    Construct(
        "image",
        "![diagram](https://example.com/diagram.png)\n",
        ("https://example.com/diagram.png",),
    ),
    Construct(
        "blockquote",
        "> quoted line\n>\n> > nested quote\n>\n> - list in quote\n",
        ("quoted line", "nested quote", "list in quote"),
    ),
    Construct(
        "horizontal_rule",
        "Above the rule.\n\n---\n\nBelow the rule.\n",
        ("Above the rule.", "Below the rule."),
    ),
    Construct(
        "headings_all_levels",
        "".join(f"{'#' * n} Level {n}\n\n" for n in range(1, 7)),
        ("Level 1", "Level 6"),
    ),
    Construct(
        "hard_line_break_and_entities",
        "line one\\\nline two &amp; &copy; 5 < 6 > 4\n",
        ("line one", "line two", "©", "5 < 6 > 4"),
    ),
]


def _join(*names: str) -> str:
    by_name = {c.name: c for c in SINGLE_CONSTRUCTS}
    return "\n\n".join(by_name[n].markdown for n in names)


# The shape of the production pages that froze the editor: a table, fenced
# code, and angle-bracket placeholders in the same document.
_REPORTED_SHAPE = (
    "angle_brackets_in_text",
    "table_pipe_in_cell",
    "code_fence_with_angle_brackets",
    "table_em_dash_cell",
    "inline_code_with_angle_brackets",
)

MIXED_CONSTRUCTS: list[Construct] = [
    Construct(
        "mixed_reported_shape",
        _join(*_REPORTED_SHAPE),
        ("<box>", "Record<string, X>", "a \\| b", "—"),
    ),
    Construct(
        "mixed_everything",
        "\n\n".join(c.markdown for c in SINGLE_CONSTRUCTS),
        ("<box>", "Record<string, X>", "def add(a, b):", "Level 6", "Below the rule."),
    ),
    Construct(
        "mixed_long_document",
        # Roughly the size of a real design doc: the full mix repeated.
        "\n\n".join(
            f"## Section {i}\n\n" + _join(*_REPORTED_SHAPE, "nested_lists", "blockquote")
            for i in range(15)
        ),
        ("Section 0", "Section 14", "<box>"),
    ),
]

ALL_CONSTRUCTS: list[Construct] = SINGLE_CONSTRUCTS + MIXED_CONSTRUCTS
