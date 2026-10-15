"""Tests for yjs_state.rebase_onto_existing (no Plane needed).

Background for readers new to Yjs: Plane's editor stores a page as a Yjs
document, a record of every insert and delete ever made, not just the final
text. Two copies of a document are combined by merging their records. If the
two copies started from unrelated histories, the merge keeps BOTH contents.

That is what went wrong. Every write used to store a brand-new, unrelated
document. A browser that had opened the page earlier kept the old copy, and
merging the two brought the old content back next to the new.

These tests build small documents shaped like the editor's (a fragment named
"default" holding <paragraph> elements), so they run without Plane.
"""

from pycrdt import Doc, XmlElement, XmlFragment, XmlText

from plane_pages_mcp.yjs_state import FRAGMENT_NAME, rebase_onto_existing


def _doc_with_paragraphs(*texts: str) -> bytes:
    """A fresh, unrelated Yjs document, like the live converter returns."""
    doc = Doc()
    fragment = doc.get(FRAGMENT_NAME, type=XmlFragment)
    for text in texts:
        fragment.children.append(XmlElement("paragraph", {}, [XmlText(text)]))
    return doc.get_update()


def _visible_text(*updates: bytes) -> str:
    """Merge the given states, as a browser would, and return what it shows."""
    doc = Doc()
    for update in updates:
        doc.apply_update(update)
    return str(doc.get(FRAGMENT_NAME, type=XmlFragment))


def test_unrelated_documents_merge_into_duplicate_content():
    # Checks: the problem itself. Merging the old state with a fresh,
    # unrelated new state shows both the old and the new paragraph.
    # Why: this test documents why the fix is needed. If it ever starts
    # failing, Yjs has changed how merges work and the fix should be rechecked.
    old = _doc_with_paragraphs("old text")
    new = _doc_with_paragraphs("new text")
    shown = _visible_text(old, new)
    assert "old text" in shown and "new text" in shown


def test_browser_with_a_stale_copy_sees_only_the_new_content():
    # Checks: after rebasing, a browser holding the OLD state (cached from an
    # earlier visit) that merges in the stored state shows only the new text.
    # Why: this is the actual bug. A person who had viewed the page saw the
    # old content come back after an MCP update, and their browser then
    # saved that mix to the database.
    old = _doc_with_paragraphs("old text")
    stored = rebase_onto_existing(existing=old, fresh=_doc_with_paragraphs("new text"))
    assert _visible_text(old, stored) == "<paragraph>new text</paragraph>"


def test_a_browser_with_no_cached_copy_also_sees_only_the_new_content():
    # Checks: someone opening the page for the first time (nothing cached)
    # sees exactly the new content.
    # Why: the rebased state carries the old history plus its deletion, so
    # this guards against the deleted old text leaking through.
    old = _doc_with_paragraphs("old 1", "old 2")
    stored = rebase_onto_existing(existing=old, fresh=_doc_with_paragraphs("new"))
    assert _visible_text(stored) == "<paragraph>new</paragraph>"


def test_two_updates_in_a_row_leave_only_the_last_content():
    # Checks: rebasing twice (update, then update again) still shows only the
    # latest content, even to a browser that cached the very first version.
    # Why: real pages get updated many times. Each rebase must build on the
    # previous one, not only on the original.
    first = _doc_with_paragraphs("v1")
    second = rebase_onto_existing(existing=first, fresh=_doc_with_paragraphs("v2"))
    third = rebase_onto_existing(existing=second, fresh=_doc_with_paragraphs("v3"))
    assert _visible_text(first, third) == "<paragraph>v3</paragraph>"
    assert _visible_text(second, third) == "<paragraph>v3</paragraph>"


def test_page_without_any_stored_state_just_gets_the_fresh_state():
    # Checks: a page created empty in the Plane UI has no Yjs state yet
    # (description_binary is NULL), and then the fresh state is stored as is.
    # Why: there is nothing to rebase onto. This keeps the common
    # "create in UI, fill via MCP" path exactly as it worked before.
    fresh = _doc_with_paragraphs("first content")
    assert rebase_onto_existing(existing=None, fresh=fresh) == fresh
    assert rebase_onto_existing(existing=b"", fresh=fresh) == fresh
