# plane_pages_mcp

An MCP server for a Plane CE instance, exposing two things a coding agent can't
otherwise reach cleanly:

1. **Pages** (DB + live converter) — Plane CE Pages are absent from the public
   REST API (upstream: makeplane/plane#8986). Reads go straight to Postgres;
   writes go through Plane's own internal `live` HTML→Yjs converter so the
   collaborative-editor state stays consistent.
2. **Work items** (public REST API) — issues/projects via Plane's supported CE
   REST API (`x-api-key`). The one exception is **relations** (`blocks`,
   `relates_to`, …), which CE omits from the API and which therefore go to
   Postgres like pages — so they need the DB subsystem too.

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
| `link_work_items(item, related_item, relation_type, project, workspace?)` | Create a relation (needs the **DB** subsystem — relations aren't in the REST API). |
| `unlink_work_items(item, related_item, relation_type, project, workspace?)` | Remove a relation. |

**Work-item relations** are absent from CE's public REST API (like pages), so
`link_work_items` / `unlink_work_items` write directly to Postgres and are gated
on `DATABASE_URL` (not on the REST subsystem). `get_work_item` includes a
`relations` list whenever the DB subsystem is enabled. `relation_type` is
directional — "`item <relation_type> related_item`" — and accepts: `blocks`,
`blocked_by`, `relates_to`, `duplicate`, `start_before`, `start_after`,
`finish_before`, `finish_after`, `implements`, `implemented_by` (Plane stores the
canonical direction; the inverse label is shown on the other item). Parent/child
hierarchy is separate — use `parent` on create/update for sub-work-items.

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
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | — | **http auth**; per-environment GitHub OAuth app |
| `PUBLIC_BASE_URL` | — | **http auth**; public origin, e.g. `https://planemcp.<domain>` (no `/mcp`) |
| `ALLOWED_GITHUB_LOGINS` | — | **http auth**; comma-separated GitHub logins allowed (case-insensitive); empty is rejected |
| `JWT_SIGNING_KEY` | — | **http auth**; long secret, stable across restarts (≥12 chars) |
| `REDIS_URL` | — | **http auth**; OAuth state store, e.g. `redis://plane-redis:6379/0` |
| `STORAGE_ENCRYPTION_KEY` | — | **http auth**; encrypts OAuth state at rest (Fernet) |
| `TOKEN_EXPIRY_SECONDS` | `604800` | http auth; FastMCP access-token lifetime (7 days) |
| `MCP_TRANSPORT` | `http` | `http` (streamable `0.0.0.0:8300/mcp`, GitHub OAuth) or `stdio` (on-box, auth-free) |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8300` / `/mcp` | http bind |
| `LOG_LEVEL` | `INFO` | page content never logged above `DEBUG`; `DEBUG` also prints the OAuth claim keys once |

In **http mode every `http auth` var above is required** — the server fails fast
at startup if any is missing (a public OAuth server must not come up half-armed).
**stdio mode ignores all of them** and runs auth-free (on-box use via
`docker exec`). `PLANE_BASE_URL` (REST instance root) is unrelated to
`LIVE_CONVERT_URL` (internal pages converter) and to `PUBLIC_BASE_URL` (this
server's own public origin).

## Authentication (http transport)

http mode is a public OAuth server: identity via **GitHub OAuth** (FastMCP's
`GitHubProvider` proxy) and authorization via an **allowlist of GitHub logins**.
This lets the server be added as a **claude.ai custom connector** and used across
claude.ai web, Claude Desktop, Claude mobile, Cowork, and Claude Code.

> **Identity ≠ authorization.** GitHubProvider only proves *who* you are — without
> the allowlist, any GitHub account on Earth could complete the flow and reach the
> tools. `ALLOWED_GITHUB_LOGINS` is the gate; it is applied to every tool list and
> call and **fails closed**. An empty allowlist is rejected at startup.

### Register two GitHub OAuth apps

github.com → Settings → Developer settings → OAuth Apps → New. Register one per
environment; the callback URL must match `<PUBLIC_BASE_URL>/auth/callback`
exactly:

| App | Authorization callback URL |
| --- | --- |
| production | `https://planemcp.<domain>/auth/callback` |
| development | `http://localhost:8300/auth/callback` |

Copy each app's **Client ID** and **Client Secret** into `GITHUB_CLIENT_ID` /
`GITHUB_CLIENT_SECRET` for that environment. Minimal scope (`read:user`) is
enough — the server only needs your identity, not repo access.

### Security notes

- **Consent screen stays on.** FastMCP shows a consent screen before authorizing
  a client; it's part of the security model (protection against
  AS-in-the-middle), not friction — do not disable it.
- **The redirect-URI allowlist is defense-in-depth, not the gate.** A Jan 2026
  upstream issue showed DCR clients bypassing it in older releases (since
  hardened — hence the `fastmcp>=3.4.4` pin). The identity allowlist is the gate.
- **Keep FastMCP current.** Auth correctness rides on the OAuth-proxy hardening;
  re-run the test suite after any FastMCP upgrade.
- **Persistence.** FastMCP's Linux defaults (ephemeral signing key, disk store
  under a key-derived path) invalidate all tokens on restart. This server pins
  `JWT_SIGNING_KEY` and stores OAuth state in Redis, **encrypted at rest** with a
  Fernet key derived from `STORAGE_ENCRYPTION_KEY`. A `docker restart` therefore
  resumes existing Claude connections without a re-login.
