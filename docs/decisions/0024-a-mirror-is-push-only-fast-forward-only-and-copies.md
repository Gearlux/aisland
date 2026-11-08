# A mirror is push-only, fast-forward-only, and copies the primary (2026-10-16)

## Context

Some submodules must be visible on two hosts — a GitHub organisation and a Bitbucket server —
while work keeps happening in one place. Git's own answer, several push URLs on `origin`, makes
every `git push` go to both, but a rejection by the second host is one `! [rejected]` line in
the push's output, and that host then falls behind without anything reporting it. Two-way
syncing (pulling from both) would make a second primary, with merges between the hosts.

## Decision

- The primary is the `.gitmodules` remote and nothing changes about it. The mirror is a second
  remote, named `mirror`, that only receives.
- `aisland workspace mirror sync` (`aisland mirror sync` until 2026-10-22) pushes the PRIMARY's `dev/main`, `main`
  and tags (read with
  `git ls-remote origin`, the missing objects fetched by id), never the local branches.
- (2026-10-28, on request) It also copies every other branch the local checkout has — the
  local branch list chooses WHICH branches, the primary still says WHAT each holds. `dev/main` and
  `main` stay always copied, so a checkout on `dev/<name>` alone keeps the mirror's `main` current.
  A local branch the primary lacks is reported `not on the primary`, not pushed and not a failure;
  a branch only on the primary is not copied (someone else's branch reaches the mirror from
  their machine's sync). The rows widen to the project's longest branch name.
- It never forces and never deletes: a branch the mirror has commits on that the primary lacks
  is refused with the commit and the merge command; a differing tag is refused; a mirror-only
  branch or tag is left alone.
- `/push-all` and `/ship` run it as their last step (chosen 2026-10-16, option A), so a
  refused or unreachable mirror is a row in their report, not a lost line of git output.
- Two layers like a project's source: committed, the project's `mirror` in `workspace.toml`; per machine,
  `aisland.mirror.<project>.mirror`; both set with `aisland workspace configure mirror set|unset [--machine-only]`
  (a `[mirrors.<project>] url` table and one key `aisland.mirror.<project>` until 2026-10-22, no longer read). Only submodules (chosen 2026-10-16).
- The remote is marked `remote.mirror.aislandManaged`; setup and `unset` remove only a marked
  one, and `set` refuses to take over an unmarked one.
- Every network call runs with `GIT_TERMINAL_PROMPT=0`.

## Consequences

- The hosts can differ only by the mirror being behind; when someone pushes to the mirror
  directly, the sync stops for that branch until the commit is merged into the primary.
- A plain `git push` in a terminal reaches only the primary; the mirror catches up on the next
  sync.
- A sync costs four network round trips per project (two `ls-remote`, a fetch, a push).

## Example

```bash
aisland workspace configure mirror set models git@github.com:acme/models.git
aisland workspace mirror sync
```
```
==> models → git@github.com:acme/models.git
    dev/main  pushed (3 commits)
    main      in sync
    tags      pushed v0.4.0
```

## What you may change

- `MIRRORED_BRANCHES` (the always-copied ones) and `_branches` in `aisland/workspace/mirrors.py`, with `docs/sources.md` and
  `tests/test_mirrors.py`.
- What must not change: no force, no delete, no tag overwrite; the primary's state is what is
  copied; an unmarked `mirror` remote is never touched.

---

[The architecture and every decision](../architecture.md#the-decisions)
