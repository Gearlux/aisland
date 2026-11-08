# A command that prints an `Error:` line exits 1 (2026-10-27)

[failures]

## Context

About forty places printed `Error: …` (or `  - Error: …`) with `click.echo` and returned, so the command ended with
exit 0: `aisland jenkins scaffold no-such-project`, `aisland setup` after a project failed to install, `aisland check`
without a `.venv`, a service whose port was taken. A script, CI or the MCP server's `aisland` call saw success. Some
of those failures must not end the command — setup installs every project, `services start` starts the others — so
raising everywhere would have changed what the commands do.

## Decision

Two kinds. A failure that ends the command raises `click.ClickException`: one `Error:` line on stderr, exit 1. A
failure the command goes on past calls `aisland.failures.report(line)`: the line goes to stderr, the command carries
on, and the top-level group's `invoke` turns a run that reported any into exit 1 — also over the command's own
`SystemExit(0)`; a non-zero code of its own is kept. The count is reset as each command starts.

## Consequences

- Those lines move from stdout to stderr.
- `aisland check` with a gate tool missing fails that gate (`isort is not installed in …`) instead of a traceback.
- `aisland supervisor down` that cannot stop the supervisor raises.

## Example

```python
from aisland import failures

for project in projects:
    try:
        install(project)
    except subprocess.CalledProcessError as error:
        failures.report(f"  - Error: Failed to install {project}: {error}")   # setup goes on; the run exits 1
```

## What you may change

- Which failures end a command and which it goes on past.
- Never: an `Error:` line printed with `click.echo` and a return, or a library `sys.exit`.

---

[The architecture and every decision](../architecture.md#the-decisions)
