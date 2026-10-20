---
description: Pull the whole workspace — every submodule, then the root — and merge everything that comes in. Runs `git pull-all` (pointer conflicts settled, new submodule commits recorded), then finishes what it hands back - uncommitted edits set aside and put back, code conflicts resolved, checked and committed - and reruns until clean. Never pushes.
model: sonnet
allowed-tools: [Bash(git pull-all:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git show:*), Bash(git add:*), Bash(git commit:*), Bash(git reset:*), Bash(git stash:*), Bash(git rev-parse:*), Bash(git -C * status:*), Bash(git -C * diff:*), Bash(git -C * log:*), Bash(git -C * show:*), Bash(git -C * add:*), Bash(git -C * commit:*), Bash(git -C * reset:*), Bash(git -C * stash:*), Bash(git -C * rev-parse:*), Bash(cd:*), Bash(source:*), Bash(isort:*), Bash(black:*), Bash(flake8:*), Bash(mypy:*), Bash(pytest:*)]
---
<!-- Shipped by aisland (aisland/harness/commands/pull-all.md), installed into .agents/workflows/ by `aisland setup` (aisland/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: Pull All (merge everything that came in)

`git pull-all` — the alias `aisland/aisland/git/setup_git.sh` installs, logic in `aisland/aisland/git/git_pull_all.sh`, usage in the aisland README "Pulling Everything (`pull-all`)" — does the mechanical part: fetches the root and every submodule, merges each submodule, merges the root (a submodule-pointer conflict settled to the submodule's checkout when it holds both sides) and records the submodule commits that came in. What it cannot do it lists under `needs you:` and exits 1. This workflow finishes those items and reruns it until it is clean. It never pushes (`/push-all` does).

When the user invokes `/pull-all`:

1. **Run it** from the workspace root: `git pull-all`. Exit 0 → step 6.

2. **Edits in the way** — `not merged: uncommitted edits to <files> would be overwritten`. The user's standing choice (2026-10-06) is to set them aside, merge and put them back; a stash rewrites those files, so coordinate first (aisland rules → "Sessions On This Machine Coordinate Before They Edit"):
   a. `ListAgents`; send each **busy** peer one message whose first line is `<your session name>: HOLD <repo>/<file>, … — /pull-all sets them aside to merge and puts them back`, naming every listed file. Wait for each `HOLDING` (`notify_when_idle: true`, never a polling loop). A peer that goes idle without answering, or no answer in about 5 minutes → tell the user which session and files, skip `--autostash` this round, and go on with step 3.
   b. `git pull-all --autostash`.
   c. When steps 3–4 are done, send the same peers `RESUME <files>`; name any file whose put-back clashed (step 4), so its owner re-reads it.

3. **Conflicts** — `CONFLICT in <files> — resolve, commit, then run git pull-all again`, in a submodule or the root. In that repository:
   a. For each file: read the conflict (`git diff <file>`) and what each side meant (`git log --merge --oneline -- <file>`, then `git show` those commits). Write a version that keeps BOTH intents; take one side whole only when it already contains the other's change. A file one side deleted and the other changed, or two designs that cannot both stand → stop and ask the user, showing both sides.
   b. `git add <file>`. Then `git diff --name-only --diff-filter=U` must print nothing and `git diff --cached --check` must find no conflict marker.
   c. **Quality gates** (aisland rules → "Mandatory Quality Validation"; skipped for a repository without `pyproject.toml`, e.g. the root): from the project directory, `isort --check-only .`, `black --check .`, `flake8 .`; from the workspace root `mypy <project>/`; from the project directory `pytest` (the fast run). A formatter failure in a file you resolved → `isort <file> && black <file>`, `git add` it, rerun. A failure in a file the merge did not touch (another session's uncommitted work, a pre-existing break) → do NOT commit: leave the merge in progress and report the failing lines to the user.
   d. Commit with the merge's own message: `git commit --no-edit --cleanup=strip` (never `-a`). Keep the hash for the report.
   A `CONFLICT in <submodule>` line in the ROOT means that submodule could not be pulled (see its own line): settle that first; the rerun settles the pointer.

4. **Put-back clashes** — `your uncommitted edits to <files> clash with the pulled changes` (or, from an earlier run, `unresolved conflict in <file> from putting back your edits`). These are uncommitted edits, possibly another session's: resolve the markers keeping both the pulled change and the edit, `git reset -q -- <file>` (it stays an uncommitted edit — never commit it), then drop the copy the run kept: `git stash list` → the entry `git pull-all autostash (applied with conflicts)` → `git stash drop stash@{n}`.

5. **Rerun** `git pull-all` after steps 2–4. Repeat until it exits 0 or only step-6 items remain. The same item back after three rounds → stop and report (aisland rules → "Problem-Solving Rigor").

6. **Items that stay the user's** — report them verbatim and change nothing: `not fetched: …` (network, VPN), `detached HEAD — not pulled` and `on <branch>, the root is on <branch> — not pulled` (a deliberate checkout), `origin has no <branch>`, `the root records <sha>, which <submodule>'s <branch> does not contain`, `could not put back your uncommitted edits …`.

7. **Report**
   - `git pull-all`'s last output, as printed.
   - Each conflict resolved: repository, file, the two sides' lines and the merged lines, the merge commit's hash. This is how the user reviews what was merged for them.
   - Edits set aside and put back (repository, files, whose session).
   - What still needs the user (step 6).
   - Last line: nothing was pushed; `/push-all` publishes.
