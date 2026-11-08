# A clone the workspace does not list: setup adds it as a submodule

*2026-10-23* [clones]

## Context

In a workspace of submodules, a repository cloned into its folder by hand was invisible: discovery and
`workspace.toml` read `.gitmodules` only, so setup neither listed nor installed it, and said nothing.

## Decision

`aisland setup` finds each clone `.gitmodules` does not list (a `.git` folder, an `origin`, a `pyproject.toml`) and runs
`git submodule add -b <its branch> <its origin> <folder>`, which keeps the clone as it is — without asking (decided
2026-10-23: a question about a project the workspace already lists in `workspace.toml` was the wrong default). The new project is tracked as the workspace says: `[workspace] track = false` in `workspace.toml` (`init --untracked`,
`track unset` with no project named) untracks every project without a `track` of its own, those added later included —
a workspace-wide choice, never inferred from the other projects (corrected 2026-10-23: the first rule, "like the
others", left a workspace made untracked with its new projects tracked).

## Consequences

- A clone is installed and listed by the same setup run; a `--local` root commits it.
- A data repository (no `pyproject.toml`) or a scratch one (no `origin`) is never offered.

## Example

```text
  - added foo as a submodule (https://host/team/foo.git, dev/main)
```

## What you may change

- The report line's wording.
- Never: a question before adding, or moving the clone's checkout.

---

[The architecture and every decision](../architecture.md#the-decisions)
