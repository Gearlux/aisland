# MCP

How aisland registers the MCP servers of a workspace's projects with MCP clients, and the MCP server aisland ships itself.

## MCP client configuration (`aisland mcp`)

Every workspace project that ships an MCP server is registered with the MCP clients in use,
from ONE discovery: a `[project.scripts]` entry targeting `<pkg>.server:main` whose module
imports the SDK's server package (`mcp.server`). The commands are absolute venv paths, which
is why both files are gitignored and regenerated per machine — `aisland setup` writes them
after the editable installs, and each has a standalone command:

```bash
aisland mcp gemini    # .gemini/settings.json — the Gemini CLI
aisland mcp claude    # .mcp.json            — Claude Code's project-scoped servers
```

```json
{"mcpServers": {"models-mcp": {"command": "/…/bin/bash",
                           "args": ["-c", "source \"$0\" >/dev/null; exec \"$@\"",
                                    "/…/project.bashrc", "/…/.venv/bin/models-mcp"]},
                "...": {}}}
```

A client starts a server with its own environment, not the workspace's. So each server is
started through bash, which first sources the workspace's `project.bashrc` — the venv,
`PROJECT_ROOT` and every `.env` setting, as in a terminal — with its output discarded (the
server's stdout is its protocol stream), then becomes the server. `.env`'s values are never
written into the client file. Without a `project.bashrc` (or without bash) a server is
started directly, `{"command": "/…/.venv/bin/models-mcp"}`. A change to `.env` reaches a
server when the client restarts it.

Claude Code reads `.mcp.json` when a session starts in the workspace and asks once whether
to use the project's servers (`enableAllProjectMcpServers` in its settings skips the prompt);
`claude mcp list` then shows them. A server whose backing service is not running still starts
— its tools answer in-band with the command that starts the service.

### aisland's own server (`aisland-mcp`)

aisland ships an MCP server too, so an AI client can check and drive this workspace. It is the
separate console script `aisland-mcp` and needs the `mcp` extra:

```bash
pip install 'aisland[mcp]'
```

When aisland is itself one of the workspace's projects (a checkout in the workspace folder),
`aisland setup` (or `aisland mcp claude`) lists it as `aisland` in `.mcp.json`, like any other
project's server. When aisland is installed from PyPI there is no project to discover: add the
command `aisland-mcp` to your client's server list yourself.

| tool | what it does |
| --- | --- |
| `doctor` | `aisland doctor`'s checks: each one's result and the command that fixes it |
| `tests_report(project, days)` | `aisland tests report PROJECT --json` |
| `sources` | `aisland workspace configure source`'s rows |
| `services_status` | every supervised service: running, PID, ports |
| `tunnel_list` | `aisland tunnel list --json` |
| `editor_themes`, `editor_colors(part)` | the VS Code themes here, the CSS colour names, this window's choice |
| `editor_set_theme(name)`, `editor_set_color(role, color, area)`, `editor_unset(what)` | `aisland workspace theme\|color set\|unset` |
| `service_start(name)`, `service_stop(name)`, `service_restart(name)` | `aisland jenkins\|mlflow start\|stop\|restart`, in the background |

Every tool answers with one JSON object, and a refusal is the tool's error in the command's own
words (`no installed VS Code theme is named 'Solarzed Dark' (did you mean 'Solarized Dark'?)`).
The first seven only read; the others change this machine and are marked as such for the
client. What prompts, runs for minutes or deletes — `setup`, `clean`, `backups prune`, the
`reset` commands, `jenkins init`, `source set` — is not a tool: run it yourself. Without the
extra, `aisland-mcp` stops with `Error: aisland-mcp needs the mcp extra: pip install 'aisland[mcp]'`.
