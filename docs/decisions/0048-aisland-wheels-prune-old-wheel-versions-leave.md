# `aisland wheels prune`: old wheel versions leave the history; `reset` follows on every other clone (2026-10-27)

## Context

`aisland wheels build --commit` commits `wheels/`; every rebuild that changes a wheel adds its bytes to the history
for good. Measured on a project with seven wheel commits: 59 wheel versions in its history (14.6 MiB), 32 of them
held by a branch tip or a tag (7.5 MiB) — half of every clone was wheels no ref holds. The maintainer asked to keep the
wheels a branch or a tag holds and to drop the accumulated versions, aisland to force-push, and every other machine
to be fixable afterwards.

## Decision

- **What goes:** every blob ever under `wheels/**.whl` that no tip of this clone's branches and tags or origin's
  branches holds (origin's tags are fetched first and must equal this clone's). Nothing else is touched.
- **How:** git-filter-repo (`prune` extra, run as `python -m aisland.wheels.filter_repo`) with `--strip-blobs-with-ids`,
  `--partial`, `--prune-empty never`, `--prune-degenerate never` and `--preserve-commit-hashes`. With these the
  rewrite is a function of each commit alone: no commit disappears and no message changes, so a second clone
  stripping the same blobs gets the same ids (measured: identical ids, an unpushed commit carried on top). That is
  what makes `reset` a re-run of the same strip instead of a rebase.
- **Safety:** refuse tracked changes, a branch ahead of or behind origin's and a differing tag; bundle every ref
  first; check every kept ref's tree is unchanged; push atomically with `--force-with-lease=<ref>:<fetched sha>`
  per ref (no `+` in the refspec: a `+` overrides the lease); a detached HEAD is carried by a temporary ref
  (filter-repo moves refs, never a detached HEAD); reflogs expire for every ref but the stash, symbolic ones
  included (a clone's `origin/HEAD` keeps its own reflog of the old history).
- **The record:** `refs/wheels-prune/map` on origin, a chain of commits, one per prune, each holding
  `stripped-blobs` and `commit-map`. Text only, so it keeps no old commit alive; a chain, so a clone that missed
  several prunes takes each in turn with that prune's own blob list (a union of the lists would strip a version a
  later commit added back). The name holds no workspace or tool name: the repository may be one that must not
  reveal them.
- **The root:** the gitlink it records is replaced by the rewritten id of the same commit, in a commit of that path
  alone (built in a scratch index, so the root's staged changes stay staged). The root never records a newer commit
  of the project than before — an untracked project stays unrecorded.

## Consequences

- Every commit from the first stripped wheel onward gets a new id; old commits of the workspace root name ids that
  are gone. A clone that merges origin before `reset` brings the old history back, so `git pull-all` fetches
  `refs/wheels-prune/*` with each repository and refuses to merge one whose HEAD still reaches a commit a prune's
  `commit-map` lists, naming `aisland wheels reset <project>`; a plain `git pull` is not guarded.
- The host frees the stripped versions only when its own garbage collection runs.
- A clone's local tag on a commit the prune rewrote moves with it and holds origin's files for that commit.

## The backup keeps what only a reflog reaches (2026-11-07)

The shrink expires every reflog but the stash's and then runs `gc --prune=now`, while the backup bundled refs only:
a commit only a reflog reached (one a `reset --hard` left behind, an amended one) was dropped with no copy. Before
bundling, each such commit (the newest of each chain, `rev-list --reflog --not --all`) gets a ref
`refs/wheels-prune/reflog/<commit>`; the bundle carries them, the refs are deleted before the rewrite, and the printed
restore brings them back as those refs (`tests/test_wheels_prune.py::test_a_commit_only_a_reflog_reaches_is_kept_in_the_backup`).

## Reset runs on a clone in any state (2026-10-29)

The first reset on another machine failed twice where a person expected one command to do it: git-filter-repo
2.47.0 (the newest release) reads `git config --list` as `key=value` lines and died with `ValueError: dictionary
update sequence element #25 has length 1` on aisland's own multi-line aliases of before that day, and a clone with
an edit was refused. Afterwards the root still recorded the old commit (`32da69a`), which `git submodule update`
would fetch back. The maintainer ruled that `aisland wheels reset <project>` must work whatever the clone's state,
tracked or untracked.

- **The config:** git-filter-repo runs as `python -m aisland.wheels.filter_repo`, which replaces its one config reader by
  one over `git config --list --null` (a value may hold newlines, a key may have none) and calls its own `main`.
  The user's config is never rewritten. A test pins that git-filter-repo still has the reader it replaces.
- **Changes no commit holds:** a `git stash create` commit in `refs/wheels-prune/set-aside`, rewritten with the
  other refs, applied with `--index` on the new HEAD and the ref removed. Not `git stash push`: filter-repo moves
  `refs/stash` without its reflog, so the pop left an entry naming the old history. A failure keeps the ref and
  prints the command that puts the changes back.
- **The root:** what prune does (the gitlink to the rewritten twin of the commit it records, committed alone),
  through every prune's map composed; also when the clone itself has nothing left to strip.

Measured on a copy of a real clone before its reset (old history, six multi-line alias lines, an edit, a root
recording the old commit, `ignore = all`): one `reset` in 8 s — `dev/main 268e226 → de5f0b2`, the edit back, the
root at `de9223d`, the aliases untouched.

```bash
aisland wheels reset app              # any clone, any state: rewritten, edits put back, root re-recorded
```

## Another remote's refs (2026-11-08)

The rewrite moved this clone's branches, tags, `refs/remotes/origin/*` and the stash, and left another remote's
(`refs/remotes/mirror/*`) on the old history: gc could not shrink the clone and the mirror sync took the mirror for
ahead. `--apply` now refuses while another remote's refs reach a version that goes, naming the command that drops
them, and the mirror sync sends the rewritten history
([0079](0079-toml-is-written-by-tomlkit-and-the-release-reviews-setup-fixes.md)). A gitlink the user staged for the
project in the root stays staged when the root's pointer moves.

## Example

```bash
aisland wheels prune app              # says what would go, changes nothing
aisland wheels prune app --apply      # rewrites, force-pushes origin, re-records the root's pointer
aisland wheels reset app              # on every other machine, before its next pull
```

## What you may change

- Which refs keep their wheels (a `--keep REF` narrowing it) — not built.
- Never: a `+` refspec beside the lease, a rewrite that drops emptied commits or rewrites messages (two clones would
  disagree), or a record that holds old commits as parents.

---

[The architecture and every decision](../architecture.md#the-decisions)
