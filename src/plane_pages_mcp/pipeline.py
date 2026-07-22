"""The write pipeline shared by create_page and update_page.

Every write sets html + json + binary + stripped together (workpackage F1): the
Yjs binary is authoritative for the editor, so an HTML-only write would be
invisible in the UI and get overwritten by the live server.
"""

from __future__ import annotations

from typing import Any

from . import convert
from .db import ACCESS_PRIVATE, ACCESS_PUBLIC, Database, NotFoundError
from .live import LiveConverter


class WriteError(RuntimeError):
    """Raised when a write cannot be completed cleanly (no partial writes)."""


_ACCESS = {"public": ACCESS_PUBLIC, "private": ACCESS_PRIVATE}


def _render(content: str, fmt: str, live: LiveConverter) -> tuple[str, Any, bytes, str]:
    """content -> (html, json_doc, binary, stripped) via markdown + live convert."""
    html = convert.to_html(content, fmt)
    json_doc, binary = live.convert(html, variant="rich")
    stripped = convert.html_to_stripped(html)
    return html, json_doc, binary, stripped


def create_page(
    db: Database,
    live: LiveConverter,
    *,
    workspace_id: str,
    title: str,
    content: str,
    project: str,
    fmt: str,
    access: str,
    service_user_id: str,
) -> dict[str, Any]:
    if access not in _ACCESS:
        raise WriteError(f"access must be 'public' or 'private', got {access!r}")
    try:
        proj = db.resolve_project(workspace_id, project)
    except NotFoundError as exc:
        raise WriteError(str(exc)) from exc

    html, json_doc, binary, stripped = _render(content, fmt, live)

    with db.transaction() as conn:
        page_id = db.insert_page(
            conn,
            workspace_id=workspace_id,
            name=title,
            html=html,
            json_doc=json_doc,
            binary=binary,
            stripped=stripped,
            access=_ACCESS[access],
            service_user_id=service_user_id,
        )
        db.insert_project_page(
            conn,
            workspace_id=workspace_id,
            page_id=page_id,
            project_id=proj["id"],
            service_user_id=service_user_id,
        )

    return {
        "id": page_id,
        "title": title,
        "project": proj["identifier"],
        "access": access,
    }


def update_page(
    db: Database,
    live: LiveConverter,
    *,
    page_id: str,
    content: str,
    fmt: str,
    title: str | None,
    mode: str,
    service_user_id: str,
) -> dict[str, Any]:
    if mode not in ("replace", "append"):
        raise WriteError(f"mode must be 'replace' or 'append', got {mode!r}")

    # For append we must read the current HTML under a row lock and combine
    # before rendering, so the whole operation is one transaction.
    with db.transaction() as conn:
        current_html = db.get_page_html(conn, page_id)
        if current_html is None:
            raise WriteError(f"page {page_id} not found (or deleted)")

        new_fragment = convert.to_html(content, fmt)
        combined_html = current_html + new_fragment if mode == "append" else new_fragment

        json_doc, binary = live.convert(combined_html, variant="rich")
        stripped = convert.html_to_stripped(combined_html)

        rowcount = db.update_page_content(
            conn,
            page_id=page_id,
            html=combined_html,
            json_doc=json_doc,
            binary=binary,
            stripped=stripped,
            service_user_id=service_user_id,
            name=title,
        )
        if rowcount != 1:
            # Rolls back the transaction; nothing is persisted.
            raise WriteError(
                f"expected to update exactly 1 row, updated {rowcount} (page {page_id})"
            )

    return {"id": page_id, "mode": mode, "title_updated": title is not None}
