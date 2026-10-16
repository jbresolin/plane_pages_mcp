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

The editor also keeps **its own copies** of a page outside the database, and
`update_page` has to respect both:

- **Browsers cache the Yjs document** (IndexedDB) after a visit. The converter
  returns a brand-new Yjs document with no relation to the page's history, so
  storing it as is made a returning browser merge old + new content and save
  that mix back. `update_page` therefore stores the new content **as a
  continuation of the page's existing Yjs history** (delete the old content,
  add the new), so cached copies converge on the new content (`yjs_state.py`).
- **While a page is open**, the live server holds it in memory and its copy
  overwrites the database on its next save. `update_page` **asks the live
  server first** (a Hocuspocus "who is here?" query over Plane's Redis,
  `presence.py`) and refuses with a clear error while the page is open. Close
  the page and retry. If the check can't run (Redis unreachable), the update
  is refused too, rather than risking a silent loss.

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
| `update_page(page_id, content, format="markdown", title?, mode="replace")` | Replace or `append` body (inherits the page's workspace). **Refused while the page is open in the editor.** |

### Work items (REST) — enabled by `PLANE_BASE_URL` + `PLANE_API_KEY`

| Tool | Purpose |
| --- | --- |
| `list_projects(workspace?)` | id, name, identifier, description. |
| `create_project(workspace, name, identifier, description?, lead?, visibility="private")` | **`workspace` required.** Returns id, identifier, `visibility`, `lead`, `notes`. Identifier: 1–10 letters/digits (Plane's UI rule), stored uppercase; a taken identifier or name gets a message saying which. `lead` (email or exact display name; default `DEFAULT_PROJECT_LEAD`) is added as project **Admin**; workspace Guests are refused before anything is created. `visibility="private"` needs DB access plus the `projects(network)` grant (see Database role) and is refused up front without them. **Read `notes`**: they say if the lead wasn't added or the project couldn't be made private, and how to fix it. The API key's user must be a workspace Admin/Member. |
| `add_project_member(workspace, project, member, role)` | **`workspace` required.** Add a workspace Admin/Member (email or exact display name) as `admin` or `member`. Guests are refused. |
| `list_work_items(project, workspace?, state_name?, assignee?, limit=50)` | Issues with sequence id (`TEST-42`), state/assignee **names** (not UUIDs), priority. |
| `get_work_item(item, project, workspace?)` | Full detail; `item` = `TEST-42` or UUID; description as markdown; `parent` (as a sequence ref) and `sub_issues_count`. |
| `create_work_item(project, title, workspace, description?, state_name?, priority?, assignees?, labels?, parent?)` | **`workspace` required**; human names resolved to UUIDs. `parent` (a `TEST-42`/UUID ref) creates a **sub-work-item**. |
| `update_work_item(item, project, workspace?, …, parent?)` | Partial update; only supplied fields are sent. `parent` re-parents into a sub-work-item. |
| `list_states(project, workspace?)` / `list_labels(project, workspace?)` | Discover valid state/label names. |
| `list_comments(item, project, workspace?)` | Comments with body as markdown, `actor` as a name, `edited` flag. |
| `add_comment(item, body, project, workspace?, format="markdown")` | Add a comment (markdown or HTML). |
| `update_comment(item, comment_id, body, project, workspace?, format="markdown")` | Edit a comment (`comment_id` from `list_comments`). |
| `delete_comment(item, comment_id, project, workspace?)` | Delete a comment. |
| `link_work_items(item, related_item, relation_type, project, workspace?)` | Create a relation (needs the **DB** subsystem — relations aren't in the REST API). |
| `unlink_work_items(item, related_item, relation_type, project, workspace?)` | Remove a relation. |
| `list_cycles(project, workspace?)` / `list_modules(project, workspace?)` | id, name, dates, issue counts (modules also `status`). |
| `create_cycle(project, name, workspace, start_date?, end_date?, description?)` / `create_module(…)` | **`workspace` required**. |
| `delete_cycle(cycle, project, workspace?)` / `delete_module(module, project, workspace?)` | By name or UUID; leaves issues intact. |
| `assign_to_cycle(cycle, items, project, workspace?)` / `assign_to_module(module, items, …)` | Add work items (list of `TEST-42`/UUIDs). |
| `unassign_from_cycle(…)` / `unassign_from_module(…)` | Remove work items (non-members ignored). |

**Cycles and modules** are fully served by the public REST API (create + delete +
issue assignment via the `cycle-issues`/`module-issues` sub-resources), so these
tools are pure-REST — no DB. `end_date` maps to a module's target date; cycle/
module resolution accepts a name or UUID.

**Comments** are pure-REST too: content is sent as `comment_html` (markdown is
converted for you) and read back as markdown, with the `actor` resolved to a
display name. Comments are addressed by their UUID (from `list_comments`).

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
blank line, matching GitHub) or raw **HTML** via `format="html"`. In markdown,
tag-like text is **text**: `<box>`, `Record<string, X>` or a stray `<pre>` show
up literally instead of being treated as HTML (use `format="html"` for real
tags; `<https://…>` autolinks still work). Reading back escapes `|` in table
cells so tables round-trip. Priorities:
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
| `DEFAULT_PROJECT_LEAD` | — | work items; email of the person `create_project` adds as project Admin when no `lead` is given. Unset → only the service user is a member (the result says so) |
| `WORKSPACE_SLUG` | — | **default** workspace (not a fixture); may be unset |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | — | **http auth**; per-environment GitHub OAuth app |
| `PUBLIC_BASE_URL` | — | **http auth**; public origin, e.g. `https://planemcp.<domain>` (no `/mcp`) |
| `ALLOWED_GITHUB_LOGINS` | — | **http auth**; comma-separated GitHub logins allowed (case-insensitive); empty is rejected |
| `JWT_SIGNING_KEY` | — | **http auth**; long secret, stable across restarts (≥12 chars) |
| `REDIS_URL` | — | **http auth**; OAuth state store, e.g. `redis://plane-redis:6379/1`. Must be **Plane's** Redis: it also serves the open-page check (below) |
| `PLANE_LIVE_REDIS_URL` | `REDIS_URL` | pages; Plane's Redis for `update_page`'s open-page check. Only set it if `REDIS_URL` points elsewhere (pub/sub ignores the DB number). Unset both → the check is skipped with a startup warning |
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
against it, and a batch of reads (listing a dozen projects) used to fail halfway.

On a `429`, requests that are **safe to repeat** (GET, HEAD, OPTIONS, PUT,
DELETE) are retried **up to 3 times**, waiting what Plane's `Retry-After`
says (or 1/2/4 s with jitter if it sends none), with **at most 65 s of
waiting** per request. After a burst Plane asks for about 57 s, because
nothing frees up until the oldest request in the 60 s window expires. Every
retry is logged as a warning (`Plane REST 429 on GET …: retry 1/3 in 57.0 s`).
**Creates and partial updates (POST, PATCH) are never retried**, because
repeating one could make a duplicate. When the budget runs out, the usual
clear rate-limit error is raised, saying why it stopped. While a retry is
waiting, the tool call blocks, for up to about a minute.

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
-- private projects (create_project): Plane v1.3.1's API ignores visibility,
-- so the MCP sets this one column itself. Without it, create_project refuses
-- visibility="private" (the default) before creating anything.
GRANT UPDATE (network, updated_at) ON projects TO plane_pages;
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

Images are published to Docker Hub as `jbresolin/plane_pages_mcp:vX.Y.Z` (and
`:latest`) by the release workflow; see "Making a release" below. Pin a
version tag in production, not `latest`. Add this service to your Plane stack
(same network as `plane-db`, `live` and `plane-redis`; publish only the MCP
port):

```yaml
  pages-mcp:
    image: jbresolin/plane_pages_mcp:${PAGES_MCP_RELEASE:-v0.3.0}
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

### Making a release

`.github/workflows/ci.yml` runs the unit tests on every pull request and every
push to `master`. A **version tag** also builds and publishes the image:

1. In a pull request, bump `version` in `pyproject.toml` (e.g. `0.3.0`), then
   merge it.
2. Tag the merge commit on `master` with `v` + that version and push the tag:
   ```sh
   git switch master && git pull
   git tag -a v0.3.0 -m "v0.3.0"
   git push origin v0.3.0
   ```
3. The workflow then runs these steps, and any failure stops the release
   before anything is pushed:
   - it runs the tests;
   - it **refuses** if the tag isn't exactly `v` + the `pyproject.toml`
     version (`scripts/check_release_version.py`);
   - it builds the image with the labels `org.opencontainers.image.version`
     (`0.3.0`) and `org.opencontainers.image.revision` (the full commit SHA);
   - it checks that the image reports that version and carries both labels;
   - it pushes `jbresolin/plane_pages_mcp:v0.3.0` and `:latest`.

It needs the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a
Docker Hub access token with read/write on the repository). The code's
`plane_pages_mcp.__version__` is read from the installed package metadata,
so `pyproject.toml` is the only place the version is written.

### Which commit is this container running?

```sh
# a running container (name as in `docker ps`, e.g. the Portainer stack's):
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' <container>
# an image:
docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }} {{ index .Config.Labels "org.opencontainers.image.version" }}' jbresolin/plane_pages_mcp:v0.3.0
```

The revision is the full git commit SHA. `git show <sha>` shows it, or
`https://github.com/jbresolin/plane_pages_mcp/commit/<sha>`. Images built
before v0.3.0 carry no labels, and the commands print `<no value>`.

