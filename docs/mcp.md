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
command `aisland-mcp` to your client's server list yourself. It works on the workspace the folder it starts in
belongs to; a client that starts servers elsewhere (its own folder, your home folder) needs the workspace named in
the entry's environment:

```json
{"mcpServers": {"aisland": {"command": "/Users/you/work/acme/.venv/bin/aisland-mcp",
                            "env": {"AISLAND_WORKSPACE": "/Users/you/work/acme"}}}}
```

| tool | what it does |
| --- | --- |
| `doctor` | `aisland doctor`'s checks: each one's result and the command that fixes it |
| `tests_report(project, days)` | `aisland tests report PROJECT --json` |
| `sources` | `aisland workspace configure source`'s rows |
| `services_status` | every service of the workspace: its state, PID, ports (`aisland services status --json`) |
| `tunnel_list` | `aisland tunnel list --json` |
| `tunnel_status` | `aisland tunnel status --json`: whether the tunnel to this machine runs, its name, account and address |
| `track_status(projects)` | `aisland workspace track status --json`: per project whether the root records its commits, whose choice that is and whether git agrees; `track set\|unset` stay commands |
| `editor_themes`, `editor_colors(part)` | the VS Code themes here, the CSS colour names, this window's choice |
| `editor_set_theme(name)`, `editor_set_color(role, color, area)`, `editor_unset(what)` | `aisland workspace theme\|color set\|unset` |
| `service_start(name)`, `service_stop(name)`, `service_restart(name)` | `aisland <name> start\|stop\|restart` for any service of the workspace — aisland's own or one a project brings — in the background where it runs under the supervisor |
| `tunnel_start(name, provider, cli)`, `tunnel_stop()` | `aisland tunnel start\|stop --json`; a sign-in still pending is answered at once with the code to hand over |

Every tool answers with one JSON object, and a refusal is the tool's error in the command's own
words (`no installed VS Code theme is named 'Solarzed Dark' (did you mean 'Solarized Dark'?)`).
A warning the command would print on stderr comes back in the object as `warnings`, a list of lines (the key is
there only when there is one); a question the command would ask is refused, since no one is there to answer it.
The first nine only read; the others change this machine and are marked as such for the
client. What prompts, runs for minutes or deletes — `setup`, `clean`, `backups prune`, the
`reset` commands, `jenkins init`, `source set` — is not a tool: run it yourself. Without the
extra, `aisland-mcp` stops with `Error: aisland-mcp needs the mcp extra: pip install 'aisland[mcp]'`.
