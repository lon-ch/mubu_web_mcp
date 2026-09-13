# mubu_web_mcp

> Let your AI agent (Codex, Claude, Cursor, Windsurf…) read and create your [Mubu / 幕布](https://mubu.com) outlines through the Model Context Protocol.

[![CI](https://github.com/lon-ch/mubu_web_mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/lon-ch/mubu_web_mcp/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Maintained by **miaoteam**. 中文文档见 [README.zh-CN.md](README.zh-CN.md)。

---

## ⚠️ Read this first

* **This is an unofficial integration.** It is not affiliated with, endorsed by, or sponsored by 深圳市十里湖科技有限公司 (the company behind Mubu / 幕布).
* It talks to the **same internal HTTP API the Mubu web app uses** (`api2.mubu.com/v3/api`), authenticated with **your own Mubu phone number and password**. No OAuth screen, no official developer program.
* The [Mubu Terms of Service](https://mubu.com/agreement) prohibit signing in or using the service through third-party tools that are not authorized by the vendor. Using this project is therefore **at your own risk** — the vendor may rate-limit, suspend or terminate the account involved.
* Practically speaking: low-volume, personal use of your own data is what this is designed for. Do not use it for bulk exporting, scraping, or anything resembling abuse of the service.
* Because of that, this project deliberately **ships no destructive capability**: there is no tool to delete, rename, move, or overwrite your existing documents. The only write operation creates a **new** document.

If any of that is a problem for you, please use the [official MCP integration](https://mubu.com/help/124) instead (it requires a paid membership).

---

## What it does

Give an MCP-capable AI agent these tools and it can, in a normal conversation:

| Tool | What it does |
| --- | --- |
| `mubu_whoami` | Show the signed-in account and login state |
| `mubu_list` | List folders and documents in a folder (**text + structured data**) |
| `mubu_get_doc` | Read a document as a Markdown outline, or complete JSON |
| `mubu_get_doc_json` | Structured read with **cursor paging** for large documents |
| `mubu_search` | Search folder/document names, optionally inside document bodies |
| `mubu_inspect` | **Redacted structure report** (field names, counts, suspected image/link fields) |
| `mubu_diagnostics` | Request/retry/rate-limit counters and last error code |
| `mubu_create_doc` | Create a **new** document from Markdown (headings, nested bullets, `- [x]` checkboxes, `> notes`) |
| `mubu_create_folder` | Create a new folder |

Typical use: *"summarise this discussion into an outline and save it to Mubu"*, or *"find the note I wrote about X and use it as context"*.

Markdown ↔ Mubu conversion is round-trip stable: import a document, read it back, and you get byte-identical Markdown for headings, nesting, checkboxes and notes.

### Structured output

The tools that are meant to be consumed by programs return `structuredContent` with a declared
`outputSchema`, so nothing has to parse human-facing Chinese text:

```json
{
  "folderId": "0",
  "folders": [
    {"id": "f1", "name": "工作", "parentId": "0", "order": 0, "updatedAt": 1789301039119,
     "type": "folder", "updateTime": 1789301039119}
  ],
  "documents": [
    {"id": "d1", "name": "会议记录", "parentId": "0", "order": 2, "updatedAt": 1789301039481,
     "type": "document", "updateTime": 1789301039481}
  ]
}
```

Raw API fields are preserved; `parentId`, `order`, `updatedAt` and `type` are added on top.

Large documents use `mubu_get_doc_json` with a cursor instead of ever returning truncated JSON:

```json
{"docId": "d1", "totalTopLevelNodes": 120, "offset": 0, "limit": 20,
 "hasMore": true, "nextCursor": "20", "nodes": [ ... ]}
```

---

## Local backup (content never touches the model)

The MCP tools are for low-frequency chat queries, where whatever is read ends up in the model's
context. For backing up an account, use the CLI instead — it reads from Mubu and writes to your
disk, with no model in the loop:

```bash
mubu-web-mcp backup --out ~/mubu-backup                 # incremental, resumable
mubu-web-mcp backup --out ~/mubu-backup --folder f1     # scope to one folder
mubu-web-mcp backup --out ~/mubu-backup --dry-run       # list what would be backed up
mubu-web-mcp backup --out ~/mubu-backup --assets        # also fetch images (experimental)
```

What it gives you:

* recursive folder index, with `--depth` / `--max-folders` / `--max-docs` limits;
* **incremental**: a document whose `updateTime` is unchanged is skipped without even fetching
  it (one listing call per folder is all it costs);
* **resumable**: progress is written after every folder, so `Ctrl+C` + rerun continues where it
  stopped;
* Markdown files plus a `manifest.json` with per-document size, SHA-256, version and asset list;
* a conservative default interval of 2 s, and it is read-only by construction — no delete,
  rename, move or overwrite path exists.

Assets (`--assets`) are experimental and restricted to `*.mubu.com` hostnames, re-validated
after redirects; the JWT is never sent to a non-Mubu host.

---

## Quick start

### 1. Install

```bash
pipx install git+https://github.com/lon-ch/mubu_web_mcp
# or: uv tool install git+https://github.com/lon-ch/mubu_web_mcp
# or from a clone: pip install -e .
```

No third-party runtime dependencies — standard library only.

### 2. Sign in

```bash
mubu-web-mcp login      # asks for your Mubu phone number + password
```

Credentials are stored with the best mechanism your OS offers (see [Security](#security)).

### 3. Register the server with your AI client

```bash
mubu-web-mcp setup                 # detect installed clients, write config, verify
mubu-web-mcp setup --agent codex   # or target one client
```

Then restart the client (for Codex / Claude Code, start a new session).

### 4. Verify

```bash
mubu-web-mcp doctor     # credentials, login, and root folder listing
```

---

## Let your agent do the setup

Paste this into your agent and it can install and configure everything itself:

```text
Install the MCP server at https://github.com/lon-ch/mubu_web_mcp for me and connect it
to your own tool list.
Steps: (1) pipx install git+https://github.com/lon-ch/mubu_web_mcp
(2) ask me to run `mubu-web-mcp login` myself in a terminal — never ask me for my
password, and never put it in a command line or a config file
(3) `mubu-web-mcp setup --agent all --yes`
(4) `mubu-web-mcp doctor` and show me the result.
Follow https://github.com/lon-ch/mubu_web_mcp/blob/main/AGENTS.md for the details.
```

See [AGENTS.md](AGENTS.md) for the full instructions written for AI agents, and
[docs/agent-setup.md](docs/agent-setup.md) for a copy-paste prompt per client.

---

## Supported clients

`mubu-web-mcp install --agent <name>` writes the right config for:

| Agent | Config file it writes |
| --- | --- |
| `codex` | `~/.codex/config.toml` |
| `claude-desktop` | `claude_desktop_config.json` (macOS / Windows / Linux path) |
| `claude-code` | via `claude mcp add` |
| `cursor` | `~/.cursor/mcp.json` |
| `windsurf` | `~/.codeium/windsurf/mcp_config.json` |
| `vscode` | `.vscode/mcp.json` in the current project |
| `cherry-studio` | prints a JSON snippet to paste into the GUI |

Existing files are backed up to `<file>.bak` before being modified, and the writes are
idempotent. `--dry-run` shows what would change without touching anything.

---

## Read-only mode

If you only want the agent to *read* your notes:

```bash
mubu-web-mcp install --agent codex --read-only
# or just set MUBU_READ_ONLY=1 in the server's environment
```

In read-only mode the create tools are not even advertised to the agent.

---

## Security

**Credentials never leave your machine**, except as part of the login request to Mubu itself.

* Storage per platform:
  * **Windows** — DPAPI-encrypted file at `~/.mubu/credentials.dpapi` (key bound to your Windows user).
  * **macOS** — the system Keychain (`security add-generic-password`).
  * **Linux** — Secret Service via `secret-tool`.
  * **Fallback** — `~/.mubu/credentials.json` with `0600` permissions, and the CLI tells you loudly when it falls back.
* The session token is cached in `~/.mubu/token.json` (about 2 hours, refreshed automatically).
* The only network destination in the code is `https://api2.mubu.com/v3/api`; the host name is hard-coded and re-checked before every request. There is no telemetry and no third-party logging.
* `mubu-web-mcp logout` deletes everything that was stored locally.

What this project **cannot** protect you from: a cloud AI agent will send whatever it *reads*
to its model provider. If a document is sensitive, don't ask the agent to read it — or use
the CLI directly: `mubu-web-mcp doctor`, or drive the API from your own script.

For the full threat model see [SECURITY.md](SECURITY.md).

---

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `MUBU_PHONE` / `MUBU_PASSWORD` | — | Credentials; take priority over stored ones |
| `MUBU_READ_ONLY` | off | `1` disables all create tools |
| `MUBU_HOME` | `~/.mubu` | Where credentials and the token cache live |
| `MUBU_TIMEOUT` | `20` | HTTP timeout in seconds |
| `MUBU_MIN_INTERVAL_MS` | `500` | Minimum delay between two requests; shared across processes |
| `MUBU_JITTER_MS` | `150` | Random jitter added to the interval / backoff |
| `MUBU_MAX_RETRIES` | `2` | Retries for network errors, 5xx and rate limits |
| `MUBU_MAX_BACKOFF_SECONDS` | `60` | Cap for any single wait (including `Retry-After`) |
| `MUBU_PROCESS_LOCK` | on | Share the rate limit between processes via a file lock |
| `MUBU_RATE_LIMIT_CODES` | empty | Extra business codes (inside HTTP 200) treated as rate limiting |

---

## How it works

The project talks to Mubu's web API directly. A few things were reverse-engineered and are
documented in [docs/mubu-api-notes.md](docs/mubu-api-notes.md), most importantly:

* `POST /list/create_doc` **ignores** its `content` parameter — you cannot write a document's
  body that way (several third-party projects on GitHub get this wrong).
* Writing the body requires `POST /list/import_doc` with
  `{name, folderId, itemCount, define}` where `define` is a JSON string of `{"nodes": [...]}`,
  and the Markdown is parsed into nodes client-side.
* Every request needs `Jwt-Token`, `data-unique-id`, `x-session-id`, `x-request-id` and
  `x-reg-entrance` headers, otherwise some endpoints answer `illegal request`.
* Overwriting an existing document uses a WebSocket collaboration protocol, which this project
  intentionally does not implement.

---

## Limitations

* Unofficial API: it can break whenever Mubu changes its web client.
* No editing/updating of existing documents, no delete/rename/move.
* Markdown fidelity covers headings, nested bullets, checkboxes and notes. Collapsed state,
  ordered-list numbering, images and attachments are out of scope.
* `mubu_search` walks folders locally (depth ≤ 3, ≤ 100 folders, ≤ 300 documents), so it is
  not a full-text index.
* Accounts with encrypted documents: encrypted documents can't be read and don't show up in
  listings.

---

## Development

```bash
python -m unittest discover -s tests   # 64 tests, no network needed
ruff check src tests                   # lint
mubu-web-mcp selftest                  # offline smoke test
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

For the detailed test/verification report (including what is verified and what is still
pending), see [docs/verification.md](docs/verification.md).

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