## Caveats & non-goals

- **Pages open in the editor can't be updated.** `update_page` refuses while
  a page is open (see "How pages work"). There is a sub-second window between
  the check and the commit in which a newly opened tab could still load the
  old state; that tab's copy would then win.
- **No page version history.** Tool page-writes create no `PageLog`/
  `page_versions` entries. Cosmetic, but be aware.
- **Non-goals:** page delete/archive, nested pages (`parent`), image/asset upload
  in content, workspace-global page creation (`is_global`), the internal
  session-auth app API; and for work items: delete, attachments, intake.
  (Sub-work-items **are** supported via `parent`, work-item **relations** via
  `link_work_items`/`unlink_work_items`, **cycles/modules** — CRUD + issue
  assignment — via the `*_cycle`/`*_module` tools, and **comments** — CRUD — via
  the `*_comment` tools.)

## Development

```bash
uv pip install -e '.[dev]'
pytest   # conversions, row shaping, column coverage, config capabilities,
         # REST helpers, work-item shaping (fake REST), auth middleware,
         # Yjs rebase + open-page check (fakes)
```

**Editor harness.** `tests/e2e` writes pages through this server's code and
opens them in the real Plane web editor with headless Chromium (load, no
freeze, no new console errors, content round-trips, stale-cache and open-page
behaviour). Skipped unless `PLANE_E2E=1`; rerun it after every Plane upgrade.
See [tests/e2e/README.md](tests/e2e/README.md).

Integration testing runs against a real Plane instance using a throwaway project
and disposable pages/items (take a `pg_dump` first for pages).
