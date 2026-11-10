# The commands print the real install line, and every command is on a page (2026-10-28)

## Context

A finding of the release review's second pass: the install command `init` and the venv error printed was a
placeholder (`<the aisland wheel>`); twelve commands were on no page; the README listed no requirement but Python and
no environment variable.

## Decision

The commands print `pip install aisland`. The pages show every command, and the README the requirements and the
variables, each pinned: `tests/test_docs.py::test_every_command_is_shown_on_a_page` and
`::test_every_variable_aisland_reads_is_in_the_readme` (against `env.complete`).

## Consequences

A new command, or a new variable a person sets, fails the docs tests until a page shows it.

## Example

```text
$ aisland workspace init acme          # aisland not installed in acme/.venv
...
Next:
    cd /home/you/acme
    uv venv --seed .venv && .venv/bin/pip install aisland
    .venv/bin/aisland setup
```

## What you may change

- The wording of the lines, and which page shows a command.
- Never: a placeholder in a line a person copies, or a command no page shows.

---

[The architecture and every decision](../architecture.md#the-decisions)
