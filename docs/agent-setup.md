# Letting an agent configure this for you

Copy the block for your client into the chat. Each one tells the agent to install the server,
to have **you** run the login command yourself, and to verify the result.

## Generic (works with any agent)

```text
Install the MCP server at https://github.com/lon-ch/mubu_web_mcp and connect it to your
own tool list.
Steps:
1. pipx install git+https://github.com/lon-ch/mubu_web_mcp
2. Tell me to run `mubu-web-mcp login` myself in a terminal. Do not ask me for my password,
   and do not put it in any command line or config file.
3. mubu-web-mcp setup --agent all --yes
4. mubu-web-mcp doctor — show me the output.
5. Tell me to restart you / start a new session so the mubu_* tools show up.
Follow AGENTS.md from the repository for the details.
```

## Codex

```text
Please install https://github.com/lon-ch/mubu_web_mcp and wire it into Codex:
run `pipx install git+https://github.com/lon-ch/mubu_web_mcp`,
ask me to run `mubu-web-mcp login` in my own terminal,
then run `mubu-web-mcp setup --agent codex --yes` and `mubu-web-mcp doctor`.
If you can't write ~/.codex/config.toml, show me the exact block to paste instead.
```

The installer writes this into `~/.codex/config.toml`:

```toml
[mcp_servers.mubu_web_mcp]
command = "/path/to/python"
args = ["-m", "mubu_web_mcp"]
```

## Claude Desktop / Cursor / Windsurf / VS Code

```text
Install https://github.com/lon-ch/mubu_web_mcp for me, then run
`mubu-web-mcp setup --agent <claude-desktop|cursor|windsurf|vscode> --yes`
so the MCP server gets registered, and show me `mubu-web-mcp doctor` output.
Ask me to run `mubu-web-mcp login` in my own terminal — never ask me for the password.
```

## Choosing read-only

Add this sentence to any prompt above if the agent should only *read* your notes:

```text
Register it in read-only mode (`--read-only`), so you cannot create documents.
```

## Manual configuration, if the installer cannot help

```json
{
  "mcpServers": {
    "mubu_web_mcp": {
      "command": "python",
      "args": ["-m", "mubu_web_mcp"]
    }
  }
}
```

`mubu-web-mcp install --agent cherry-studio` prints exactly this snippet with the correct
absolute path for your machine.
