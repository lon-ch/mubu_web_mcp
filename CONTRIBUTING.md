# Contributing

Thanks for helping. A few ground rules keep this project useful and safe.

## Ground rules

* **No destructive capabilities.** Pull requests adding delete, rename, move, or
  update-existing-document tools will be declined. The value of this project is that an AI agent
  can be pointed at a real account without being able to destroy anything.
* **No bulk or export features designed to scrape an account.** Same reason.
* **Standard library only.** The zero-dependency property is a security feature: there is
  nothing to audit but the code in this repository. If a change seems to need a dependency,
  open an issue first.
* **Never commit credentials, tokens, or captured API responses that contain personal data.**

## Development setup

```bash
git clone https://github.com/lon-ch/mubu_web_mcp
cd mubu_web_mcp
python -m pip install -e .
python -m unittest discover -s tests
```

The test suite runs completely offline; it never touches a real Mubu account. If you need to
place temporary directories somewhere else (restricted environments), set `MUBU_TEST_TMPDIR`.

## Tests

* Every behaviour change needs a test. Prefer faking `urllib.request.urlopen`
  (see `tests/test_client.py`) over hitting the network.
* New endpoints or API quirks must be documented in `docs/mubu-api-notes.md` in the same pull
  request.
* CI runs the suite on Linux, macOS and Windows for Python 3.10 through 3.13.

## Style

* `ruff check src tests` must pass (line length 100).
* MCP tool output stays in Chinese, since the user base is Chinese. Code comments and commit
  messages are fine in either language; English is preferred for anything a non-Chinese
  maintainer has to read.

## Pull requests

* One logical change per pull request, with a short description of the user-visible effect.
* Call out API behaviour changes explicitly: Mubu changes break this project, and the notes in
  `docs/mubu-api-notes.md` are how we recover.
