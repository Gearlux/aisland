# Every MCP server is started with the workspace's environment (2026-10-21)

## Context

A client (Claude Code, the desktop app, the Gemini CLI) starts each server in `.mcp.json` /
`.gemini/settings.json` as a subprocess with the client's OWN environment. The workspace's
`.env` reaches a terminal only through `source project.bashrc`, so a server never saw it:
measured on this machine, the desktop app's app-mcp ran with `DATA_ROOT`, `MODEL_ROOT` and
`RUN_DIR` unset, and a download that names no folder — which goes under those roots — landed
wherever the server had started.

## Decision

`mcp_config._with_workspace_env` writes each discovered server as

```json
{"command": "<bash>", "args": ["-c", "source \"$0\" >/dev/null; exec \"$@\"", "<root>/project.bashrc", "<server>"]}
```

bash sources the same shell init a terminal does (venv, `PROJECT_ROOT`, `.env`, `PATH`) with its
stdout discarded, then `exec`s the server, which inherits all of it. Without a root
`project.bashrc` (a workspace before its first setup) or without bash on PATH, the plain command,
as before. Discovery and the missing-console-script warning still read the plain map.

## Consequences

- A server sees exactly what a run from a terminal sees, and `.env` stays the one place the
  values live — copying them into the client file's `"env"` would also put `.env`'s secrets in a
  second file.
- Nothing the init prints can corrupt the stdio protocol stream; its stderr still reaches the
  client's log.
- A change to `.env` reaches a server when the client restarts it; `user.bashrc` runs too.
- Starting costs one bash and the init, about 0.05 s here.

## Example

```python
from pathlib import Path
from aisland import mcp_config

root = Path("~/acme").expanduser()
mcp_config._with_workspace_env(root, {"app": {"command": f"{root}/.venv/bin/app-mcp"}})
# {"app": {"command": "/opt/homebrew/bin/bash",
#             "args": ["-c", 'source "$0" >/dev/null; exec "$@"', f"{root}/project.bashrc", f"{root}/.venv/bin/app-mcp"]}}
```

## What you may change

- The program bash runs (`_WITH_WORKSPACE_ENV`), with
  `tests/test_mcp_config.py::TestTheServersGetTheWorkspaceEnvironment`.
- Never: writing `.env`'s values into a client file, or letting the init's stdout through.

---

[The architecture and every decision](../architecture.md#the-decisions)
