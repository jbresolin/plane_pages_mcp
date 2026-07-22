# plane_pages_mcp

An MCP server that exposes **read/write access to Plane CE Pages**, which are
absent from Plane CE's public REST API (upstream: makeplane/plane#8986).

- **Reads** go straight to Postgres (scoped to one workspace, soft-delete aware).
- **Writes** go through Plane's own internal `live` service HTML→Yjs converter,
  so the collaborative-editor state stays consistent and edits are visible in
  the UI (not silently overwritten).

Built with Python + [FastMCP](https://github.com/jlowin/fastmcp). Ships as one
container to sit inside your Plane Docker Compose stack.

> **Version coupling.** All schema/endpoint facts were verified against Plane CE
> **v1.3.1**. On any Plane upgrade, re-run `plane-pages-mcp verify` (see below)
> before trusting writes — a schema change can add a required column.

## How it works

A page's content lives in **four** columns on `pages`: `description_html`,
`description_json`, `description_binary` (the Yjs editor state — authoritative),
and `description_stripped`. Writing only HTML produces edits that are invisible
in the editor and get clobbered by the live server. So **every write sets all
four together**, deriving json + binary from the live service's
`convert-document` endpoint (the same converter Plane uses internally, e.g. in
`bgtasks/copy_s3_object.py`).

## Tools

| Tool | Purpose |
| --- | --- |
| `list_pages(project?, include_archived=False, limit=50)` | List pages (newest first). `project` = identifier (e.g. `ENG`) or UUID; omit for the whole workspace. |
| `search_pages(query, limit=20)` | Case-insensitive match on name + body; returns a ±120-char snippet. |
| `read_page(page_id, format="markdown")` | Metadata + content as markdown or html. |
| `create_page(title, content, project, format="markdown", access="public")` | Create a page; returns id + UI URL. |
| `update_page(page_id, content, format="markdown", title?, mode="replace")` | Replace or `append` body (append is useful for log-style pages). |

Content accepts **markdown** (default; rendered with the `tables` and
`fenced_code` extensions) or raw **HTML** via `format="html"`.

## Configuration

Set via environment (see [`.env.example`](.env.example)):

| Var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | — | dedicated role, see below |
| `WORKSPACE_SLUG` | — | resolved to a workspace id at startup |
| `SERVICE_USER_ID` | — | UUID that tool-created pages are owned by |
| `MCP_AUTH_TOKEN` | — | **required in http mode**; requests without a matching Bearer are rejected |
| `LIVE_CONVERT_URL` | `http://live:3000/live/convert-document/` | confirm base path at deploy |
| `PLANE_WEB_URL` | `http://localhost` | used to build the UI URL from `create_page` |
| `MCP_TRANSPORT` | `http` | `http` (streamable, `0.0.0.0:8300/mcp`) or `stdio` for dev |
| `MCP_HOST` / `MCP_PORT` / `MCP_PATH` | `0.0.0.0` / `8300` / `/mcp` | http bind |
| `LOG_LEVEL` | `INFO` | page content is never logged above `DEBUG` |

## Database role

Create a dedicated, least-privilege role (do **not** run as the Plane superuser):

```sql
CREATE ROLE plane_pages LOGIN PASSWORD '…';
GRANT SELECT ON pages, project_pages, projects, workspaces TO plane_pages;
GRANT INSERT, UPDATE ON pages, project_pages TO plane_pages;
-- no DELETE, no other tables
```

## Post-upgrade ritual: `verify`

```bash
plane-pages-mcp verify
```

Connects to the DB + live service and checks every assumption, exiting non-zero
with a report on any mismatch:

1. Expected tables exist.
2. The four description columns exist, and **every NOT-NULL-without-default
   column on `pages`/`project_pages` is covered by the INSERT builders** (so a
   new required column fails loudly here, not mid-write).
3. `pages.deleted_at` exists (reads filter `deleted_at IS NULL`).
4. `POST <p>ping</p>` to the converter returns 200 with both keys and a
   non-empty decoded binary.
5. Prints the resolved workspace id, service user, and `sort_order` default.

Run it after every Plane upgrade. It should **fail informatively** against a
wrong `LIVE_CONVERT_URL` or a DB missing a column.

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
claude mcp add plane-pages -- \
  env MCP_TRANSPORT=stdio \
      DATABASE_URL='postgresql://plane_pages:…@localhost:5432/plane' \
      WORKSPACE_SLUG=test \
      SERVICE_USER_ID='<uuid>' \
      LIVE_CONVERT_URL='http://localhost:3300/live/convert-document/' \
  plane-pages-mcp serve
```

**HTTP** (server running with a Bearer token):

```bash
claude mcp add --transport http plane-pages http://localhost:8300/mcp \
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
      WORKSPACE_SLUG: ${PAGES_MCP_WORKSPACE_SLUG}
      SERVICE_USER_ID: ${PAGES_MCP_SERVICE_USER_ID}
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
- **No version history.** Tool writes create no `PageLog`/`page_versions`
  entries. Cosmetic, but be aware.
- **MVP non-goals:** delete/archive, labels, nested pages (`parent`), image/
  asset upload, workspace-global page creation (`is_global`), and any use of the
  internal session-auth app API.

## Development

```bash
uv pip install -e '.[dev]'
pytest          # unit tests: conversions, snippet/row shaping, column coverage
```

Integration testing is done against a real Plane instance using a throwaway
project and disposable pages (take a `pg_dump` first).
