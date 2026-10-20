---
description: Publish the whole workspace — /pull-all first (so no push is rejected and every conflict is merged), then each submodule with commits to push and the root last, the root only when every submodule commit it records is on origin. Runs no quality gates (/ship does). Never touches main.
model: sonnet
allowed-tools: [Bash(git pull-all:*), Bash(aisland mirror sync:*), Bash(git push:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git show:*), Bash(git add:*), Bash(git commit:*), Bash(git reset:*), Bash(git stash:*), Bash(git rev-parse:*), Bash(git rev-list:*), Bash(git merge-base:*), Bash(git -C * push:*), Bash(git -C * status:*), Bash(git -C * diff:*), Bash(git -C * log:*), Bash(git -C * show:*), Bash(git -C * add:*), Bash(git -C * commit:*), Bash(git -C * reset:*), Bash(git -C * stash:*), Bash(git -C * rev-parse:*), Bash(git -C * rev-list:*), Bash(git -C * merge-base:*), Bash(git -C * branch:*), Bash(cd:*), Bash(source:*), Bash(isort:*), Bash(black:*), Bash(flake8:*), Bash(mypy:*), Bash(pytest:*)]
---
<!-- Shipped by aisland (aisland/harness/commands/push-all.md), installed into .agents/workflows/ by `aisland setup` (aisland/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: Push All (pull, publish)

Pushes the current branch of every repository — the submodules, then the root — after merging what origin has. It commits only what `/pull-all` commits (merges, recorded submodule pointers): run `/commit-all` or `/commit-session` first for your own work. It never merges into `main` (that is `/ship`).

**No quality gates** (user ruling 2026-10-07: "push-all should not execute the gates (that is the /ship command)"): the committed work was checked when its task ended, and `/ship` runs the gates before it merges into `main`. The only checks on the way are `/pull-all`'s own, on a merge conflict it resolved by hand before committing it — which is why the formatters and `pytest` stay in `allowed-tools`.

When the user invokes `/push-all`:

1. **Pull first.** Run the `/pull-all` workflow (`.agents/workflows/pull-all.md`) in full. If it ends with items that still need the user → stop, report them, push nothing.

2. **What to push.** `BRANCH` = the root's branch. For each submodule (`git submodule --quiet foreach 'echo "$sm_path"'`), then the root: `git -C <repo> rev-list --count origin/$BRANCH..HEAD`. Nothing ahead → skip. A submodule not on `$BRANCH` (`git -C <repo> branch --show-current`) → skip and report.

3. **Push the submodules** with commits to push: `git -C <project> push origin $BRANCH`. Uncommitted changes in that project (`git -C <project> status --short`, often another session's work in progress) are not pushed — say so beside its row. Rejected because origin moved meanwhile → run `/pull-all` once more, push again; rejected twice → stop and report.

4. **Push the root last**, and only when origin has every submodule commit it records: for each submodule, `git -C <submodule> merge-base --is-ancestor "$(git rev-parse HEAD:<submodule>)" origin/$BRANCH` (a push updates `origin/$BRANCH`). Any that fails → do not push the root; name the submodule (its push failed, or it is off `$BRANCH`). Otherwise `git push origin $BRANCH`.

5. **Mirrors.** After the pushes — whatever their outcome, since it copies only what reached the primary — run `aisland mirror sync` from the workspace root. It pushes each mirrored submodule's `dev/main`, `main` and tags to its second host (`aisland/README.md` → "Project Mirrors"); with no mirror configured it prints one line and does nothing. Exit 1 (a branch or tag REFUSED, a host unreachable) is reported, never retried and never forced: the refusal line names the merge that fixes it, and that merge is the user's.

6. **Report** — one row per repository with commits to push: repository | commits | pushed / not pushed and why. Then one row per mirror from `aisland mirror sync`: project | dev/main | main | tags (its words: `pushed (3 commits)`, `in sync`, `REFUSED: …`). Then `/pull-all`'s own report (conflicts resolved, edits set aside and put back). Last line: nothing was merged into `main` — `/ship` does that.