- **Re-auth cadence.** Access tokens live `TOKEN_EXPIRY_SECONDS` (7 days default);
  clients refresh transparently. In practice mobile/desktop re-prompt roughly at
  that cadence, not per session.
- **`/healthz` is unauthenticated** (for container health checks); it exposes
  nothing but `ok`.

### Rate limiting

The instance sets `API_KEY_RATE_LIMIT` (default `60/minute`). A chained work-item
sequence (list projects → list states → resolve assignees → create) can brush
against it. A `429` surfaces as a clear rate-limit error naming the limit; the
server does **not** silently retry in a loop.

## Database role (pages + work-item relations)

Create a dedicated, least-privilege role (do **not** run as the Plane superuser):

```sql
CREATE ROLE plane_pages LOGIN PASSWORD '…';
GRANT SELECT ON pages, project_pages, projects, workspaces TO plane_pages;
GRANT INSERT, UPDATE ON pages, project_pages TO plane_pages;
GRANT SELECT ON users TO plane_pages;
-- work-item relations (only if you use link_work_items / unlink_work_items):
GRANT SELECT ON issues TO plane_pages;
GRANT SELECT, INSERT, DELETE ON issue_relations TO plane_pages;
-- no other tables
```

Tool paths read `pages`, `project_pages`, `projects`, `workspaces` (page owner is
the configured `SERVICE_USER_ID`, not resolved from `users`; work-item
user/assignee resolution happens over REST, never the DB). `users` is read only
by `verify`. **Relations** additionally read `issues` and read/write
`issue_relations` — `DELETE` is needed because `unlink_work_items` hard-deletes
the relation row. `verify` runs a `SELECT` against **every** one of these tables,
so a missing `GRANT` shows up as a clean `[FAIL] SELECT grant on '<table>'` (with
the exact `GRANT` to run) rather than a runtime error later.

## Post-upgrade ritual: `verify`

```bash
plane-pages-mcp verify
```

Reports **each subsystem independently** and exits non-zero on any failure in an
*enabled* subsystem (a disabled one is skipped, never failed):

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
than shipped broken. A `403` is annotated with a hint that the service user may
not be a member of that project (workspace-level calls can still return 200).

**Auth** (when `PUBLIC_BASE_URL` is set) — confirms `ALLOWED_GITHUB_LOGINS` is
non-empty; fetches `PUBLIC_BASE_URL/.well-known/oauth-authorization-server` and
the protected-resource metadata for `/mcp` (expects 200 JSON naming the
authorize/token endpoints and authorization servers); and round-trips a value
through the encrypted Redis store to confirm the backend is reachable and the
Fernet wrapper is active. Run it **through the tunnel** after deploy so the
discovery docs are checked exactly as an external client sees them.

`verify` **never raises** — a failed probe (bad grant, `403`, connection refused,
timeout, unreachable discovery) is recorded as a `[FAIL]` and the run continues
to the summary line, exiting non-zero. It **fails informatively** against a wrong
`LIVE_CONVERT_URL`, a DB missing a column or grant, a bad/absent PAT, or an
unreachable/misconfigured OAuth endpoint.

## Running

