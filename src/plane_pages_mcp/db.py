"""Postgres access layer for Plane Pages.

Reads go straight to Postgres; every read is scoped to a workspace (passed per
call — the instance is multi-workspace) and filters ``deleted_at IS NULL``
(Plane soft-deletes). Writes are performed by ``pipeline.py`` using the
low-level helpers here inside a single transaction.

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
ISSUE_RELATIONS_INSERT_COLUMNS = frozenset(
    {
        "id", "created_at", "updated_at", "relation_type", "issue_id",
        "related_issue_id", "project_id", "workspace_id",
        "created_by_id", "updated_by_id",
    }
)


class DuplicateRelationError(RuntimeError):
    """A relation already exists between these two work items in this direction."""


class NotFoundError(LookupError):
    """Raised when a workspace / project / page cannot be resolved."""


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


class Database:
    def __init__(self, database_url: str) -> None:
        self._pool = ConnectionPool(
            database_url, min_size=1, max_size=4, open=True, kwargs={"row_factory": dict_row}
        )
        # slug -> workspace uuid, populated on demand (any call may name a new one).
        self._workspace_ids: dict[str, str] = {}

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn:
            yield conn

    # --- resolution -----------------------------------------------------

    def resolve_workspace(self, slug: str) -> str:
        """Slug -> workspace uuid, cached. Unknown slug lists the ones that exist."""
        if slug in self._workspace_ids:
            return self._workspace_ids[slug]
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM workspaces WHERE slug = %s AND deleted_at IS NULL",
                (slug,),
            ).fetchone()
            if not row:
                existing = [
                    r["slug"]
                    for r in conn.execute(
                        "SELECT slug FROM workspaces WHERE deleted_at IS NULL ORDER BY slug"
                    ).fetchall()
                ]
                raise NotFoundError(
                    f"workspace {slug!r} not found; existing workspaces: {existing}"
                )
        ws_id = str(row["id"])
        self._workspace_ids[slug] = ws_id
        return ws_id

    def resolve_project(self, workspace_id: str, project_ref: str) -> dict[str, Any]:
        """Resolve a project by UUID or (case-insensitive) identifier.

        Returns {id, identifier, name}. Scoped to the given workspace.
        """
        where = "id = %s" if _is_uuid(project_ref) else "UPPER(identifier) = UPPER(%s)"
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT id, identifier, name FROM projects
                WHERE {where} AND workspace_id = %s AND deleted_at IS NULL
                """,
                (project_ref, workspace_id),
            ).fetchone()
        if not row:
            raise NotFoundError(
                f"project {project_ref!r} not found in the target workspace"
            )
        return {"id": str(row["id"]), "identifier": row["identifier"], "name": row["name"]}

    # --- reads ----------------------------------------------------------

    def list_pages(
        self, workspace_id: str, project_id: str | None, include_archived: bool, limit: int
    ) -> list[dict[str, Any]]:
        clauses = ["p.workspace_id = %s", "p.deleted_at IS NULL"]
        params: list[Any] = [workspace_id]
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

    def search_pages(self, workspace_id: str, query: str, limit: int) -> list[dict[str, Any]]:
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
                (workspace_id, like, like, limit),
            ).fetchall()
        out = []
        for r in rows:
            summary = _page_summary(r)
            summary["snippet"] = _snippet(r.get("description_stripped") or "", query)
            out.append(summary)
        return out

    def read_page(self, page_id: str) -> dict[str, Any] | None:
        # page_id is globally unique, so no workspace filter is needed; we return
        # the owning workspace's slug so the caller can tell where it came from.
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT p.id, p.name, p.description_html, p.access, p.is_locked,
                       p.archived_at, p.created_at, p.updated_at, p.owned_by_id,
                       w.slug AS workspace_slug,
                       {_project_identifiers_subquery()}
                FROM pages p
                JOIN workspaces w ON w.id = p.workspace_id
                WHERE p.id = %s AND p.deleted_at IS NULL
                """,
                (page_id,),
            ).fetchone()
        return row

    def get_page_html(self, conn: psycopg.Connection, page_id: str) -> str | None:
        # No workspace filter: update_page targets a globally-unique page id and
        # inherits that page's workspace.
        row = conn.execute(
            """
            SELECT description_html FROM pages
            WHERE id = %s AND deleted_at IS NULL
            FOR UPDATE
            """,
            (page_id,),
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
        params.append(page_id)
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
            WHERE id = %s AND deleted_at IS NULL
            """,
            params,
        )
        return cur.rowcount

    def next_sort_order(self, conn: psycopg.Connection, workspace_id: str) -> float:
        """Per-workspace: max(sort_order)+65535, or 65535 when the workspace is empty."""
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) AS m FROM pages "
            "WHERE workspace_id = %s AND deleted_at IS NULL",
            (workspace_id,),
        ).fetchone()
        return float(row["m"]) + DEFAULT_SORT_ORDER

    def insert_page(
        self,
        conn: psycopg.Connection,
        *,
        workspace_id: str,
        name: str,
        html: str,
        json_doc: Any,
        binary: bytes,
        stripped: str,
        access: int,
        service_user_id: str,
        sort_order: float | None = None,
    ) -> str:
        if sort_order is None:
            sort_order = self.next_sort_order(conn, workspace_id)
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
                "workspace_id": workspace_id,
                "user": service_user_id,
            },
        )
        return page_id

    def insert_project_page(
        self,
        conn: psycopg.Connection,
        *,
        workspace_id: str,
        page_id: str,
        project_id: str,
        service_user_id: str,
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
                "workspace_id": workspace_id,
                "user": service_user_id,
            },
        )
        return link_id

    # --- work-item relations (issue_relations) --------------------------
    #
    # Work items are otherwise a pure-REST subsystem, but relations are absent
    # from CE's public REST API (like pages), so they go straight to Postgres.
    # Gated on DATABASE_URL being set.

    def resolve_issue(self, workspace_id: str, project_id: str, item_ref: str) -> dict[str, Any]:
        """Resolve an issue by UUID or sequence ref (e.g. 'TEST-42' or '42').

        Returns {id, sequence_id}. Scoped to the given project/workspace.
        """
        if _is_uuid(item_ref):
            where, param = "id = %s", item_ref
        else:
            tail = str(item_ref).rsplit("-", 1)[-1]
            if not tail.isdigit():
                raise NotFoundError(
                    f"{item_ref!r} is neither a UUID nor a sequence ref like TEST-42"
                )
            where, param = "sequence_id = %s", int(tail)
        with self._conn() as conn:
            row = conn.execute(
                f"""
                SELECT id, sequence_id FROM issues
                WHERE {where} AND project_id = %s AND workspace_id = %s
                  AND deleted_at IS NULL
                """,
                (param, project_id, workspace_id),
            ).fetchone()
        if not row:
            raise NotFoundError(f"work item {item_ref!r} not found in the target project")
        return {"id": str(row["id"]), "sequence_id": row["sequence_id"]}

    def issue_ref(self, issue_id: str) -> str | None:
        """issue uuid -> 'TEST-42' (identifier + sequence), or None if missing."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT pr.identifier, i.sequence_id
                FROM issues i JOIN projects pr ON pr.id = i.project_id
                WHERE i.id = %s AND i.deleted_at IS NULL
                """,
                (issue_id,),
            ).fetchone()
        return f"{row['identifier']}-{row['sequence_id']}" if row else None

    def insert_issue_relation(
        self,
        conn: psycopg.Connection,
        *,
        workspace_id: str,
        project_id: str,
        issue_id: str,
        related_issue_id: str,
        relation_type: str,
        service_user_id: str,
    ) -> str:
        rel_id = str(uuid.uuid4())
        try:
            conn.execute(
                """
                INSERT INTO issue_relations (
                    id, created_at, updated_at,
                    relation_type, issue_id, related_issue_id,
                    project_id, workspace_id, created_by_id, updated_by_id
                ) VALUES (
                    %(id)s, now(), now(),
                    %(type)s, %(issue)s, %(related)s,
                    %(project)s, %(workspace)s, %(user)s, %(user)s
                )
                """,
                {
                    "id": rel_id,
                    "type": relation_type,
                    "issue": issue_id,
                    "related": related_issue_id,
                    "project": project_id,
                    "workspace": workspace_id,
                    "user": service_user_id,
                },
            )
        except psycopg.errors.UniqueViolation as exc:
            # Unique on (issue_id, related_issue_id) where deleted_at IS NULL.
            raise DuplicateRelationError(
                "a relation already exists between these work items in this direction"
            ) from exc
        return rel_id

    def delete_issue_relation(
        self,
        conn: psycopg.Connection,
        *,
        issue_id: str,
        related_issue_id: str,
        relation_type: str,
    ) -> int:
        """Hard-delete the matching active relation row; returns rowcount."""
        cur = conn.execute(
            """
            DELETE FROM issue_relations
            WHERE issue_id = %s AND related_issue_id = %s
              AND relation_type = %s AND deleted_at IS NULL
            """,
            (issue_id, related_issue_id, relation_type),
        )
        return cur.rowcount

    def read_issue_relations(self, issue_id: str) -> list[dict[str, Any]]:
        """All active relations touching this issue, in either stored direction."""
        with self._conn() as conn:
            return conn.execute(
                """
                SELECT issue_id, related_issue_id, relation_type
                FROM issue_relations
                WHERE (issue_id = %s OR related_issue_id = %s)
                  AND deleted_at IS NULL
                """,
                (issue_id, issue_id),
            ).fetchall()

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
