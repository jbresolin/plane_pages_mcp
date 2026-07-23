"""Phase 0 runtime verification.

Checks each subsystem independently and reports both:
  * pages — DB schema assumptions + live converter.
  * rest  — per-endpoint status against the public REST API for the configured
            (default) workspace.
Exits non-zero on any failure in an *enabled* subsystem. A disabled subsystem
is reported as skipped, never as a failure. Run after every Plane upgrade.
"""

from __future__ import annotations

import asyncio
import base64
import sys

import httpx
import psycopg
from psycopg.rows import dict_row

from .config import Config
from .db import (
    ISSUE_RELATIONS_INSERT_COLUMNS,
    PAGES_INSERT_COLUMNS,
    PROJECT_PAGES_INSERT_COLUMNS,
)
from .rest import FORBIDDEN_HINT, PlaneREST

EXPECTED_TABLES = ["pages", "project_pages", "projects", "workspaces"]
# Every table a tool path reads (db.py) plus `users` (read by verify to validate
# SERVICE_USER_ID). `issues`/`issue_relations` back the work-item relation tools
# (gated on the DB subsystem). Writes touch pages/project_pages/issue_relations
# too, but verify stays read-only, so INSERT/UPDATE/DELETE grants are documented
# rather than exercised.
READ_TABLES = [
    "pages", "project_pages", "projects", "workspaces", "users",
    "issues", "issue_relations",
]
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


def _status_detail(code: int) -> str:
    """Format a REST probe status; add the membership hint on a 403."""
    if code == 200:
        return ""
    if code == 403:
        return f"status 403 — {FORBIDDEN_HINT}"
    return f"status {code}"


_FAILED = object()  # sentinel: distinguishes "probe raised" from "returned None"


def _guard(report: _Report, label: str, fn, *, hint: str = "", report_success: bool = False):
    """Run a probe; record any exception as a FAIL and keep going.

    verify must always reach its summary line — a failing check (bad grant,
    403, connection refused, timeout) is data, not a reason to crash. Returns
    the probe's value on success, or the ``_FAILED`` sentinel if it raised.
    """
    try:
        value = fn()
    except Exception as exc:  # noqa: BLE001 - deliberately broad; verify never raises
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        if hint:
            detail = f"{detail} — {hint}"
        report.check(False, label, detail)
        return _FAILED
    if report_success:
        report.check(True, label)
    return value


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

    # autocommit so a permission error on one table (e.g. a missing GRANT) is
    # reported and does not poison the transaction for the following reads.
    conn.autocommit = True
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

        if "issue_relations" in existing:
            ir_required = _required_columns(conn, "issue_relations")
            missing_ir = ir_required - ISSUE_RELATIONS_INSERT_COLUMNS
            report.check(
                not missing_ir,
                "issue_relations INSERT covers every required column",
                "" if not missing_ir else f"UNCOVERED: {sorted(missing_ir)}",
            )

        has_deleted_at = "deleted_at" in pages_cols
        report.check(
            has_deleted_at,
            "pages.deleted_at present (reads filter deleted_at IS NULL)",
            "" if has_deleted_at else "column absent — remove the filter if intended",
        )

        # Exercise a SELECT against every table a tool path (or verify) reads,
        # so a missing GRANT surfaces here rather than at runtime. These are the
        # tables touched by db.py's reads/writes plus `users` (read by verify).
        for table in READ_TABLES:
            _guard(
                report,
                f"SELECT grant on {table!r}",
                lambda t=table: conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone(),
                hint=f"grant with: GRANT SELECT ON {table} TO <role>",
                report_success=True,
            )

        # Workspace enumeration (multi-workspace): list what exists.
        slugs_rows = _guard(
            report,
            "read workspaces",
            lambda: conn.execute(
                "SELECT slug FROM workspaces WHERE deleted_at IS NULL ORDER BY slug"
            ).fetchall(),
        )
        if slugs_rows is not _FAILED:
            report.info("workspaces present", str([r["slug"] for r in slugs_rows]))

        slug_label = f"default WORKSPACE_SLUG {cfg.workspace_slug!r} resolves"
        if cfg.workspace_slug:
            ws = _guard(
                report,
                slug_label,
                lambda: conn.execute(
                    "SELECT id FROM workspaces WHERE slug = %s AND deleted_at IS NULL",
                    (cfg.workspace_slug,),
                ).fetchone(),
            )
            if ws is not _FAILED:  # query ran; None means no matching row
                report.check(ws is not None, slug_label,
                             "" if ws is not None else "no matching workspace row")
        else:
            report.info("default WORKSPACE_SLUG", "unset — callers must pass workspace explicitly")

        user = _guard(
            report,
            "SERVICE_USER_ID exists in users",
            lambda: conn.execute(
                "SELECT id, email FROM users WHERE id = %s", (cfg.service_user_id,)
            ).fetchone(),
            hint="grant with: GRANT SELECT ON users TO <role>",
        )
        if user is not _FAILED:
            report.check(user is not None, "SERVICE_USER_ID exists in users",
                         "" if user is not None else "no matching user row")
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
        # Every probe is status-only (never raises) or wrapped in _guard, so a
        # 403/refused/timeout is recorded and the run continues to the summary.
        code = rest.status_of("GET", f"workspaces/{slug}/projects/")
        report.check(code == 200, f"GET workspaces/{slug}/projects/", _status_detail(code))

        project_id = None
        if code == 200:
            projects = _guard(report, "list projects", lambda: rest.list_projects(slug))
            if projects is _FAILED:
                projects = None
            if projects:
                project_id = str(projects[0]["id"])
                report.info("probe project", f"{projects[0].get('identifier')} ({project_id})")
            elif projects is not None:
                report.info("projects", "none in this workspace — project endpoints skipped")

        if project_id:
            base = f"workspaces/{slug}/projects/{project_id}"
            probes = [
                (f"{base}/issues/", "list work items"),
                (f"{base}/states/", "list states"),
                (f"{base}/labels/", "list labels"),
                (f"{base}/members/", "list members"),
                (f"{base}/cycles/", "list cycles"),
                (f"{base}/modules/", "list modules"),
            ]
            for path, label in probes:
                c = rest.status_of("GET", path)
                report.check(c == 200, f"{label} (GET .../{path.split('/')[-2]}/)", _status_detail(c))

            # Single-item GET, only if listing issues succeeds and any exist.
            issues = _guard(report, "list issues (for single-item probe)",
                            lambda: rest.list_issues(slug, project_id))
            if issues is _FAILED:
                pass  # already recorded a FAIL with the endpoint's message
            elif issues:
                iid = str(issues[0]["id"])
                c = rest.status_of("GET", f"{base}/issues/{iid}/")
                report.check(c == 200, "get work item (GET .../issues/{id}/)", _status_detail(c))
            else:
                report.info("work items", "none present — single-item GET skipped")
    finally:
        rest.close()


