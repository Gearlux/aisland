# `--force` builds a dirty project from its working tree, and says so (2026-10-20)

## Context

A wheel is built from its project's last commit, and a project with changes no commit holds is
refused, so no one ships a change believing it is in. In a workspace where several sessions work at
once that refusal blocks every build that covers a project someone else is editing: a run over every
project with a checkout stopped on three projects with work in progress, though the person building
wanted exactly that work on the other machine.

## Decision

`aisland wheels build … --force` (chosen 2026-10-20) builds such a project from its files as they
are (`export_worktree`), not from its commit:

- the files are the ones git tracks plus the untracked ones it does not ignore
  (`git ls-files --cached --others --exclude-standard`): an edit and a new module are in, a deleted file
  is out, and an ignored setuptools `build/lib` stays out — the reason wheels were never built from the
  bare folder still holds;
- its commit is written `<sha>+uncommitted` wherever a commit is written (the report's table,
  `wheels.yaml`, the folder's README), and its `git status --porcelain` lines go into the report
  (`BuiltWheel.uncommitted`, a text block "Built with changes no commit holds");
- a clean project is still exported from `git archive HEAD`, so `--force` on a clean workspace builds
  the same files as a run without it;
- under `--force` a dirty target is exported from its working tree too, so the import check reaches its
  new modules (it is installed editable from its folder anyway);
- `--force --commit` is refused before anything is built: a committed wheel must be one its commit
  reproduces.

Rejected: building the last commit and leaving the changes out. It keeps every wheel reproducible, but
the person who forces a build wants the work in progress shipped; leaving it out silently is the
mistake the refusal exists to prevent.

## Consequences

- A forced wheel is not reproducible from any commit; the `+uncommitted` mark is the only record. Its
  timestamp is still the last commit's, so a rerun over unchanged files builds the same file.
- Without `--force` nothing changes: the refusal names the files, and now also the flag.

## Example

```python
from aisland import wheels
from aisland.workspace.projects import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "planner", scope="all", force=True)
[(b.project, b.commit, b.uncommitted) for b in report.built if b.uncommitted]
# [('planner-dependency', '1a2b3c4+uncommitted', [' M pkg/module.py', '?? pkg/new.py'])]
```

## What you may change

- The wording of the mark and of the report's block, together with the tests that read them.
- What must not change: a dirty project is built only under `--force`; the working tree's files are the
  ones git lists, never the bare folder; a forced wheel is marked; `--force` never goes with `--commit`.

---

[The architecture and every decision](../architecture.md#the-decisions)
