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
