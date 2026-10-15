# Editor harness (end-to-end)

Writes one Plane page per markdown sample through this MCP's own write code,
opens each page in the real Plane web editor with headless Chromium, and checks
that the editor loads and that the content reads back intact. Rerun it after
every Plane upgrade: it is the only test that exercises Plane's editor.

It is skipped in the normal `pytest` run. It needs a running Plane you control
(never production: it creates pages and soft-deletes leftovers by title).

## Run

```sh
uv pip install --python .venv/bin/python -e '.[e2e]'
PLANE_E2E=1 .venv/bin/python -m pytest tests/e2e -v
```

Each test id names the sample and the write path, e.g.
`test_page_opens_in_editor_and_round_trips[table_pipe_in_cell-update_replace]`,
and its page is titled the same way (prefix `[e2e]`), so a failure can be
opened by hand. Pages from the previous run are soft-deleted at the start of
the next one.

## What it checks

- **Samples**: `constructs.py`. Each construct on its own, then mixed documents,
  including the shape of the pages that broke in production.
- **Write paths**: `create` (create_page), `update_replace` (a blank page made
  through Plane's app API, as the UI does, then update_page) and
  `update_append`.
- **"Loaded"**: see `editor_probe.py`. The body editor appears, a sentinel at the
  end of the page renders, the tab still responds, and there are no console
  errors beyond those a plain page also logs.
- **Editor copies** (`test_editor_sync.py`): a browser that saw the old
  version sees only the update after reopening, and an update is refused
  while the page is open in a tab, then accepted once it closes.
- **Round trip**: the stored HTML, after the editor has had the page open, is
  converted back with the same function `read_page` uses. The sample's key
  strings must still be there.

## Settings (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `PLANE_E2E` | unset | must be `1` or the harness is skipped |
| `PLANE_E2E_WEB_URL` | `http://localhost` | Plane web origin (the proxy) |
| `PLANE_E2E_DATABASE_URL` | `postgresql://plane:plane@localhost:5432/plane` | Plane's Postgres |
| `PLANE_E2E_LIVE_URL` | `http://localhost:3300/live/convert-document/` | live converter |
| `PLANE_E2E_WORKSPACE` / `PLANE_E2E_PROJECT` | `test` / `TEST` | where pages go |
| `PLANE_E2E_USER_EMAIL` | `test@bresolinrs.com.br` | user who opens (and owns) pages; must be a project member |
| `PLANE_E2E_SESSION_ID` | unset | a `session-id` cookie to use as is |
| `PLANE_E2E_API_CONTAINER` | `plane-dev-api-1` | if no session is given, one is minted with `docker exec` in this container |
| `PLANE_E2E_CHROMIUM` | Playwright's own | path to a Chromium binary, if you don't run `playwright install chromium` |
| `PLANE_E2E_REDIS_URL` | `redis://localhost:6380/0` | Plane's Redis, for the open-page check |

Plane's stock compose file doesn't publish Redis. For the dev stack, add a
compose override like this (6379 is often taken on the host):

```yaml
services:
  plane-redis:
    ports:
      - "127.0.0.1:6380:6379"
```

The session is minted inside the Plane api container with Django's own session
store, so the harness never needs or changes a password.
