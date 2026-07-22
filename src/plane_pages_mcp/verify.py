"""Phase 0 runtime verification.

Checks each subsystem independently and reports both:
  * pages — DB schema assumptions + live converter.
  * rest  — per-endpoint status against the public REST API for the configured
            (default) workspace.
Exits non-zero on any failure in an *enabled* subsystem. A disabled subsystem
is reported as skipped, never as a failure. Run after every Plane upgrade.
"""

from __future__ import annotations

import base64
import sys

import httpx
import psycopg
from psycopg.rows import dict_row

from .config import Config
from .db import PAGES_INSERT_COLUMNS, PROJECT_PAGES_INSERT_COLUMNS
from .rest import PlaneREST

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


def _verify_pages(cfg: Config, report: _Report) -> None:
    print("\n== Pages subsystem (DB + live converter) ==")
    if not cfg.pages_enabled:
        report.info("pages", "disabled (no DATABASE_URL) — skipped")
        return

    try:
        conn = psycopg.connect(cfg.database_url, row_factory=dict_row)
    except psycopg.Error as exc:
        report.check(False, "connect to DATABASE_URL", str(exc))
        return

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

        has_deleted_at = "deleted_at" in pages_cols
        report.check(
            has_deleted_at,
            "pages.deleted_at present (reads filter deleted_at IS NULL)",
            "" if has_deleted_at else "column absent — remove the filter if intended",
        )

        # Workspace enumeration (multi-workspace): list what exists.
        slugs = [
            r["slug"]
            for r in conn.execute(
                "SELECT slug FROM workspaces WHERE deleted_at IS NULL ORDER BY slug"
            ).fetchall()
        ]
        report.info("workspaces present", str(slugs))

        if cfg.workspace_slug:
            ws = conn.execute(
                "SELECT id FROM workspaces WHERE slug = %s AND deleted_at IS NULL",
                (cfg.workspace_slug,),
            ).fetchone()
            report.check(ws is not None, f"default WORKSPACE_SLUG {cfg.workspace_slug!r} resolves")
        else:
            report.info("default WORKSPACE_SLUG", "unset — callers must pass workspace explicitly")

        user = conn.execute(
            "SELECT id, email FROM users WHERE id = %s", (cfg.service_user_id,)
        ).fetchone()
        report.check(user is not None, "SERVICE_USER_ID exists in users")
        if user:
            report.info("service user", f"{user['id']} ({user['email']})")

        sd = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='pages' AND column_name='sort_order'"
        ).fetchone()
        report.info(
            "pages.sort_order db default",
            (sd["column_default"] if sd else "?") or "NONE (app computes per-workspace)",
        )

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
                report.check(
                    len(binary) > 0,
                    "description_binary decodes to non-empty bytes",
                    f"{len(binary)} bytes",
                )
    except (httpx.HTTPError, ValueError) as exc:
        report.check(False, f"POST {cfg.live_convert_url}", str(exc))


def _verify_rest(cfg: Config, report: _Report) -> None:
    print("\n== Work-items subsystem (public REST API) ==")
    if not cfg.rest_enabled:
        report.info("rest", "disabled (no PLANE_BASE_URL/PLANE_API_KEY) — skipped")
        return
    slug = cfg.workspace_slug
    if not slug:
        report.check(
            False,
            "REST verify needs a workspace",
            "set WORKSPACE_SLUG (used as the default) to probe endpoints",
        )
        return

    report.info("REST base", f"{cfg.plane_base_url}/api/v1 (workspace {slug!r})")
    rest = PlaneREST(cfg.plane_base_url, cfg.plane_api_key)
    try:
        # workspace-level endpoint first; also gives us a project id for the rest.
        code = rest.status_of("GET", f"workspaces/{slug}/projects/")
        report.check(code == 200, f"GET workspaces/{slug}/projects/", f"status {code}")

        project_id = None
        if code == 200:
            projects = rest.list_projects(slug)
            if projects:
                project_id = str(projects[0]["id"])
                report.info("probe project", f"{projects[0].get('identifier')} ({project_id})")
            else:
                report.info("projects", "none in this workspace — project endpoints skipped")

        if project_id:
            base = f"workspaces/{slug}/projects/{project_id}"
            probes = [
                ("GET", f"{base}/issues/", "list work items"),
                ("GET", f"{base}/states/", "list states"),
                ("GET", f"{base}/labels/", "list labels"),
                ("GET", f"{base}/members/", "list members"),
                ("GET", f"{base}/cycles/", "list cycles"),
                ("GET", f"{base}/modules/", "list modules"),
            ]
            for method, path, label in probes:
                c = rest.status_of(method, path)
                report.check(c == 200, f"{label} ({method} .../{path.split('/')[-2]}/)", f"status {c}")

            # get single work item, if any exist
            issues = rest.list_issues(slug, project_id)
            if issues:
                iid = str(issues[0]["id"])
                c = rest.status_of("GET", f"{base}/issues/{iid}/")
                report.check(c == 200, "get work item (GET .../issues/{id}/)", f"status {c}")
            else:
                report.info("work items", "none present — single-item GET skipped")
    finally:
        rest.close()


def verify(cfg: Config) -> int:
    report = _Report()
    print("plane_pages_mcp verify")
    print(
        f"  capabilities: pages={'on' if cfg.pages_enabled else 'off'}, "
        f"work_items={'on' if cfg.rest_enabled else 'off'}"
    )
    _verify_pages(cfg, report)
    _verify_rest(cfg, report)

    print()
    if report.ok:
        print("verify: ALL CHECKS PASSED")
        return 0
    print("verify: FAILURES PRESENT — see above")
    return 1


def main(cfg: Config) -> None:
    sys.exit(verify(cfg))
