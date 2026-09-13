# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
