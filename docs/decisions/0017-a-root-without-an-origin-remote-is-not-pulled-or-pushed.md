# A root without an origin remote is not pulled or pushed, and its submodules follow their own branch

*2026-10-21*

## Context

`aisland workspace init` makes the root with `git init` and adds no remote: the root of such a
workspace lives on one machine. `pull-all` fetched it anyway and required every submodule to be on
the root's branch, so a root renamed to a feature branch, with one submodule left on `dev/main`, gave:

```text
needs you:
  root                 not fetched: fatal: 'origin' does not appear to be a git repository
  alpha                on dev/main, the root is on dev/feature — not pulled
  root                 origin has no dev/feature — not pulled
```

and `push-all` stopped on `git push origin <branch>` in the root after pushing the submodules.

## Decision

When the root has no `origin` remote, `pull-all` neither fetches nor merges it, pulls each submodule
on the branch it is on (the root's branch is then a local name, shared with nobody), and still
records what came in from origin under the same rule (only origin's commits, never backwards).
`push-all`, both forms, skips such a root and says so. A root with an origin is unchanged: a
submodule off its branch is still listed, not pulled.

## Consequences

- In an init-made workspace `pull-all` exits 0 when nothing needs a person, and `git status` in the
  root shows no moved submodule after it.
- The same holds for a folder workspace whose root repository has no origin.

## Example

```text
$ git pull-all
fetching 2 submodules (the root has no origin remote)…
beta                   merged origin/dev/feature (2 commits)
root                   recorded submodule pointers: beta
```

## What you may change

- The wording, with `tests/test_git_pull_push_all.py` and `tests/test_folder_git_aliases.py`.
- What must not change: with an origin, a submodule off the root's branch and outside `dev/*` is
  never pulled (`::test_a_submodule_off_the_roots_branch_is_not_pulled`).
- (2026-10-28, on request) A submodule on a `dev/<name>` branch of its own is pulled on THAT
  branch — commits go to any `dev/*` branch, and `/push-all` pushes it, so it must be pulled first
  or its push is rejected. The root, on another branch, records no pointer for it: the root's
  branch is not where that work lives
  (`::test_a_submodule_on_its_own_dev_branch_is_pulled_on_it_and_its_pointer_left`).

---

[The architecture and every decision](../architecture.md#the-decisions)
