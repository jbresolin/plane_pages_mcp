# Work Package: plane_pages_mcp

MCP server exposing read/write access to Plane CE **Pages**, which are absent
from CE's public REST API (upstream issue makeplane/plane#8986). Reads go
straight to Postgres; writes go through Plane's own internal HTML→Yjs
converter so the editor state stays consistent. Python + FastMCP, deployed as
one container inside the existing `plane-personal` Docker Compose stack.

Target Plane version: **CE v1.3.1** (`makeplane/plane` tag `v1.3.1`). All
facts below were verified against that tag's source. Re-verify on any Plane
upgrade (see `verify` command, Phase 0).

## 1. Verified facts (do not re-derive; cite paths if in doubt)

**F1 — A page's content is stored in FOUR columns** on the `Page` model
(`apps/api/plane/db/models/page.py`): `description_html` (text),
`description_json` (jsonb), `description_binary` (bytea — the Yjs
collaborative-editor state), `description_stripped` (plain text). The binary
is authoritative for the editor: writing only HTML via SQL produces edits
that are invisible in the UI and get overwritten by the live server. **All
writes must set html + json + binary + stripped together.**

**F2 — Plane's `live` service exposes an internal converter** that produces
the matching representations from HTML
(`apps/live/src/controllers/document.controller.ts`):

    POST {LIVE_BASE}/convert-document/
    body: {"description_html": "<h1>…</h1>…", "variant": "rich"}
    resp: {"description_json": {...}, "description_binary": "<base64>"}

- Pages use `variant: "rich"` (see `apps/api/plane/bgtasks/copy_s3_object.py`,
  which is also the reference consumer: it POSTs with no auth headers and
  `base64.b64decode()`s the binary before storing it).
- The endpoint has **no authentication** — helmet/compression/cors only. It
  must remain reachable ONLY inside the compose network.
- HTML must contain at least one tag (zod validation rejects plain text).

**F3 — Model relations**: `Page.projects` is M2M via `ProjectPage`
(`project` FK, `page` FK, `workspace` FK). Pages also have `workspace` FK,
`owned_by` FK (user), `parent` (self-FK, nullable), `access` (0=Public,
1=Private), `is_locked`, `archived_at` (date, nullable), `sort_order`
(float), `name` (text), `logo_props`/`view_props` (json), `is_global`.

**F4 — CE v1.3.1's public API has no pages endpoints** (`apps/api/plane/api/urls/`
contains work_item, cycle, module, project, state, label, member, intake,
etc. — no page). The internal session-auth app API does
(`apps/api/plane/app/urls/page.py`) but is out of scope here.

## 2. Phase 0 — runtime verification (build this first)

Ship a CLI subcommand `plane-pages-mcp verify` that connects to the DB and
live service and checks every assumption, exiting non-zero with a clear
report on any mismatch. Run it in CI-of-one fashion after every Plane
upgrade. Checks:

1. Tables exist (expected names `pages`, `project_pages`, `projects`,
   `workspaces` — confirm via `information_schema.tables`; Django app is
   `db` with explicit `db_table` names, so verify rather than assume).
2. Column inventory of `pages` and `project_pages` via
   `information_schema.columns`: confirm the four description columns, and
   **enumerate all NOT-NULL-without-default columns** (BaseModel audit
   columns like `created_at`, `updated_at`, `created_by_id`,
   `updated_by_id`, and possibly `deleted_at` soft-delete). The INSERT
   builders must cover every such column or fail loudly naming them.
3. If a `deleted_at` column exists, ALL read queries filter
   `deleted_at IS NULL`.
4. `POST` a minimal `<p>ping</p>` to the convert endpoint; expect 200 with
   both keys, and that the base64 decodes to non-empty bytes.
5. Resolve and print: workspace id for `WORKSPACE_SLUG`, service user id,
   `sort_order` column default.

## 3. Tool contract (MCP tools)

All tools return structured JSON. Content parameters accept markdown
(preferred; convert to HTML with `python-markdown`, extensions
`tables, fenced_code`) or raw HTML via a `format` field.

1. `list_pages(project: str|None, include_archived: bool=False, limit: int=50)`
   — project accepts a project identifier (e.g. `ENG`) or UUID; None lists
   workspace-wide. Returns id, name, project identifiers, archived flag,
   updated_at; ordered by updated_at desc.
2. `search_pages(query: str, limit: int=20)` — case-insensitive match on
   `name` and `description_stripped` (ILIKE for MVP; `websearch_to_tsquery`
   as a stretch). Returns id, name, a ±120-char snippet around the first
   match, updated_at.
3. `read_page(page_id: str, format: "markdown"|"html" = "markdown")` —
   metadata + content; markdown via `markdownify`.
4. `create_page(title: str, content: str, project: str, format: "markdown"|"html" = "markdown", access: "public"|"private" = "public")`
   — returns new page id and UI URL.
5. `update_page(page_id: str, content: str, format="markdown", title: str|None=None, mode: "replace"|"append" = "replace")`
   — `append` reads current `description_html`, concatenates, and runs the
   full write pipeline on the combined HTML (useful for log-style pages).

## 4. Write pipeline (exact, both create and update)

1. markdown → HTML if needed.
2. `POST` convert endpoint with `variant: "rich"`; on non-200, abort with the
   endpoint's error body.
3. `binary = base64.b64decode(resp["description_binary"])` → pass as
   `psycopg.Binary`.
