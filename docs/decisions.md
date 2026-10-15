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

## 2026-10-15: create_project enforces the web UI's identifier rule

**Decision.** `create_project` accepts an identifier only if, after trimming
and uppercasing, it is 1–10 characters from `A-Z`, `0-9` and `ÇŞĞIİÖÜ`. It
also rejects names containing Plane's forbidden characters
(`& + , : ; $ ^ } { * = ? @ # | ' < > . ( ) % ! -`) or longer than 255
characters. All of this is checked before calling Plane.

**Reason.** Plane v1.3.1 has two rules. The API/model allows 12 characters
and anything except the forbidden set (so spaces, `_`, any Unicode letter).
The web UI's create form allows 1–10 of `[ÇŞĞIİÖÜA-Z0-9]`. A project created
with an API-only identifier works, but the UI's settings form then rejects it
when someone edits the project. Chosen with the operator. The name check
mirrors the serializer, which applies the forbidden set to names too.

## 2026-10-15: Explain Plane's ambiguous 409 on project create

**Decision.** On HTTP 409 from project create, list the workspace's projects
and report whether the **identifier** (compared uppercase) or the **name**
(compared exactly) is taken. If no visible project matches, say "name or
identifier in use, possibly by an archived project or one this key can't
see". There is no pre-check, so a normal create is a single request.

**Reason.** Plane v1.3.1 answers `409 {"name": "The project name is already
taken"}` for a duplicate identifier too (verified on the dev stack), which
sends the caller to change the wrong field. Names are unique
case-sensitively ("ZZ PROBE" and "zz probe" coexist); identifiers are stored
uppercase, so they clash regardless of case. A deleted project's identifier
can be reused.

## 2026-10-15: Never send `project_lead` to Plane

**Decision.** `create_project` never sends Plane's `project_lead` field, on
create or by PATCH. The lead is added as a project member through
`POST …/projects/<id>/members/` after the project exists.

**Reason.** In Plane v1.3.1, `POST /api/v1/workspaces/<slug>/projects/` with
`project_lead` set is broken:

- After saving the project, the view runs
  `ProjectMember.objects.create(member_id=serializer.instance.project_lead, …)`.
  That passes a `User` object where Django expects a UUID, which raises
  `ValidationError: "<username> <email>" is not a valid UUID`.
- The API returns `400 {"error": "Please provide valid detail"}`.
- Nothing is wrapped in a transaction, so the project **already exists**: the
  API key's user is Admin, there is no lead and **no default states**. That's
  a half-created project.
- Reproduced on the dev stack with both another member and the key's own
  user as lead. (The view's "lead is the caller" check also compares
  `str(User)` to an id, so it never matches.)

Do not "fix" this by sending `project_lead` until Plane is upgraded and the
bug is confirmed gone. The UI's "lead" label is cosmetic; membership is what
makes the project visible.

## 2026-10-15: Project lead = project Admin; workspace Guests can't join

**Decision.**
- `create_project(lead=…)`, or `DEFAULT_PROJECT_LEAD` when no lead is given,
  adds that person as project **Admin** (role 20).
- The lead is matched exactly: email ignoring case, or display name as
  written. No match, or several matches, is an error that lists the
  candidates.
- Workspace **Guests** are refused, before anything is created.
- If adding the lead fails after the project exists, the project is kept and
  the result's `notes` say the lead was NOT added, why, and give the exact
  `add_project_member(...)` call that fixes it.
- `add_project_member` uses the same lookup and Guest rule, and allows roles
  `admin`/`member` only.
- If the lead is the API key's own user, nothing more is sent: they're
  already Admin, and adding them again would fail.

**Reason.** Projects created by the MCP had only the service user as a
member, so the person who asked couldn't see them. Plane itself would make a
workspace Guest a project Admin if asked, so the rule is ours. Chosen with the
operator.

## 2026-10-15: New projects are private by default (direct DB write)

**Decision.** `create_project(visibility="private")` is the default.
`network` is always sent explicitly (0 private, 2 public). Because Plane
v1.3.1 ignores it, a private project is then made private with
`UPDATE projects SET network = 0` (`db.set_project_network`). That needs
`DATABASE_URL` and `GRANT UPDATE (network, updated_at) ON projects`.

**Checked before creating (added 2026-10-15, at the operator's request).** For
a private project, `create_project` first confirms the write can happen, and
**refuses without creating anything** if there is no database access or the
role lacks the grant. The error gives the fix: set `DATABASE_URL`, run the
`GRANT`, or pass `visibility="public"` if a public project is really wanted.
The grant check is the same `has_column_privilege` query `verify` uses
(`db.PROJECT_NETWORK_GRANT_SQL`), and if the check itself errors, that is a
refusal too. Only a failure of the write itself, after the create, leaves a
public project; it is kept and reported as `"visibility": "public"` with a
note. When no lead ends up a member, the notes say only the service user is a
member, and that nobody else can see a private project, naming
`add_project_member` as the fix.

**Reason.** Plane stores visibility in `projects.network` (`NETWORK_CHOICES`:
0 "Secret", 2 "Public"; model default 2). The v1.3.1 REST create and update
serializers don't include `network`. POST and PATCH with `network: 0` both
succeed and the project stays public (verified on the dev stack). A private
default can only be reached by writing the column. The relations tools set
the precedent for narrow DB writes where the API falls short. Chosen with the
operator.

**Checked.** On the dev stack, a private project is absent from a
non-member's project list in the web app (direct GET returns 403) and appears
once they are added.
