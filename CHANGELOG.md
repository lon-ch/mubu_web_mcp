# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-09-17

Aligned with Mubu's own export using four real documents as references.

### Added

- **Bold/italic detection by CSS class** — real documents express emphasis as
  `<span class="bold text-color-green">…</span>`, not `<b>`/`<strong>` or `font-weight`
  (verified: 0 occurrences of `font-weight`/`<b>` across 314 nodes, 30+ occurrences of
  `class="bold"`). Colour/highlight classes are unwrapped, since Markdown has no equivalent.
- Zero-width spaces (U+200B, 17 occurrences in one reference document) are stripped, matching
  the official export.
- Title now comes from the **document name**, the first node is emitted as a paragraph, its
  children are promoted to level 0, and further top-level nodes become `#` sections.
- Notes are indented to align with the node's text column (`- ` items get +2, the leading
  paragraph gets 0).

### Notes

- Document/account identifiers were removed from `docs/mubu-api-notes.md` (replaced with
  `<doc-id>` / `<user-id>`) before the repository was made public. No personal notes,
  tokens, credentials or exported documents are tracked in this repository.

## [0.5.0] - 2026-09-14

### Added

- **Rich-text conversion for node content.** Mubu stores node `text` and `note` as HTML, not
  plain text (confirmed on a live document: `<span>…</span>`, `<div class="table-container">
  <table>…</table></div>`). A conversion layer now maps the tags we have actually seen:
  `<span>`/`<div>` are unwrapped, `<b>`/`<strong>` → `**…**`, `<i>`/`<em>` → `*…*`,
  `<code>` → backticks, `<a href>` → `[text](url)`, `<br>` → newline, and HTML entities are
  unescaped. **Unknown tags are kept verbatim** rather than silently dropped.
- **Tables become standard Markdown pipe tables** (`| a | b |` plus the `---` separator row),
  matching Mubu's own export, and are emitted as a block at the node's indent instead of a list
  bullet.
- `emoji` node field is emitted as a prefix on the node's line.

### Notes

- Checkboxes were confirmed again on a live document (`finish` on three nodes) and are covered by
  a dedicated regression test producing `- [ ]` / `- [x]` exactly like the official export.

## [0.4.0] - 2026-09-14

Aligns the Markdown output with Mubu's own export and uses the real document structure that was
confirmed on live documents.

### Added

- **Internal document links are rewritten to local relative paths.** Mubu link URLs carry the
  target document id (`https://mubu.com/app/edit/home/<id>`), so the backup builds an
  id → local path map in a listing-only pre-pass and substitutes it while rendering. Links to
  documents outside the backup (or to documents that could not be read) keep their original URL.
- **Images use the real `images` field** — `[{"id", "uri", "w", "ow", "oh"}]` — instead of the
  previous field-name/URL-suffix heuristic. `uri` is a relative path
  (`document_image/<user>_<uuid>.<ext>`) and is resolved to `https://api2.mubu.com/v3/<uri>`
  (verified: returns `image/png`). Alt text defaults to `image-N` like the official export, and
  `w`/`ow`/`oh` are recorded in `assets.json`.
- **Task metadata is preserved as an HTML comment** (`<!-- mubu: deadline=… taskStatus=1 … -->`),
  so `deadline`, `remindAt`, `taskStatus` and `collapsed` survive without inventing visible
  Markdown syntax.

### Changed