### Local dev (stdio)

```bash
uv venv && uv pip install -e .
cp .env.example .env   # edit it; from the host use localhost URLs
set -a; . .env; set +a
plane-pages-mcp verify
plane-pages-mcp serve   # MCP_TRANSPORT=stdio
```

To exercise the **OAuth flow locally**, use the *development* GitHub app with
`PUBLIC_BASE_URL=http://localhost:8300`, `MCP_TRANSPORT=http`, and a reachable
`REDIS_URL`, then drive it with MCP Inspector or the `fastmcp` client. (claude.ai
cannot reach `localhost`, so the claude.ai/mobile/desktop connector tests run
against the production deployment.)

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

**HTTP** (GitHub OAuth — no header, no secret):

```bash
claude mcp add --transport http plane https://planemcp.<domain>/mcp
```

Claude Code runs the OAuth flow itself (GitHub login → consent → connected). The
**claude.ai custom connector** is added the same way: paste only the URL
(`https://planemcp.<domain>/mcp`) — leave the Client ID / Secret fields blank —
then complete GitHub login. The same connector then works from Claude Desktop and
Claude mobile with no extra setup. Every identity must be in
`ALLOWED_GITHUB_LOGINS` or it is refused after login.

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
      PLANE_WEB_URL: ${PAGES_MCP_WEB_URL:-http://localhost}
      # http auth (GitHub OAuth)
      PUBLIC_BASE_URL: ${PAGES_MCP_PUBLIC_BASE_URL}      # https://planemcp.<domain>
      GITHUB_CLIENT_ID: ${PAGES_MCP_GITHUB_CLIENT_ID}
      GITHUB_CLIENT_SECRET: ${PAGES_MCP_GITHUB_CLIENT_SECRET}
      ALLOWED_GITHUB_LOGINS: ${PAGES_MCP_ALLOWED_GITHUB_LOGINS}
      JWT_SIGNING_KEY: ${PAGES_MCP_JWT_SIGNING_KEY}
      STORAGE_ENCRYPTION_KEY: ${PAGES_MCP_STORAGE_ENCRYPTION_KEY}
      # Logical DB 1 isolates OAuth state from Plane's own Redis use (DB 0).
      REDIS_URL: redis://plane-redis:6379/1
    depends_on: [plane-db, live, plane-redis]
```

`JWT_SIGNING_KEY` and `STORAGE_ENCRYPTION_KEY` are **two independent secrets** —
keep them so. They rotate for different reasons (a suspected leaked JWT vs.
leaked Redis data at rest) and must rotate independently; deriving one from the
other would let a single compromise expose both. No healthcheck is needed:
`restart_policy: any` already covers Valkey not being ready at boot (a one-restart
race), so Plane's own service definitions stay untouched.

External exposure is via the existing Cloudflare Tunnel to
`https://planemcp.<domain>` — public-hostname routing is host-level and already
forwards every path to `:8300`, so `/auth/callback`, `/authorize`, `/token`, and
the `/.well-known/*` documents all flow through the existing route untouched;
nothing to change there. The `convert-document` endpoint has **no auth** and must
remain reachable only inside the compose network. After cutover, rotate/burn the
old `MCP_AUTH_TOKEN` everywhere it was stored — it circulated in shells and configs.

## Caveats & non-goals

- **Concurrency (last-writer-loses).** If a page is open in a browser, the live
  server's in-memory Yjs doc wins on its next persist and can overwrite a tool
  write. No Yjs merging is attempted — write to pages that aren't being edited.
- **No page version history.** Tool page-writes create no `PageLog`/
  `page_versions` entries. Cosmetic, but be aware.
- **Non-goals:** page delete/archive, nested pages (`parent`), image/asset upload
  in content, workspace-global page creation (`is_global`), the internal
  session-auth app API; and for work items: delete, cycle/module membership,
  attachments, comments, intake. (Sub-work-items **are** supported via `parent`,
  and work-item **relations** via `link_work_items`/`unlink_work_items`.)

## Development

```bash
uv pip install -e '.[dev]'
pytest   # conversions, row shaping, column coverage, config capabilities,
         # REST helpers, work-item shaping (fake REST), auth middleware
```

Integration testing runs against a real Plane instance using a throwaway project
and disposable pages/items (take a `pg_dump` first for pages).
