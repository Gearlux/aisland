# aisland's tools are its own MCP server, a chosen set (2026-10-21)

## Context

An AI client working in the workspace should check the environment, read test statistics, start Jenkins or MLflow
and set the editor's colours without parsing terminal text. Another project of the workspace serves its libraries'
tools over MCP; the maintainer ruled that aisland's commands never become that server's tools — aisland runs where that
project is not installed, and it installs the projects that server needs. liquifai, which turns
one signature into a command and a tool, is out for the same reason: `aisland setup` installs it.

## Decision

`aisland/server.py` is a stdio MCP server, `aisland-mcp`, behind the `[mcp]` extra. Its tools are a list chosen with
the user, not the CLI generated: seven that read and six that change this machine without destroying anything
(the editor's theme and colours, starting, stopping and restarting a service — Jenkins and MLflow only until
2026-10-29, any service of the workspace since: record "The MCP service tools take any service of the workspace").
Each returns one JSON object: the data
its command prints with `--json`, from the same function (`doctor.run_checks`, `stats_report.build_report`,
`sources.describe`, `tunnel.store.store_payload`, `editor.*`). A service tool runs the `aisland` command as a subprocess
(this interpreter, `-P`, the workspace root), so it starts a service exactly as a person does. The server drops
the resources/prompts handlers so it advertises tools only; it needs no schema sanitizing (flat arguments), so it
is not a copy of the `_LLMSafeMCPServer` the corelib-based servers share.

## Consequences

- What prompts, runs for minutes or deletes stays a command; an agent cannot `clean` or `reset`.
- `.mcp.json` lists the server through the existing discovery — no registration code.
- A print in a tool lands on stderr: the SDK's stdio transport serves the protocol from a private copy of stdout.
- The base install is unchanged: without the extra, `aisland-mcp` says what to install.

## Example

```text
→ tools/call editor_set_color {"role": "background", "color": "navy"}
← {"theme": "Monokai Pro", "background": "navy", "foreground": "white", "area": "frame"}
→ tools/call service_start {"name": "clearml"}
← error: no service 'clearml' (the services: docker, jenkins, swiftbar)
```

## What you may change

- Add a tool: a function returning one JSON object, in `_READING` or `_CHANGING`, its command's `--json` the same
  data; `tests/test_server.py::READ_ONLY`/`CHANGING` name it.
- What must not change: no tool that prompts, runs for minutes or deletes; the extra stays optional; aisland's
  tools stay out of that server.

## The tools that end something are marked destructive (2026-11-07)

A finding of the release review's third pass: `service_stop`, `service_restart` and `tunnel_stop` said
`destructive_hint=False`, so a client called them without asking, yet stopping `docker` stops every container in it
and stopping the tunnel ends every remote session. They are now their own group, `_STOPPING`, annotated
`destructive_hint=True`; the others of `_CHANGING` still destroy nothing. `tunnel_start` lost its `cli` argument: a VS
Code CLI archive of one's own is installed and run unchecked (the download policy's checksum is skipped for it), so it
stays `aisland tunnel start --cli`, a person's choice at the command line. `tests/test_server.py::STOPPING` names
them.

---

[The architecture and every decision](../architecture.md#the-decisions)
