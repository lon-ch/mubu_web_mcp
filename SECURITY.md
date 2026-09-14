# Security

## Reporting a vulnerability

Please use GitHub's private security advisory feature (Security tab, "Report a vulnerability")
instead of a public issue, or contact the maintainers through the address listed in the
repository profile. Include reproduction steps and the affected version.

## What this project stores, and where

| Item | Location | Protection |
| --- | --- | --- |
| Mubu phone number and password | Windows: `~/.mubu/credentials.dpapi` | DPAPI, bound to the Windows user account |
| | macOS: system Keychain | Keychain access control |
| | Linux: Secret Service via `secret-tool` | Desktop keyring |
| | Fallback: `~/.mubu/credentials.json` | Filesystem permissions `0600` only |
| Session token (JWT, about 2 hours) | Same OS secret store as the credentials (DPAPI / Keychain / Secret Service) | `~/.mubu/token.json` is written only as a `0600` fallback when no system store exists |
| Environment override | `MUBU_PHONE` / `MUBU_PASSWORD` | Whatever your shell environment provides |

`mubu-web-mcp logout` removes the credential material from every backend it can reach. The token
file is written atomically (temporary file plus replace) so concurrent processes cannot corrupt
it.

## Network behaviour

* The only outbound destination in the code is `https://api2.mubu.com/v3/api`. The host name is
  hard-coded in `mubu_client.py` and re-checked immediately before each request; a URL that does
  not start with that prefix is refused.
* There is no telemetry, analytics, crash reporting or third-party logging.
* The only data sent is what the requested operation needs: credentials at login, and folder or
  document ids plus Markdown content when creating a document.
* `tests/test_client.py` asserts the "refuse non-Mubu host" behaviour, so a regression fails CI.

## Threat model

### In scope

* Local credential theft: mitigated by the OS keychain or DPAPI where available. The plaintext
  fallback is announced loudly by the CLI.
* Accidental data exfiltration by the code itself: mitigated by the hard-coded host check and by
  having no dependencies at all, so there is no supply chain to trust beyond this repository.
* Destructive actions by an AI agent: mitigated by shipping no delete, rename, move or
  overwrite tools. The only write operations create new documents or folders.

### Out of scope, and cannot be mitigated here

* A cloud-hosted AI agent sends whatever it reads to its model provider. If an agent calls
  `mubu_get_doc`, that document's text leaves the machine. Use read-only mode selectively, avoid
  pointing agents at sensitive documents, or drive the client library from a local script.
* A compromised machine, a keylogger, or malware running as the same user. Such software can read
  the token cache and, on Windows, decrypt DPAPI data as that user. This is a property of DPAPI
  itself, not of this project.
* Mubu's own service: the vendor sees every request, exactly as it does for their web client.
* Anything the user does with exported data afterwards.

## Using your own Mubu account safely

* Treat the account as exposed to the vendor's terms of service; see the disclaimer in the
  README. Prefer a low-value account for experiments.
* Keep `MUBU_MIN_INTERVAL_MS` at its default or higher. Bulk operations are both rude and the
  most likely thing to attract rate limiting.
* Use `--read-only` when the agent does not need to write.
