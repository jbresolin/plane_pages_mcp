"""Postgres access layer for Plane Pages.

Reads go straight to Postgres; every read is scoped to the configured workspace
and filters ``deleted_at IS NULL`` (Plane soft-deletes). Writes are performed by
``pipeline.py`` using the low-level helpers here inside a single transaction.

Ground-truth schema facts (verified against CE v1.3.1, see verify.py):
  * pages.sort_order is NOT NULL with NO column default -> we supply one.
  * pages.color is NOT NULL with no default -> '' (matches app-created rows).
  * pages.view_props / logo_props NOT NULL no default -> {"full_width": false} / {}.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

# App-level defaults for NOT-NULL-without-DB-default columns (mirrors Plane's
# Django model defaults / observed rows).
DEFAULT_SORT_ORDER = 65535.0
DEFAULT_COLOR = ""
DEFAULT_VIEW_PROPS = {"full_width": False}
DEFAULT_LOGO_PROPS: dict[str, Any] = {}

ACCESS_PUBLIC = 0
ACCESS_PRIVATE = 1

# Columns the INSERT builders below populate. verify.py cross-checks these
# against the DB's NOT-NULL-without-default columns so a schema change that adds
# a required column fails loudly instead of raising a NotNullViolation at write.
PAGES_INSERT_COLUMNS = frozenset(
    {
        "id", "created_at", "updated_at", "name", "description_html",
        "description_json", "description_binary", "description_stripped",
        "access", "color", "is_locked", "is_global", "view_props", "logo_props",
        "sort_order", "workspace_id", "owned_by_id", "created_by_id", "updated_by_id",
    }
)
PROJECT_PAGES_INSERT_COLUMNS = frozenset(
    {
        "id", "created_at", "updated_at", "page_id", "project_id",
        "workspace_id", "created_by_id", "updated_by_id",
    }
)


class NotFoundError(LookupError):
    """Raised when a workspace / project / page cannot be resolved."""


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class Database:
    def __init__(self, database_url: str, workspace_slug: str) -> None:
        self._pool = ConnectionPool(
            database_url, min_size=1, max_size=4, open=True, kwargs={"row_factory": dict_row}
        )
        self._workspace_slug = workspace_slug
        self._workspace_id: str | None = None

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn:
            yield conn

    # --- resolution -----------------------------------------------------

    @property
    def workspace_id(self) -> str:
        if self._workspace_id is None:
            self._workspace_id = self.resolve_workspace(self._workspace_slug)
        return self._workspace_id

    def resolve_workspace(self, slug: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM workspaces WHERE slug = %s AND deleted_at IS NULL",
                (slug,),
            ).fetchone()
        if not row:
            raise NotFoundError(f"workspace slug {slug!r} not found")
        return str(row["id"])

    def resolve_project(self, project_ref: str) -> dict[str, Any]:
        """Resolve a project by UUID or (case-insensitive) identifier.

        Returns {id, identifier, name}. Scoped to the configured workspace.
        """
        where = "id = %s" if _is_uuid(project_ref) else "UPPER(identifier) = UPPER(%s)"
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT id, identifier, name FROM projects
                WHERE {where} AND workspace_id = %s AND deleted_at IS NULL
                """,
                (project_ref, self.workspace_id),
            ).fetchone()
        if not row:
            raise NotFoundError(
                f"project {project_ref!r} not found in workspace {self._workspace_slug!r}"
            )
        return {"id": str(row["id"]), "identifier": row["identifier"], "name": row["name"]}

    # --- reads ----------------------------------------------------------

    def list_pages(
        self, project_id: str | None, include_archived: bool, limit: int
    ) -> list[dict[str, Any]]:
        clauses = ["p.workspace_id = %s", "p.deleted_at IS NULL"]
        params: list[Any] = [self.workspace_id]
        if project_id is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM project_pages pp "
                "WHERE pp.page_id = p.id AND pp.project_id = %s AND pp.deleted_at IS NULL)"
            )
            params.append(project_id)
        if not include_archived:
            clauses.append("p.archived_at IS NULL")
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT p.id, p.name, p.archived_at, p.updated_at, p.access,
                       {_project_identifiers_subquery()}
                FROM pages p
                WHERE {" AND ".join(clauses)}
                ORDER BY p.updated_at DESC
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [_page_summary(r) for r in rows]

    def search_pages(self, query: str, limit: int) -> list[dict[str, Any]]:
        like = f"%{query}%"
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT p.id, p.name, p.updated_at, p.description_stripped,
                       {_project_identifiers_subquery()}
                FROM pages p
                WHERE p.workspace_id = %s AND p.deleted_at IS NULL
                  AND (p.name ILIKE %s OR p.description_stripped ILIKE %s)
                ORDER BY p.updated_at DESC
                LIMIT %s
                """,
                (self.workspace_id, like, like, limit),
            ).fetchall()
        out = []
        for r in rows:
            summary = _page_summary(r)
            summary["snippet"] = _snippet(r.get("description_stripped") or "", query)
            out.append(summary)
        return out

    def read_page(self, page_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT p.id, p.name, p.description_html, p.access, p.is_locked,
                       p.archived_at, p.created_at, p.updated_at, p.owned_by_id,
                       {_project_identifiers_subquery()}
                FROM pages p
                WHERE p.id = %s AND p.workspace_id = %s AND p.deleted_at IS NULL
                """,
                (page_id, self.workspace_id),
            ).fetchone()
        return row

    def get_page_html(self, conn: psycopg.Connection, page_id: str) -> str | None:
        row = conn.execute(
            """
            SELECT description_html FROM pages
            WHERE id = %s AND workspace_id = %s AND deleted_at IS NULL
            FOR UPDATE
            """,
            (page_id, self.workspace_id),
        ).fetchone()
        return None if row is None else (row["description_html"] or "")

    # --- writes (called within a transaction by pipeline.py) ------------

    def update_page_content(
        self,
        conn: psycopg.Connection,
        *,
        page_id: str,
        html: str,
        json_doc: Any,
        binary: bytes,
        stripped: str,
        service_user_id: str,
        name: str | None,
    ) -> int:
        set_name = ", name = %s" if name is not None else ""
        params: list[Any] = [
            html,
            Jsonb(json_doc),
            psycopg.Binary(binary),
            stripped,
            service_user_id,
        ]
        if name is not None:
            params.append(name)
        params.extend([page_id, self.workspace_id])
        cur = conn.execute(
            f"""
            UPDATE pages
            SET description_html = %s,
                description_json = %s,
                description_binary = %s,
                description_stripped = %s,
                updated_by_id = %s,
                updated_at = now()
                {set_name}
            WHERE id = %s AND workspace_id = %s AND deleted_at IS NULL
            """,
            params,
        )
        return cur.rowcount

    def insert_page(
        self,
        conn: psycopg.Connection,
        *,
        name: str,
        html: str,
        json_doc: Any,
        binary: bytes,
        stripped: str,
        access: int,
        service_user_id: str,
        sort_order: float = DEFAULT_SORT_ORDER,
    ) -> str:
        page_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO pages (
                id, created_at, updated_at,
                name, description_html, description_json, description_binary,
                description_stripped, access, color, is_locked, is_global,
                view_props, logo_props, sort_order,
                workspace_id, owned_by_id, created_by_id, updated_by_id
            ) VALUES (
                %(id)s, now(), now(),
                %(name)s, %(html)s, %(json)s, %(binary)s,
                %(stripped)s, %(access)s, %(color)s, false, false,
                %(view_props)s, %(logo_props)s, %(sort_order)s,
                %(workspace_id)s, %(user)s, %(user)s, %(user)s
            )
            """,
            {
                "id": page_id,
                "name": name,
                "html": html,
                "json": Jsonb(json_doc),
                "binary": psycopg.Binary(binary),
                "stripped": stripped,
                "access": access,
                "color": DEFAULT_COLOR,
                "view_props": Jsonb(DEFAULT_VIEW_PROPS),
                "logo_props": Jsonb(DEFAULT_LOGO_PROPS),
                "sort_order": sort_order,
                "workspace_id": self.workspace_id,
                "user": service_user_id,
            },
        )
        return page_id

    def insert_project_page(
        self, conn: psycopg.Connection, *, page_id: str, project_id: str, service_user_id: str
    ) -> str:
        link_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO project_pages (
                id, created_at, updated_at,
                page_id, project_id, workspace_id, created_by_id, updated_by_id
            ) VALUES (
                %(id)s, now(), now(),
                %(page_id)s, %(project_id)s, %(workspace_id)s, %(user)s, %(user)s
            )
            """,
            {
                "id": link_id,
                "page_id": page_id,
                "project_id": project_id,
                "workspace_id": self.workspace_id,
                "user": service_user_id,
            },
        )
        return link_id

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        with self._conn() as conn:
            with conn.transaction():
                yield conn


# --- row shaping --------------------------------------------------------


def _project_identifiers_subquery() -> str:
    return (
        "(SELECT COALESCE(array_agg(pr.identifier ORDER BY pr.identifier), ARRAY[]::varchar[]) "
        "FROM project_pages pp JOIN projects pr ON pr.id = pp.project_id "
        "WHERE pp.page_id = p.id AND pp.deleted_at IS NULL) AS project_identifiers"
    )


def _page_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "project_identifiers": list(row.get("project_identifiers") or []),
        "archived": row.get("archived_at") is not None,
        "access": "private" if row.get("access") == ACCESS_PRIVATE else "public",
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _snippet(text: str, query: str, radius: int = 120) -> str:
    if not text:
        return ""
    idx = text.lower().find(query.lower())
    if idx == -1:
        return text[: radius * 2].strip()
    start = max(0, idx - radius)
    end = min(len(text), idx + len(query) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"
