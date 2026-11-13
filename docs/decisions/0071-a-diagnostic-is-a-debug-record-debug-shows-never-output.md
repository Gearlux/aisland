# A diagnostic is a debug record `--debug` shows, never output (2026-10-30)

## Context

aisland had no logger: every library module reported with `click.echo` (about eighty lines in `jenkins.py` alone), so
a line that only described aisland's own mechanics — `Using cached tarball …`, `Verified … (sha256 OK).`, `All
required Jenkins plugins are already installed.` — could not be turned down, and the decisions that printed nothing
(which workspace root, which template set, which `git` and install commands) could not be turned up.

## Decision

Each module that needs one has `logger = logging.getLogger(__name__)` (the standard library: aisland depends on no
logging package). `aisland --debug`, through its option's callback, adds one handler to the `aisland` logger for the
run — `aisland: <module>: <message>` on stderr, written through click — and removes it, the level restored, when the
run ends; without the flag nothing is configured and nothing below WARNING shows. Eight `click.echo` lines that no
one acts on moved to debug; every result, progress line, question and warning stayed output. New debug lines name the
workspace root and the rule that found it (once per answer), the template set and its variables, each `gitcmd` call
with its exit code, each install command, and the `pyproject.toml` and `.gitmodules` read.

## Consequences

A plain run prints what it printed, minus the eight lines; `--debug` adds the traceback and the debug log. A host that
configures logging itself (pytest's `caplog`) receives the records without `--debug`, so a moved line is tested at
DEBUG. `aisland --help` imports nothing new: `logging` was already loaded.

## Example

```bash
aisland --debug workspace show
# aisland: locations: workspace root /home/me/acme (from the checkout: it holds .gitmodules and TASKS.md)
# aisland: gitmodules: read /home/me/acme/.gitmodules: 3 submodule(s)
# aisland: pyproject: reading /home/me/acme/models/pyproject.toml
```

## What you may change

Move a line to debug when no one acts on it, or a debug line back to output when someone does; add debug lines at
silent decisions. Never print a diagnostic with `click.echo`, `print` or at `warning`/`info`, and never add a second
handler or a logging dependency.

---

[The architecture and every decision](../architecture.md#the-decisions)
