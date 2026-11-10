# An extra a project marks is installed only when named (2026-10-23)

*2026-10-23* [frameworks]

## Context

`aisland setup` offered every extra and took them all on a blank answer, `-y` or `--all`. One project's extra pinned
two dependencies to their remote branch by URL — meant for a machine with no checkout of them — and a blank answer
installed it in a workspace that had both checked out: uv replaced the local checkouts with the remote branch.

## Decision

A project marks such extras in `[tool.aisland] manual-extras`. They stay on the menu, marked `(only when named)`, but
a blank answer, `-y` and `--all` leave them out; naming one (at the prompt or with `aisland workspace configure
extras set`) installs it. The maintainer chose the declaration over a structural rule (leave out an extra that pins a
workspace project by URL): the project knows what its extra is for, and the declaration holds in every workspace.

## Consequences

- An extra marked by one project is left out for every project offering that name.
- Discovery refuses a marked name that is no extra of the project, naming the file and the line, before setup
  installs anything.

## Example

```toml
[project.optional-dependencies]
remote = ["acme-core @ git+https://example.com/acme/core.git@dev/main"]

[tool.aisland]
manual-extras = ["remote"]
```

## What you may change

- The menu's marker and the prompt's wording.
- Never: a marked extra taken by a blank answer, `-y` or `--all`.

---

[The architecture and every decision](../architecture.md#the-decisions)
