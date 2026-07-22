# plane_pages_mcp

An MCP server for a Plane CE instance, exposing two things a coding agent can't
otherwise reach cleanly:

1. **Pages** (DB + live converter) — Plane CE Pages are absent from the public
   REST API (upstream: makeplane/plane#8986). Reads go straight to Postgres;
   writes go through Plane's own internal `live` HTML→Yjs converter so the
   collaborative-editor state stays consistent.
2. **Work items** (public REST API) — issues/projects via Plane's supported CE
   REST API (`x-api-key`), no DB access.

Both are **multi-workspace**: one instance may host several workspaces (work,
family business, personal), and every tool is workspace-scoped so content never
leaks across contexts.

Built with Python + [FastMCP](https://github.com/jlowin/fastmcp). Ships as one
container to sit inside your Plane Docker Compose stack.

> **Version coupling.** All schema/endpoint facts were verified against Plane CE
> **v1.3.1**. On any Plane upgrade, re-run `plane-pages-mcp verify` (below)
> before trusting writes — a schema change can add a required column and the
> REST surface can shift.

## Subsystems and capability degradation

The two subsystems are configured and enabled independently:

| Subsystem | Needs | Transport |
| --- | --- | --- |
| pages | `DATABASE_URL` (+ `SERVICE_USER_ID` to write) | direct Postgres + `live` converter |
| work items | `PLANE_BASE_URL` + `PLANE_API_KEY` | public REST API |

The server starts if **at least one** is configured. A missing PAT never breaks
pages; a missing DB never breaks work items. The enabled set is logged at
startup (`capabilities: pages=… work_items=…`) and reported by `verify`.

## How pages work (the four-column problem)

A page's content lives in **four** columns on `pages`: `description_html`,
`description_json`, `description_binary` (the Yjs editor state — authoritative),
and `description_stripped`. Writing only HTML produces edits that are invisible
in the editor and get clobbered by the live server. So **every write sets all
four together**, deriving json + binary from the `live` service's
`convert-document` endpoint (the same converter Plane uses internally).

Work-item descriptions have the same multi-representation issue, but the **REST
API handles it itself**: we send `description_html` and Plane fills the rest.
The pages DB-write path is never reused for work items.

## Multi-workspace rules

`WORKSPACE_SLUG` is a **default, not a fixture**. Every tool takes an optional
`workspace` argument. Resolution order: **explicit argument → `WORKSPACE_SLUG`
env → error** naming both options. Slug→UUID is cached on demand; an unknown
slug returns a clean error listing the slugs that do exist.

- **Reads/lists** (`list_pages`, `search_pages`, `list_projects`,
  `list_work_items`, …) fall back to the default when `workspace` is omitted,
  and are strictly scoped — a search from one workspace never returns another's.
- **`read_page` / `get_work_item`** take a globally-unique id, so no workspace
  is needed; `read_page` returns the owning workspace slug so you can tell where
  it came from.
- **`create_page` and `create_work_item` REQUIRE an explicit `workspace`** — no
  default fallback, so a page/issue can't silently land in the wrong workspace.
- **`update_page` / `update_work_item`** inherit the target's workspace from its
  id; no `workspace` argument.

`SERVICE_USER_ID` is a **single** configured user used for every workspace
(pages are attributed to the operator). Plane also creates a per-workspace bot
user (`bot_user_<workspace-uuid>@plane.so`), but we deliberately use one
configured id — simpler, and attribution stays accurate.

## Tools

### Pages (DB) — enabled by `DATABASE_URL`

| Tool | Purpose |
| --- | --- |
| `list_pages(workspace?, project?, include_archived=False, limit=50)` | List pages (newest first), scoped to `workspace`. `project` = identifier (e.g. `ENG`) or UUID. |
| `search_pages(query, workspace?, limit=20)` | Case-insensitive name+body match within one workspace; ±120-char snippet. |
| `read_page(page_id, format="markdown")` | Metadata + content (markdown/html) + owning workspace slug. |
| `create_page(title, content, project, workspace, format="markdown", access="public")` | Create a page — **`workspace` required**; returns id + UI URL. |
| `update_page(page_id, content, format="markdown", title?, mode="replace")` | Replace or `append` body (inherits the page's workspace). |

### Work items (REST) — enabled by `PLANE_BASE_URL` + `PLANE_API_KEY`

| Tool | Purpose |
| --- | --- |
| `list_projects(workspace?)` | id, name, identifier, description. |
| `list_work_items(project, workspace?, state_name?, assignee?, limit=50)` | Issues with sequence id (`TEST-42`), state/assignee **names** (not UUIDs), priority. |
| `get_work_item(item, project, workspace?)` | Full detail; `item` = `TEST-42` or UUID; description as markdown; `parent` (as a sequence ref) and `sub_issues_count`. |
| `create_work_item(project, title, workspace, description?, state_name?, priority?, assignees?, labels?, parent?)` | **`workspace` required**; human names resolved to UUIDs. `parent` (a `TEST-42`/UUID ref) creates a **sub-work-item**. |
| `update_work_item(item, project, workspace?, …, parent?)` | Partial update; only supplied fields are sent. `parent` re-parents into a sub-work-item. |
| `list_states(project, workspace?)` / `list_labels(project, workspace?)` | Discover valid state/label names. |

Content accepts **markdown** (default; CommonMark via `markdown-it-py`, with
tables + strikethrough — a bullet list may follow a paragraph line without a
blank line, matching GitHub) or raw **HTML** via `format="html"`. Priorities:
`urgent`/`high`/`medium`/`low`/`none`. Unknown state/label/assignee/priority/
parent names return an error listing the valid options.

## Configuration

Set via environment (see [`.env.example`](.env.example)):

| Var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | — | pages only; dedicated role, see below |
| `SERVICE_USER_ID` | — | pages only; single user that owns tool-created pages |
| `LIVE_CONVERT_URL` | `http://live:3000/live/convert-document/` | pages only; confirm base path at deploy |
| `PLANE_WEB_URL` | `http://localhost` | pages only; builds the UI URL from `create_page` |
| `PLANE_BASE_URL` | — | **work items**; instance site root, client appends `/api/v1` |
| `PLANE_API_KEY` | — | **work items**; PAT, sent as header `x-api-key` |
| `WORKSPACE_SLUG` | — | **default** workspace (not a fixture); may be unset |
| `MCP_AUTH_TOKEN` | — | **required in http mode**; requests without a matching Bearer are rejected |
| `MCP_TRANSPORT` | `http` | `http` (streamable, `0.0.0.0:8300/mcp`) or `stdio` for dev |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8300` / `/mcp` | http bind |
| `LOG_LEVEL` | `INFO` | page content is never logged above `DEBUG` |

`PLANE_BASE_URL` (REST instance root) is unrelated to `LIVE_CONVERT_URL` (the
internal, unauthenticated pages converter).

### Rate limiting

The instance sets `API_KEY_RATE_LIMIT` (default `60/minute`). A chained work-item
sequence (list projects → list states → resolve assignees → create) can brush
against it. A `429` surfaces as a clear rate-limit error naming the limit; the
server does **not** silently retry in a loop.

## Database role (pages)

Create a dedicated, least-privilege role (do **not** run as the Plane superuser):

```sql
CREATE ROLE plane_pages LOGIN PASSWORD '…';
GRANT SELECT ON pages, project_pages, projects, workspaces TO plane_pages;
GRANT INSERT, UPDATE ON pages, project_pages TO plane_pages;
-- no DELETE, no other tables
```

The `users` table is read by `verify` (to confirm `SERVICE_USER_ID`); grant
`SELECT ON users` too if you want the verify user-check to pass under this role.

## Post-upgrade ritual: `verify`

```bash
plane-pages-mcp verify
```

Reports **both subsystems independently** and exits non-zero on any failure in
an *enabled* subsystem (a disabled one is skipped, never failed):

**Pages** — expected tables exist; the four description columns exist; **every
NOT-NULL-without-default column on `pages`/`project_pages` is covered by the
INSERT builders** (a new required column fails loudly here, not mid-write);
`pages.deleted_at` exists; the `<p>ping</p>` convert round-trip returns 200 with
a non-empty decoded binary; enumerates workspaces and confirms the default slug
+ service user.

**Work items** — for the default workspace, probes each REST endpoint and prints
its status code: `projects/`, then per first project `issues/`, `states/`,
`labels/`, `members/`, `cycles/`, `modules/`, and a single-issue GET. Anything
not returning 200 should be dropped from the tool set (and noted here) rather
than shipped broken.

It should **fail informatively** against a wrong `LIVE_CONVERT_URL`, a DB missing
a column, or a bad/absent PAT.

## Running

### Local dev (stdio)

```bash
uv venv && uv pip install -e .
cp .env.example .env   # edit it; from the host use localhost URLs
set -a; . .env; set +a
plane-pages-mcp verify
plane-pages-mcp serve   # MCP_TRANSPORT=stdio
```

### Register with Claude Code

**stdio** (local):

```bash
claude mcp add plane -- \
  env MCP_TRANSPORT=stdio \
      DATABASE_URL='postgresql://plane_pages:…@localhost:5432/plane' \
      SERVICE_USER_ID='<uuid>' \
      LIVE_CONVERT_URL='http://localhost:3300/live/convert-document/' \
      PLANE_BASE_URL='http://localhost' \
      PLANE_API_KEY='<pat>' \
      WORKSPACE_SLUG=test \
  plane-pages-mcp serve
```

**HTTP** (server running with a Bearer token):

```bash
claude mcp add --transport http plane http://localhost:8300/mcp \
  --header "Authorization: Bearer $MCP_AUTH_TOKEN"
```

The server enforces the token itself, so a public URL is safe. `n8n` and other
HTTP clients work the same way. Attaching to **claude.ai** as a custom connector
additionally needs Anthropic's request-header auth (beta) or an OAuth layer.

## Deployment (Docker Compose)

Build and add this service to your Plane stack (same network as `plane-db` and
`live`; publish only the MCP port):

```yaml
  pages-mcp:
    image: <registry>/plane_pages_mcp:${PAGES_MCP_RELEASE:-latest}
    deploy: { replicas: 1, restart_policy: { condition: any } }
    ports: ["8300:8300"]
    environment:
      DATABASE_URL: ${PAGES_MCP_DATABASE_URL}
      LIVE_CONVERT_URL: http://live:3000/live/convert-document/
      SERVICE_USER_ID: ${PAGES_MCP_SERVICE_USER_ID}
      PLANE_BASE_URL: ${PAGES_MCP_PLANE_BASE_URL}
      PLANE_API_KEY: ${PAGES_MCP_API_KEY}
      WORKSPACE_SLUG: ${PAGES_MCP_WORKSPACE_SLUG}
      MCP_AUTH_TOKEN: ${PAGES_MCP_AUTH_TOKEN}
      PLANE_WEB_URL: ${PAGES_MCP_WEB_URL:-http://localhost}
    depends_on: [plane-db, live]
```

External exposure (reverse proxy + tunnel) is the operator's job. The
`convert-document` endpoint has **no auth** and must remain reachable only
inside the compose network.

## Caveats & non-goals

- **Concurrency (last-writer-loses).** If a page is open in a browser, the live
  server's in-memory Yjs doc wins on its next persist and can overwrite a tool
  write. No Yjs merging is attempted — write to pages that aren't being edited.
- **No page version history.** Tool page-writes create no `PageLog`/
  `page_versions` entries. Cosmetic, but be aware.
- **Non-goals:** page delete/archive, nested pages (`parent`), image/asset upload
  in content, workspace-global page creation (`is_global`), the internal
  session-auth app API; and for work items: delete, cycle/module membership,
  attachments, comments, intake. (Sub-work-items **are** supported via `parent`.)

## Development

```bash
uv pip install -e '.[dev]'
pytest   # conversions, row shaping, column coverage, config capabilities,
         # REST helpers, work-item shaping (fake REST), auth middleware
```

Integration testing runs against a real Plane instance using a throwaway project
and disposable pages/items (take a `pg_dump` first for pages).
