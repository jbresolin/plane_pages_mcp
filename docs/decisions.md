# Decisions

Non-obvious decisions, newest last. Each entry: date, decision, reason.

## 2026-10-15: Raw HTML in markdown input is text, not tags

**Decision.** markdown-it now runs with `html: False`. Anything tag-shaped in
markdown (`<box>`, `Record<string, X>`, `Array<T>`, a stray `<pre>`) is escaped
and shown literally. This applies wherever markdown is accepted: pages,
work-item descriptions and comments. Callers who really mean HTML pass
`format="html"`, which is unchanged. Autolinks (`<https://…>`) still work
because markdown handles them with a separate rule.

**Reason.** The editor harness (tests/e2e) showed that, with raw HTML enabled:

- inline placeholders that parse as tags (`<box>`, `<T>`, `<img>`, `<iframe>`, …)
  silently vanished from the page;
- a placeholder alone on a line opened a raw HTML block, so a table directly
  below it was stored as literal `| a | b |` text;
- an unclosed `<pre>`, `<script>`, `<textarea>` or `<style>` hid everything
  after it.

When the page is opened, the editor writes its own version back to the
database, so the loss becomes permanent. Agents write placeholders as prose far
more often than they intend raw HTML.

**Cost.** A `<br>` inside a markdown table cell now shows as the text `<br>`
instead of a line break. Use `format="html"` for that.

## 2026-10-15: Escape `|` in table cells when reading pages back as markdown

**Decision.** `html_to_markdown` escapes `|` inside `<td>`/`<th>` as `\|`,
including inside code spans.

**Reason.** Without the escape, a cell reading `a | b` came back as an extra
column. Reading a page and writing it back changed the table.

**Not fixed here.** Plane's editor drops the language of a fenced code block
when it re-serializes the page (it stores `<pre><code>` with no class). The
language therefore cannot round-trip, whatever this server does.

## 2026-10-15: Rebase page updates onto the existing Yjs history

**Decision.** `update_page` no longer stores the converter's Yjs state as is.
It loads the page's current `description_binary`, deletes the content inside
that history, then merges in the converter's fresh state (`yjs_state.py`,
using `pycrdt`). `create_page` is unchanged: a new page has no history.

**Reason.** Plane's editor caches each page's Yjs document in the browser
(IndexedDB). The converter returns a document unrelated to the page's history,
so a browser that had seen the page merged its cached copy with ours on the
next visit. The editor showed old + new content (or only the old), and saved
that mix back to the database for everyone. Reproduced on the dev stack; the
e2e test `test_browser_that_saw_the_old_version_shows_only_the_update` fails
without this change. This is the likely cause of the pages that "broke" after
MCP writes. The editor freeze itself was never reproduced.

**Alternatives.** (1) Write through the live server as a collaborating Yjs
client: also fixes open pages, but much larger and tied to Plane internals.
(2) Leave as is and document it: data loss stays. Chosen with the operator as
"option A".

**Cost.** New dependency `pycrdt` (Rust wheels), pinned to `<0.15` because its
0.x API changes between minors.

## 2026-10-15: Refuse page updates while the page is open in the editor

**Decision.** Before writing, `update_page` asks Plane's live server whether
the page is loaded, and refuses with a clear error if it is. It asks by
publishing a Hocuspocus QueryAwareness message on Redis channel
`hocuspocus:<page id>`; a live server that has the page loaded answers. Any
answer means open, silence (1 s) means closed (`presence.py`). If the check
can't run, the update is **refused** (fail closed). With no Redis configured
at all, the check is skipped and a warning is logged at startup and by
`verify`.

**Reason.** While a page is open the live server's in-memory copy overwrites
the database on its next save, so the update was silently lost (reproduced).
Rebasing doesn't help there. Chosen with the operator as "option B".

**Why this signal.**
- "The channel has a subscriber" is not usable: the Redis extension leaves
  its subscription behind after the page closes (336 stale channels seen on
  the dev stack).
- An answer is only sent while the page is actually loaded. That was
  verified against headless tabs and against a real browser.
- Answers can list zero client states (headless tabs set none), so the
  presence of an answer is what counts, not its contents.

**Config.** Reuses `REDIS_URL` (already Plane's Redis in the standard
deployment; pub/sub ignores the DB number). `PLANE_LIVE_REDIS_URL` overrides.

**Known gap.** A tab opened in the sub-second window between the check and the
commit loads the old state, and its copy wins. Accepted: it needs a person
opening the page at that exact moment.
