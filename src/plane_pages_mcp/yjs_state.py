"""Build the Yjs state we store for a page update.

Plane's editor keeps a page as a Yjs document (``pages.description_binary``):
a history of inserts and deletes, not just the final content. The live
converter gives us a *fresh* document for new HTML, with no relation to the
page's current history.

Storing that fresh document as is was a bug. Browsers that had opened the page
before keep the old document cached (IndexedDB). On their next visit Yjs
merges the cached and stored documents, and two unrelated histories merge into
"old content + new content". The browser then saves that mix back for
everyone.

The fix is to store the new content as a *continuation* of the existing
history: start from the page's current state, delete its content, then add the
fresh content. A browser with a cached copy then receives "delete what you
have, add this", and ends up with exactly the new content.

See docs/decisions.md (2026-10-15, "Rebase page updates onto the existing Yjs
history").
"""

from __future__ import annotations

from pycrdt import Doc, XmlFragment

# The name of the shared XML fragment that Plane's editor (y-prosemirror)
# reads. Visible in every stored state and in the converter's output.
FRAGMENT_NAME = "default"


def rebase_onto_existing(existing: bytes | None, fresh: bytes) -> bytes:
    """Return ``fresh``'s content expressed as a continuation of ``existing``.

    ``existing`` is the page's stored ``description_binary`` (None or empty
    for a page created blank in the Plane UI, which has no history yet);
    ``fresh`` is the converter's state for the new content.
    """
    if not existing:
        return fresh

    doc = Doc()
    doc.apply_update(existing)
    fragment = doc.get(FRAGMENT_NAME, type=XmlFragment)
    # Deleting inside the same history records *which* items went away, so
    # any copy that still has them will drop them when it merges.
    with doc.transaction():
        count = len(fragment.children)
        if count:
            del fragment.children[0:count]
    # The fresh content's items are new to this history, so merging them in
    # simply adds them. Nothing else is left to compete with them.
    doc.apply_update(fresh)
    return doc.get_update()