def _verify_auth(cfg: Config, report: _Report) -> None:
    print("\n== Auth subsystem (GitHub OAuth, http transport) ==")
    if not cfg.public_base_url:
        report.info("auth", "no PUBLIC_BASE_URL — OAuth not configured (stdio-only) — skipped")
        return

    # Allowlist must be non-empty — an empty allowlist with OAuth would let any
    # GitHub account through (fail-open), the exact thing this gate prevents.
    report.check(
        bool(cfg.allowed_github_logins),
        "ALLOWED_GITHUB_LOGINS is non-empty",
        "" if cfg.allowed_github_logins else "empty — every GitHub account would be refused/allowed",
    )
    if cfg.allowed_github_logins:
        report.info("allowed logins", str(sorted(cfg.allowed_github_logins)))

    base = cfg.public_base_url
    # Discovery documents (served by the running server; must be tunnel-reachable).
    as_url = f"{base}/.well-known/oauth-authorization-server"
    data = _guard(report, f"GET {as_url}", lambda: _get_json(as_url))
    if data not in (None, _FAILED) and isinstance(data, dict):
        for key in ("authorization_endpoint", "token_endpoint"):
            report.check(key in data, f"authorization-server metadata names {key}")

    pr_url = f"{base}/.well-known/oauth-protected-resource{cfg.mcp_path}"
    prd = _guard(report, f"GET {pr_url}", lambda: _get_json(pr_url))
    if prd not in (None, _FAILED) and isinstance(prd, dict):
        report.check("authorization_servers" in prd,
                     "protected-resource metadata names authorization_servers")

    # Storage backend: reachable + encrypted at rest.
    if cfg.redis_url and cfg.storage_encryption_key:
        _guard(report, "OAuth storage (Redis) reachable + encryption active",
               lambda: _check_storage(cfg), report_success=True)
    else:
        report.check(False, "OAuth storage configured",
                     "REDIS_URL and STORAGE_ENCRYPTION_KEY are required in http mode")


def _get_json(url: str) -> dict:
    resp = httpx.get(url, timeout=10.0)
    if resp.status_code != 200:
        raise RuntimeError(f"status {resp.status_code}")
    return resp.json()


def _check_storage(cfg: Config) -> None:
    """Round-trip a value through the encrypted Redis wrapper; prove ciphertext."""
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    from .auth import build_storage

    async def _run() -> None:
        store = build_storage(cfg)
        assert isinstance(store, FernetEncryptionWrapper), "storage is not Fernet-encrypted"
        await store.put("probe", {"v": "ping"}, collection="verify")
        got = await store.get("probe", collection="verify")
        assert got == {"v": "ping"}, f"storage round-trip mismatch: {got!r}"
        await store.delete("probe", collection="verify")

    asyncio.run(_run())


def verify(cfg: Config) -> int:
    report = _Report()
    print("plane_pages_mcp verify")
    print(
        f"  capabilities: pages={'on' if cfg.pages_enabled else 'off'}, "
        f"work_items={'on' if cfg.rest_enabled else 'off'}, "
        f"auth={'on' if cfg.public_base_url else 'off'}"
    )
    _verify_pages(cfg, report)
    _verify_rest(cfg, report)
    _verify_auth(cfg, report)

    print()
    if report.ok:
        print("verify: ALL CHECKS PASSED")
        return 0
    print("verify: FAILURES PRESENT — see above")
    return 1


def main(cfg: Config) -> None:
    sys.exit(verify(cfg))
