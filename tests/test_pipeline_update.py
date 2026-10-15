"""Tests for pipeline.update_page's two safety rules (no Plane needed).

1. Don't write while the page is open in an editor: the live server holds
   its own copy then, and would overwrite ours on its next save.
2. Store the new content as a continuation of the page's existing Yjs
   history (see test_yjs_state.py), not as an unrelated new document.

The database, the live converter and the presence check are small fakes.
"""

from contextlib import contextmanager

import pytest
from pycrdt import Doc, XmlElement, XmlFragment, XmlText

from plane_pages_mcp import pipeline
from plane_pages_mcp.presence import PresenceError
from plane_pages_mcp.yjs_state import FRAGMENT_NAME

PAGE = "11111111-1111-1111-1111-111111111111"


def _state(text: str) -> bytes:
    doc = Doc()
    doc.get(FRAGMENT_NAME, type=XmlFragment).children.append(
        XmlElement("paragraph", {}, [XmlText(text)])
    )
    return doc.get_update()


def _shown(*updates: bytes) -> str:
    doc = Doc()
    for update in updates:
        doc.apply_update(update)
    return str(doc.get(FRAGMENT_NAME, type=XmlFragment))


class FakeDatabase:
    def __init__(self, html: str, binary: bytes | None) -> None:
        self.row = {"html": html, "binary": binary}
        self.writes: list[dict] = []

    @contextmanager
    def transaction(self):
        yield "conn"

    def get_page_content(self, conn, page_id):
        return dict(self.row)

    def update_page_content(self, conn, **fields):
        self.writes.append(fields)
        return 1


class FakeLive:
    """Returns a fresh, unrelated state, exactly like Plane's converter."""

    def __init__(self) -> None:
        self.state = _state("new text")

    def convert(self, html, variant="rich"):
        return {"type": "doc"}, self.state


class FakePresence:
    def __init__(self, open_: bool = False, broken: bool = False) -> None:
        self.open, self.broken = open_, broken

    def is_open(self, page_id):
        if self.broken:
            raise PresenceError("could not check whether the page is open: redis down")
        return self.open


def _update(db, presence, mode="replace", live=None):
    return pipeline.update_page(
        db, live or FakeLive(), page_id=PAGE, content="new text", fmt="markdown",
        title=None, mode=mode, service_user_id="user", presence=presence,
    )


def test_update_is_refused_while_the_page_is_open_in_an_editor():
    # Checks: if someone has the page open, update_page raises a WriteError
    # that tells the caller why, and writes nothing.
    # Why: while a page is open, the live server's in-memory copy wins. Our
    # write would be silently thrown away on its next save. Refusing is
    # honest; the caller can ask the person to close the page and retry.
    db = FakeDatabase("<p>old</p>", _state("old text"))
    with pytest.raises(pipeline.WriteError, match="open in the Plane editor"):
        _update(db, FakePresence(open_=True))
    assert db.writes == []


def test_update_is_refused_when_presence_cannot_be_checked():
    # Checks: if the "is it open?" check itself fails (e.g. Redis is down),
    # the update is refused, not written blindly.
    # Why: when we can't tell, writing anyway risks exactly the silent loss
    # this rule exists to prevent.
    db = FakeDatabase("<p>old</p>", _state("old text"))
    with pytest.raises(pipeline.WriteError, match="could not check"):
        _update(db, FakePresence(broken=True))
    assert db.writes == []


def test_stored_state_continues_the_existing_history():
    # Checks: what update_page stores, merged with the page's OLD state (as a
    # browser with a cached copy would do), shows only the new text.
    # Why: this is the end-to-end promise of the rebase, checked through the
    # real update_page code path rather than the helper alone.
    old = _state("old text")
    db = FakeDatabase("<p>old</p>", old)
    _update(db, FakePresence())
    stored = db.writes[0]["binary"]
    assert _shown(old, stored) == "<paragraph>new text</paragraph>"


def test_update_works_without_a_presence_checker():
    # Checks: with no presence checker configured (presence=None), the update
    # still goes through and is still rebased.
    # Why: Redis is optional (stdio mode needs none). Then the open-page
    # check is skipped, a warning is logged at startup, and the rebase alone
    # still protects people whose browsers cached an older copy.
    old = _state("old text")
    db = FakeDatabase("<p>old</p>", old)
    _update(db, presence=None)
    assert _shown(old, db.writes[0]["binary"]) == "<paragraph>new text</paragraph>"


def test_page_created_empty_in_the_ui_stores_the_fresh_state():
    # Checks: a page with no Yjs state yet (blank page made in the Plane UI)
    # gets the converter's state as is.
    # Why: that's the one path that already worked in production; it must
    # stay identical.
    db, live = FakeDatabase("<p></p>", None), FakeLive()
    _update(db, FakePresence(), live=live)
    assert db.writes[0]["binary"] == live.state
