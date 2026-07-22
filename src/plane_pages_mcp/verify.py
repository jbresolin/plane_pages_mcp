"""Phase 0 runtime verification.

Connects to the DB and the live service and checks every assumption the write
pipeline relies on, printing a report and exiting non-zero on any mismatch.
Run after every Plane upgrade.
"""

from __future__ import annotations

import base64
import sys

import httpx
import psycopg
from psycopg.rows import dict_row

from .config import Config
from .db import PAGES_INSERT_COLUMNS, PROJECT_PAGES_INSERT_COLUMNS

EXPECTED_TABLES = ["pages", "project_pages", "projects", "workspaces"]
DESCRIPTION_COLUMNS = [
    "description_html",
    "description_json",
    "description_binary",
    "description_stripped",
]


class _Report:
    def __init__(self) -> None:
        self.ok = True

    def check(self, passed: bool, label: str, detail: str = "") -> None:
        mark = "PASS" if passed else "FAIL"
        if not passed:
            self.ok = False
        line = f"  [{mark}] {label}"
        if detail:
            line += f" — {detail}"
        print(line)

    def info(self, label: str, detail: str) -> None:
        print(f"  [INFO] {label} — {detail}")


def _required_columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s AND is_nullable = 'NO' AND column_default IS NULL
        """,
        (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def _columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,),
    ).fetchall()
    return {r["column_name"] for r in rows}


def verify(cfg: Config) -> int:
    report = _Report()
    print(f"plane_pages_mcp verify — workspace={cfg.workspace_slug!r}")

    # --- database ------------------------------------------------------
    try:
        conn = psycopg.connect(cfg.database_url, row_factory=dict_row)
    except psycopg.Error as exc:
        print(f"  [FAIL] connect to DATABASE_URL — {exc}")
        return 1

    with conn:
        print("\nDatabase:")
        existing = {
            r["table_name"]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            ).fetchall()
        }
        for t in EXPECTED_TABLES:
            report.check(t in existing, f"table {t!r} exists")

        pages_cols = _columns(conn, "pages")
        for c in DESCRIPTION_COLUMNS:
            report.check(c in pages_cols, f"pages.{c} present")

        # NOT-NULL-without-default columns must all be covered by INSERT builders.
        pages_required = _required_columns(conn, "pages")
        missing_pages = pages_required - PAGES_INSERT_COLUMNS
        report.check(
            not missing_pages,
            "pages INSERT covers every required column",
            "" if not missing_pages else f"UNCOVERED: {sorted(missing_pages)}",
        )
        report.info("pages required columns", ", ".join(sorted(pages_required)))

        pp_required = _required_columns(conn, "project_pages")
        missing_pp = pp_required - PROJECT_PAGES_INSERT_COLUMNS
        report.check(
            not missing_pp,
            "project_pages INSERT covers every required column",
            "" if not missing_pp else f"UNCOVERED: {sorted(missing_pp)}",
        )

        # Soft-delete: reads must filter deleted_at IS NULL (they do — see db.py).
        has_deleted_at = "deleted_at" in pages_cols
        report.check(
            has_deleted_at,
            "pages.deleted_at present (reads filter deleted_at IS NULL)",
            "" if has_deleted_at else "column absent — remove the filter if this is intended",
        )

        # Resolutions.
        ws = conn.execute(
            "SELECT id FROM workspaces WHERE slug = %s AND deleted_at IS NULL",
            (cfg.workspace_slug,),
        ).fetchone()
        report.check(ws is not None, f"workspace slug {cfg.workspace_slug!r} resolves")
        if ws:
            report.info("workspace id", str(ws["id"]))

        user = conn.execute(
            "SELECT id, email FROM users WHERE id = %s",
            (cfg.service_user_id,),
        ).fetchone()
        report.check(user is not None, "SERVICE_USER_ID exists in users")
        if user:
            report.info("service user", f"{user['id']} ({user['email']})")

        default = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='pages' AND column_name='sort_order'"
        ).fetchone()
        report.info(
            "pages.sort_order db default",
            (default["column_default"] if default else "?")
            or "NONE (app supplies 65535)",
        )

    # --- live convert endpoint -----------------------------------------
    print("\nLive converter:")
    try:
        resp = httpx.post(
            cfg.live_convert_url,
            json={"description_html": "<p>ping</p>", "variant": "rich"},
            timeout=15.0,
        )
        report.check(
            resp.status_code == 200,
            f"POST {cfg.live_convert_url} returns 200",
            f"got {resp.status_code}: {resp.text[:200]}" if resp.status_code != 200 else "",
        )
        if resp.status_code == 200:
            data = resp.json()
            has_keys = "description_json" in data and "description_binary" in data
            report.check(has_keys, "response has description_json + description_binary")
            if has_keys:
                binary = base64.b64decode(data["description_binary"])
                report.check(len(binary) > 0, "description_binary decodes to non-empty bytes",
                             f"{len(binary)} bytes")
    except (httpx.HTTPError, ValueError) as exc:
        report.check(False, f"POST {cfg.live_convert_url}", str(exc))

    print()
    if report.ok:
        print("verify: ALL CHECKS PASSED")
        return 0
    print("verify: FAILURES PRESENT — see above")
    return 1


def main(cfg: Config) -> None:
    sys.exit(verify(cfg))
