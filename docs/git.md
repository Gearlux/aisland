# Git across the workspace

The Git aliases `aisland setup` installs for a workspace whose projects are Git submodules — pull, push, switch, merge, tag and status across the root (the workspace's own repository) and every project — and the `safe` alias that runs the quality gates before pushing.

## Submodule Management

A workspace can keep its projects as Git submodules of the root repository. The aliases below work on the root and every active submodule at once.

### Workspace Git Aliases

The aliases below (`pull-all`, `push-all`, `switch-all`, `merge-all`, `tag-all`, `status-all`, `safe`) are installed by `aisland setup` into the **local** config of the root repository *and* every active submodule. They assume the workspace layout and toolchain, so they are deliberately not written to the global config — that would leak workspace-specific behavior into unrelated repositories. The definitions live in the aisland package's [`setup_git.sh`](../aisland/git/setup_git.sh) (`pull-all` calls [`git_pull_all.sh`](../aisland/git/git_pull_all.sh) beside it); `aisland setup` installs them again, which is also how to pick up a newly activated submodule.

### Pulling Everything (`pull-all`)

`git pull-all` brings the whole workspace up to date with origin: every active submodule, then the root, on the root's branch. Type it in the root or inside any submodule — it always pulls the whole workspace.

```bash
git pull-all                # merge what came in; list what needs you
git pull-all --autostash    # also set aside uncommitted edits that are in the way, and put them back
```

A plain `git pull` (even with `submodule.recurse`) only moves each submodule to the commit the root *records*; a commit pushed to a submodule that nobody recorded in the root is never pulled. And when two machines both commit in a submodule and record it, the root's merge stops on the pointer even though the submodule merges cleanly:

```
CONFLICT (submodule): Merge conflict in app
Automatic merge failed; fix conflicts and then commit the result.
```

`git pull-all` handles both. In order, it:

1. **fetches** the root and every submodule from `origin`, in parallel, before anything merges;
2. **merges** `origin/<branch>` into each submodule that is on the root's branch. A branch whose name starts with `dev/` (`dev/main`, `dev/fix-x`) is a working branch: a submodule on one is merged with that branch's `origin/dev/<name>` even when the root is on another, and when it is not the root's the root records no pointer for it;
3. **merges** `origin/<branch>` into the root. A pointer conflict is settled to the submodule's checked-out commit **only when that commit contains both sides** (the commit git itself names as "a possible merge resolution"), and the merge is committed;
4. **records** the submodules that hold commits from origin the root does not record yet, in one commit — `chore: sync submodule pointers after pull-all (<submodules>)` — leaving anything else you staged staged. A pointer never moves backwards, and a submodule whose only unrecorded commits are your own local ones is left for you (`/commit-all` records it).

The same situation as above, with `git pull-all`:

```
fetching the root and 25 submodules…
app                    merged origin/dev/main (1 commit)
root                   pointer conflict app → 6cb3d7f (holds both sides)
root                   merged origin/dev/main (1 commit)
```

What it cannot do is listed under `needs you:` and it exits 1; fix the item and run it again — it continues where it stopped (it also finishes a root merge a plain `git pull` left on a pointer conflict):

| listed as | what happened | what to do |
| --- | --- | --- |
| `CONFLICT in <files>` | both sides changed the same lines; that repository is left mid-merge (a submodule's conflict also holds back the root, whose pointers depend on it) | resolve, `git add`, `git commit`, run `git pull-all` again |
| `not merged: uncommitted edits to <files> would be overwritten` | the merge changes files you are editing; nothing was touched | commit them, or run `git pull-all --autostash` |
| `your uncommitted edits to <files> clash with the pulled changes` | `--autostash` put your edits back over lines the pull changed; the file has conflict markers, and a copy is in `git stash list` as `git pull-all autostash (applied with conflicts)` | fix the markers, `git reset -q -- <file>`, then `git stash drop` that copy |
| `detached HEAD — not pulled` / `on <branch>, the root is on <branch> — not pulled` | that submodule is on no branch, or on another branch than the root's whose name does not start with `dev/` | `git switch <branch>` in it to have it pulled |
| `on no branch, and git switch <branch> refused: …` | an untracked submodule's uncommitted edits are in the way of its branch | commit or set them aside, run `git pull-all` again |
| `not fetched: …` | its remote could not be reached (e.g. off VPN); the others still pull | reconnect and run again |
| `not merged: origin's history was rewritten by aisland wheels prune and this clone still holds the old one` | origin's old committed wheels were pruned ([wheels](wheels.md#old-committed-wheels-aisland-wheels-prune-and-aisland-wheels-reset)); merging would bring the old history back, so nothing was touched | `aisland wheels reset <project>`, then run `git pull-all` again |
| `the root records <sha>, which <submodule>'s <branch> does not contain` | the root points at a commit outside that branch; the pointer is left as recorded | check the submodule's branch |

`--autostash` sets aside only the files the merge needs, in a stash named `git pull-all autostash`, merges, and puts them back. If the merge itself conflicts, your edits stay aside until you commit the merge; the next `git pull-all` puts them back.

A root with no `origin` remote — the one `aisland workspace init` makes — has nothing to pull: it is neither fetched nor merged, and each submodule is pulled on the branch it is on, whatever the root's branch. What came in is still recorded in the root:

```text
$ git pull-all
fetching 2 submodules (the root has no origin remote)…
alpha                  merged origin/dev/feature (2 commits)
beta                   merged origin/dev/main (1 commit)
root                   recorded submodule pointers: alpha, beta
```

An untracked submodule (`aisland workspace untrack`: `ignore = all` in `.gitmodules` or this machine's
`.git/config`) is pulled the same way and never recorded.

The merges are explicit (`git fetch` + `git merge`, never `git pull`), so the result does not depend on a machine's `pull.rebase` setting. The logic lives in [`git_pull_all.sh`](../aisland/git/git_pull_all.sh) (why it is shaped this way: [architecture.md](architecture.md), "`pull-all` settles a submodule pointer only to a commit holding both sides"). In Claude Code, `/pull-all` (one of the commands [harness.md](harness.md) describes) runs it and finishes the listed conflicts for you; `/push-all` runs `/pull-all`, pushes, and ends with `aisland workspace mirror sync` (see [Project Mirrors](sources.md#project-mirrors-aisland-workspace-configure-mirror)).

### Pushing Everything (`push-all`)

`git push-all` pushes the current branch of every submodule, then the root's — the root only when every submodule push succeeded, so the root on origin never records a submodule commit origin does not have. A root with no `origin` remote is skipped, ending with `root: no origin remote — not pushed`.

```bash
git push-all
```

### Switching All Submodules (`switch-all`)

When moving between development streams (e.g., switching from a feature branch back to `dev/main`), you often need to update the branch context for both the parent repository and all its constituent submodules simultaneously.

To use it:

```bash
git switch-all dev/main
```

### Merging All Submodules (`merge-all`)

When you need to merge a base branch (e.g., `main` or `dev/main`) into your current feature branch across all projects in the workspace, you can use `merge-all`. This ensures your entire dependency tree stays synchronized with upstream changes.

To use it (e.g., to merge `dev/main` into your current branch everywhere):

```bash
git merge-all dev/main
```

### Tagging All Submodules (`tag-all`)

When you want to mark a workspace-wide snapshot (a release, or a proof-of-concept milestone), you need the *same* tag on the root repository and every active submodule, then pushed to each remote. `tag-all` is the cross-repo analogue of a single `git tag`: it creates one **annotated** tag at the current `HEAD` of the parent and each active submodule, then pushes it. Tags are created everywhere **first** (cheap, local) before any push, so an unreachable remote (e.g. an off-VPN private host) leaves every local tag intact — you simply re-run to push the rest. The tag message defaults to the tag name if omitted.

To use it (creates the tag locally **and** pushes it to every remote in one command):

```bash
git tag-all 'v1.0.0' 'First stable release'
```

> Tags are non-forcing: if `<tag>` already exists, the command aborts before pushing. Delete it first (`git tag -d <tag>` / `git submodule foreach 'git tag -d <tag>'`) to re-tag.

### Checking Status of All Submodules (`status-all`)

To quickly see the status and current branch of the root repository and all its constituent submodules, you can use the `status-all` alias.

To use it:

```bash
git status-all
```

### Safely Pushing Changes (`safe`)

A custom Git alias exists to enforce the workspace mandates before pushing code. Run it from inside any project submodule (or the workspace root): it activates the shared workspace `.venv`, formats the code with `isort` and `black`, lints with `flake8`, type-checks with `mypy` (executed **from the workspace root** so the root `mypy.ini`'s `mypy_path` is honored), and runs `pytest`. If everything passes, it commits and pushes safely to the remote.

To use the shortcut:

```bash
git safe "feat(lib): fixed the configuration parser bug"
```
