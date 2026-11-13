# `pull-all` settles a submodule pointer only to a commit holding both sides

*2026-10-06*

## Context

The root records one commit per submodule, and several machines and sessions commit in the same
submodules and record them. `git pull` cannot bring that together. It moves each submodule only to
the commit the root records, so a commit pushed to a submodule that nobody recorded is never
pulled. And when both sides of a root merge recorded a new commit of the same submodule, the merge
stops on `CONFLICT (submodule)` even when the submodule merges cleanly; git prints the
submodule's merge commit as "a possible merge resolution" and leaves it to a person. The earlier
one-line alias pulled each submodule and then the root, so it walked into that conflict and,
re-run, stopped at once on `Pulling is not possible because you have unmerged files`.

## Decision

`git pull-all` is a script, `git_pull_all.sh`, that the alias runs from the workspace root
wherever it is typed. It fetches everything first (in parallel, before anything merges), merges
each submodule on the root's branch, then the root, then records pointers. Three rules:

- **A pointer conflict is settled to the submodule's checked-out commit only when that commit
  contains both sides** (`merge-base --is-ancestor` with stage 2 and stage 3). That is exactly the
  commit git suggests; any other case stays a conflict for a person.
- **A pointer is recorded only for what came from origin.** A submodule is recorded when origin has
  commits the root's pointer lacks and the checkout contains them, and the root holds everything
  its own origin has. A pointer never moves backwards. A submodule whose only unrecorded commits are
  local is left for whoever made them.
- **What it cannot do is listed, and a rerun continues.** A submodule left mid-merge holds back the
  root (its pointer depends on it). A root merge left on pointer conflicts, by this script or by a
  plain `git pull`, is settled and committed by the next run.

`--autostash` sets aside only the files the merge needs and puts them back. A put-back that clashes
renames the kept stash entry (`… (applied with conflicts)`), so no later run applies it twice.

Merges are `git fetch` + `git merge`, never `git pull`: a `pull` honours each machine's
`pull.rebase`/`pull.ff` (unset, a diverged pull refuses with "Need to specify how to reconcile
divergent branches"), and under `submodule.recurse` it checks the recorded pointers out into the
submodules. Measured: a submodule on `dev/main` with a local commit went to
`branch='' 05b1514 wfA`, detached at the recorded commit, its local commit on no branch.

Rejected: resolving pointer conflicts to "ours" or "theirs". Either can drop the other machine's
submodule commits from the record. Also rejected: a Python `aisland` command. The alias must work
before the `.venv` exists, and from a submodule's shell, where `aisland` is not on `PATH`.

## Consequences

- Two machines that both recorded a submodule pull each other without a manual step. Only real
  content conflicts reach a person, and the `/pull-all` workflow resolves those too.
- `pull-all` makes commits: the root's merge and one pointer-record commit. The root is then ahead
  of origin until pushed, and `push-all` pushes the submodules before the root, so the root on origin
  never records a submodule commit origin does not have.
- The script runs under macOS's `/bin/bash` 3.2: no `wait -n`, associative arrays or `mapfile`. The
  tests run it there.

## Example

```console
$ git pull-all                       # both machines recorded planner
fetching the root and 25 submodules…
planner             merged origin/dev/main (1 commit)
root                   pointer conflict planner → 6cb3d7f (holds both sides)
root                   merged origin/dev/main (1 commit)
```

```bash
# the settle rule, as git_pull_all.sh applies it to each conflicted gitlink
ours=$(git rev-parse :2:planner); theirs=$(git rev-parse :3:planner)
head=$(git -C planner rev-parse HEAD)
git -C planner merge-base --is-ancestor "$ours" "$head" &&
    git -C planner merge-base --is-ancestor "$theirs" "$head" &&
    git add planner          # settled; otherwise it stays listed as CONFLICT
```

## What you may change

- The output wording, the fetch parallelism (`FETCH_JOBS`), and what is listed under `needs you:`,
  with `tests/test_git_pull_push_all.py`.
- What must not change: a pointer is settled only to a commit containing both sides and never moved
  backwards; a submodule's local-only unrecorded commits are never recorded by the pull; the merges
  never depend on `pull.*` settings; uncommitted edits are touched only under `--autostash`, and only
  the files the merge needs.

---

[The architecture and every decision](../architecture.md#the-decisions)