4. `stripped = BeautifulSoup(html, "html.parser").get_text()`.
5. One transaction:
   - update: `UPDATE pages SET description_html=%s, description_json=%s,
     description_binary=%s, description_stripped=%s, updated_at=now()
     [, name=%s] WHERE id=%s [AND deleted_at IS NULL]`; require rowcount==1.
   - create: `INSERT INTO pages (...)` with `id=uuid4()`, workspace id,
     `owned_by_id=created_by_id=updated_by_id=SERVICE_USER_ID`, `access`,
     `sort_order` = column default (Phase 0), timestamps `now()`, plus every
     NOT-NULL column found in Phase 0; then `INSERT INTO project_pages`
     linking page↔project↔workspace with its own uuid + audit columns.

## 5. Configuration (env)

| Var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | — | dedicated role, see §7 |
| `LIVE_CONVERT_URL` | `http://live:3000/live/convert-document/` | confirm base path at deploy (`LIVE_BASE_PATH`) |
| `WORKSPACE_SLUG` | — | resolve to workspace id at startup |
| `SERVICE_USER_ID` | — | UUID of the user tool-created pages belong to |
| `MCP_AUTH_TOKEN` | — | required in http mode; reject all requests without matching Bearer |
| `MCP_TRANSPORT` | `http` | `http` (streamable, `0.0.0.0:8300/mcp`) or `stdio` for dev |
| `LOG_LEVEL` | `INFO` | never log page content above DEBUG |

## 6. Deployment

- `Dockerfile`: `python:3.12-slim`, non-root, healthcheck hitting a
  `/healthz` route (added alongside the MCP path).
- Compose service (to be pasted into the `plane-personal` stack; same
  network as `plane-db` and `live`; publish only the MCP port):

```yaml
  pages-mcp:
    image: <registry>/plane_pages_mcp:${PAGES_MCP_RELEASE:-latest}
    deploy: { replicas: 1, restart_policy: { condition: any } }
    ports: ["8300:8300"]
    environment:
      DATABASE_URL: ${PAGES_MCP_DATABASE_URL}
      LIVE_CONVERT_URL: http://live:3000/live/convert-document/
      WORKSPACE_SLUG: ${PAGES_MCP_WORKSPACE_SLUG}
      SERVICE_USER_ID: ${PAGES_MCP_SERVICE_USER_ID}
      MCP_AUTH_TOKEN: ${PAGES_MCP_AUTH_TOKEN}
    depends_on: [plane-db, live]
```

- External exposure (reverse proxy + tunnel) is the operator's job; the
  server itself must enforce the Bearer token so a public URL is safe.
  Note in the README: attaching this to claude.ai as a custom connector
  requires Anthropic's request-header auth (beta) or an OAuth layer —
  Claude Code (stdio or HTTP+token), n8n, and other clients work regardless.

## 7. Database role (document in README, don't auto-create)

```sql
CREATE ROLE plane_pages LOGIN PASSWORD '…';
GRANT SELECT ON pages, project_pages, projects, workspaces TO plane_pages;
GRANT INSERT, UPDATE ON pages, project_pages TO plane_pages;
-- no DELETE, no other tables
```

## 8. Guardrails and non-goals

- **Concurrency**: if a page is open in a browser, the live server's
  in-memory Yjs doc wins on its next persist (last-writer-loses). Document
  it; do not attempt Yjs merging.
- Tool writes create no `PageLog`/version-history entries — cosmetic,
  document it.
- Non-goals for MVP: delete/archive, labels, nested pages (`parent`), image
  or asset upload inside content, workspace-global page creation
  (`is_global`), any use of the internal session-auth app API.

## 9. Acceptance criteria

1. `verify` passes against the live stack and fails informatively against a
   deliberately wrong `LIVE_CONVERT_URL` and a DB missing a column.
2. `list_pages`/`search_pages`/`read_page` return a manually created seed
   page; search finds a term that exists only in the body.
3. `create_page` with markdown containing an h2, a bullet list, a table, and
   a fenced code block → page renders correctly in the Plane UI, **opens in
   the editor, survives a human edit + save, and the tool's content is still
   intact afterward** (this proves the binary is valid).
4. `update_page` in both modes is reflected in the UI after reload;
   `rowcount != 1` and convert failures produce clean tool errors, not
   partial writes.
5. HTTP mode rejects requests with missing/incorrect Bearer token.
6. README covers: env setup, DB role creation, Claude Code registration
   (stdio and HTTP), compose deployment, the concurrency + version-coupling
   caveats, and the post-upgrade `verify` ritual.

## 10. Test environment

Run against the operator's real instance using a throwaway project
(`MCP-SANDBOX`) and disposable pages; a fresh `pg_dump` exists before write
testing. No mocked-Plane test harness required for MVP; unit-test the
markdown/HTML/stripped conversions and SQL builders, integration-test the
rest against the sandbox project.

## References

- `plane-1.3.1/apps/api/plane/db/models/page.py` — Page/ProjectPage models
- `plane-1.3.1/apps/live/src/controllers/document.controller.ts` — converter
- `plane-1.3.1/apps/api/plane/bgtasks/copy_s3_object.py` — reference consumer
- `plane-1.3.1/apps/api/plane/api/urls/` — public API surface (no pages)
- github.com/makeplane/plane issue #8986 — upstream gap this tool fills