- **Notes now follow the official export convention**: emitted immediately after the node's own
  line, indented one level deeper, *before* its children (previously they were emitted after the
  children at the node's own indent). The parser was updated to match, so round-trips stay stable.
- Images are emitted at the node's own indent, matching the official export.

## [0.3.0] - 2026-09-14

### Security

- Asset downloads are **HTTPS only**; `http://` is refused outright.
- URLs carrying a username/password (`https://user:pass@host/…`) are refused.
- **Redirects are validated hop by hop**: 30x responses are handled manually, every target is
  checked against the whitelist *before* the next request is sent, and the JWT is only ever
  attached to `*.mubu.com` hosts. A redirect to a foreign host aborts the download and the
  foreign URL is never requested.
- Downloads are bounded by a size limit, a timeout and a redirect-count limit; content that is
  not an image MIME type is rejected instead of being saved as `.png`.
- Report/log entries store only a redacted URL (`https://host/…<digest>`), never the full path
  or query string.

### Added

- Images are downloaded into `<document>.assets/` and **written into the Markdown at the node
  where they appear**, with relative `/`-separated paths and an `assets.json` index.
- Failed images produce a visible placeholder in the document and a structured entry in the
  manifest (node id, redacted address, reason, attempts, timestamp) without losing the document.
- Ordered naming: sort prefixes (`001 `) restore Mubu's ordering on disk, and can be disabled
  with `--no-prefix`.
- Collision-safe names: duplicates within a directory get a stable `__<shortid>` suffix
  (case-insensitive), Windows reserved names are escaped.
- Stale handling: renamed/moved documents leave the old file in place, are listed under
  `stale` in the manifest, and are only moved to `_backup_stale/` by an explicit
  `backup prune --confirm`. `_backup_stale/` never touches files the tool did not write.
- `backup verify` (re-hash and compare), `backup report` (human/JSON summary) and
  `backup prune --dry-run|--confirm` subcommands.
- Human-readable and JSON reports with per-run statistics (API requests, asset requests,
  retries, rate-limit hits, duration, average interval); partial failures exit with code 2.
- Atomic writes for the manifest, state file, Markdown and images (temp + replace); a corrupt
  manifest is quarantined instead of being overwritten.
- Multiline notes keep their paragraph structure, and images/ordered lists have a defined
  Markdown mapping.

### Changed

- Incremental skipping now also compares the document name and target path, so renames,
  moves and ordering changes are detected instead of silently keeping a stale file.
- Installer backups are timestamped (`config.toml.20260914-101500.bak`) instead of overwriting
  one `.bak`; JSON/TOML are validated before replacing, the original is restored on failure,
  and TOML section matching is exact (a similar section name is no longer deleted).
- `setup --yes` fails with a clear error when credentials are missing instead of silently
  dropping into interactive prompts.

## [0.2.0] - 2026-09-13

### Added

- **Structured output**: `mubu_list`, `mubu_inspect` and `mubu_get_doc_json` return
  `structuredContent` with a declared `outputSchema`, so programs no longer have to parse
  human-facing Chinese text. Original API fields are preserved, with `parentId`, `order`
  and `updatedAt` added alongside them.
- **Paging instead of truncation**: `mubu_get_doc_json` pages the top-level nodes with a
  cursor (`limit` / `offset` / `nextCursor`). `mubu_get_doc(format="json")` now returns the
  complete JSON — the old 60 000 character slice produced unparseable half-JSON.
- **`mubu_inspect`**: a redacted structure report (field names, counts, suspected image/link
  fields, URL host names only) so interface structures can be investigated and shared without
  leaking document text.
- **`mubu_diagnostics`**: request/retry/rate-limit/login counters and the last error code —
  never document content or credentials.
- **Local backup engine** (`mubu-web-mcp backup`): recursive index, per-folder scope,
  incremental (unchanged documents are skipped without even fetching them), resumable via a
  state file, configurable interval (default 2 s), Markdown output plus a `manifest.json`
  with sizes and SHA-256. Document content never passes through an AI model.
- **Experimental asset download** (`--assets`) with a strict `mubu.com` hostname whitelist,
  re-validation after redirects, and no credential header sent to non-Mubu hosts.
- **Cancellation**: `notifications/cancelled` aborts in-flight requests and backoff waits;
  `serve()` now handles requests on worker threads so cancellation can actually arrive.

### Changed

- **Rate limiting** is now cross-process (file lock + shared timestamp), default minimum
  interval raised 200 ms → 500 ms, plus jitter. `Retry-After` is honoured for HTTP 429 and
  503, backoff is capped (`MUBU_MAX_BACKOFF_SECONDS`), and 429 / 5xx / network / auth failures
  are classified separately (`RateLimitError`, `AuthError`, `MubuError`).
- Business rate-limit errors returned inside HTTP 200 are detected by code
  (`MUBU_RATE_LIMIT_CODES`) or message keywords, and retried with backoff.
- Authentication failure re-logs in **once** and never loops.
- URL validation now compares the parsed hostname (and rejects non-443 ports) instead of using
  a string prefix, so `api2.mubu.com.evil.example` is rejected.
- The login token is stored through the OS secret store (DPAPI / Keychain / Secret Service)
  like the credentials are, instead of only a `0600` file.
- The CLI grew `backup` and `inspect` subcommands; `mubu-web-mcp backup` is the supported
  personal-backup path.

### Fixed

- Tests no longer touch real OS credential stores (a regression test asserts this), so running
  the suite on macOS can no longer write into the developer's Keychain.
- `mubu_markdown` tolerates malformed child entries instead of raising `AttributeError`.

## [0.1.0] - 2026-09-13

First public release.

### Added

- MCP server (stdio transport, implemented with the standard library only) exposing
  `mubu_whoami`, `mubu_list`, `mubu_get_doc`, `mubu_search`, `mubu_create_doc`,
  `mubu_create_folder`.
- Round-trip stable Markdown to Mubu outline conversion (headings, nested bullets,
  `- [x]` checkboxes, `> notes`).
- `mubu-web-mcp setup` / `install` / `login` / `logout` / `doctor` / `selftest` CLI, plus an
  installer that writes MCP config for Codex, Claude Desktop, Claude Code, Cursor, Windsurf,
  VS Code and Cherry Studio (with backups, idempotent, `--dry-run`).
- Credential storage per platform: Windows DPAPI, macOS Keychain, Linux Secret Service,
  plaintext file with `0600` permissions as a last resort.
- Read-only mode (`MUBU_READ_ONLY=1` or `--read-only`) that hides all create tools.
- Politeness controls: minimum interval between requests, retries with backoff on 5xx and
  network errors, atomic token-cache writes.
- Human-readable hints for the API error codes seen in practice (`2`, `6`, `17`, `1204`).
- 64 offline unit tests and a GitHub Actions matrix (Linux/macOS/Windows, Python 3.10-3.13).
- Documentation: English and Chinese READMEs, `AGENTS.md` for AI agents, per-client setup
  prompts, and reverse-engineering notes for the unofficial API.

### Fixed during development

- Reading document ids from `data.id` (the API does not return `{"doc": {"id": ...}}`).
- Creating documents with content: `/list/create_doc` ignores `content`, so creation with
  content goes through `/list/import_doc` instead.
- Duplicate title when the document name and the first Markdown heading are identical.
- Cross-platform import failure caused by a top-level `ctypes.wintypes` import (a Windows-only
  module) - DPAPI is now loaded lazily, on Windows only.

### Known limitations

- Unofficial API; may break when Mubu ships a new web client.
- No delete / rename / move / update-existing-document tools, by design.
- Collapsed state, ordered-list numbering, images and attachments are outside the Markdown
  fidelity range.
