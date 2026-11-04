---
description: Per project (and the root workspace) in dependency order — run local quality gates, push, merge to main, push main, merge main back. Real GH Actions verifies post-push. Stop on any failure. Does NOT commit (use /commit-all first).
model: sonnet
allowed-tools: [Bash(aisland workspace mirror sync:*), Bash(git add:*), Bash(git commit:*), Bash(git push:*), Bash(git pull:*), Bash(git checkout:*), Bash(git merge:*), Bash(git -c submodule.recurse=false:*), Bash(git -C * add:*), Bash(git -C * commit:*), Bash(git -C * push:*), Bash(git -C * pull:*), Bash(git -C * checkout:*), Bash(git -C * merge:*), Bash(cd:*), Bash(source:*), Bash(for *), Bash(while *), Bash(if *)]
---
<!-- Shipped by aisland (aisland/harness/commands/ship.md), installed into .agents/workflows/ by `aisland setup` (aisland/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: Ship (Push → Promote → Sync-Back)

When the user invokes `/ship`:

## 1. Pre-flight gates

- For the root workspace **and** every submodule: `git status --short`. If anything is uncommitted **anywhere**, ABORT and tell the user to run `/commit-all` first. Shipping with dirty working trees is forbidden.

No local-CI gate: CI runs on every push and is the authoritative gate.

## 2. Dependency order

Resolve via the call `/commit-all` uses (step 1 of `commit-all.md`), with the workspace `.venv` active: `aisland workspace projects` — the project folders, one a line, dependencies first. Iterate the **submodules** in that order, then auto-stage any submodule pointer drift (§ 2a), then handle the **root workspace** with the per-project flow at the end.

### 2a. Auto-stage submodule pointer bumps (between submodules and root)

Every successful submodule sync-back advances the submodule's tip on `main`, which drifts the pointer the root repo records for that submodule. Before root's per-project flow begins, stage **only** those pointer bumps and commit them in the root. Any other dirt in the root tree at this point → ABORT (tell the user to run `/commit-all`).

```bash
# Detect drifted submodule pointers and non-submodule dirt simultaneously.
drifted=""
other=""
while IFS= read -r line; do
  status="${line:0:2}"
  path="${line:3}"
  # Submodule pointer drift shows as " m " (content changed) or " M " (commit changed).
  if git -C "$WORKSPACE_ROOT" config --file .gitmodules --get-regexp "^submodule\..*\.path$" \
     | awk '{print $2}' | grep -qx "$path"; then
    drifted="$drifted $path"
  else
    other="$other $path"
  fi
done < <(git -C "$WORKSPACE_ROOT" status --porcelain)

if [ -n "$other" ]; then
  echo "Non-submodule dirt in root: $other — run /commit-all first"
  exit 1
fi
if [ -n "$drifted" ]; then
  git -C "$WORKSPACE_ROOT" add $drifted
  git -C "$WORKSPACE_ROOT" commit -m "chore: sync submodule pointers after /ship"
fi
```

The pointer-bump commit is the **only** commit /ship creates. Everything else still requires `/commit-all` in advance.

## 3. Per-project flow

For each project (submodule or root), `cd` into its directory and run these steps **in order**, stopping the entire workflow on the first failure (no skip-and-continue).

> **`submodule.recurse=true` safety.** Every `git checkout`/`pull`/`merge` below is invoked with `-c submodule.recurse=false`. Without this override, a `git checkout main` on the root with `submodule.recurse=true` will revert every submodule to whatever SHA the **old** `main` recorded — detaching them at pre-ship pointers and (for newly-registered submodules absent from old `main`) effectively `submodule deinit`-ing them: the `.git` gitlink and tracked files in the working tree get wiped while untracked artifacts are preserved. The `-c` override is per-invocation; it does not change the user's stored config, which keeps recurse on for everything else.

### a. Quality gates

Skip if no `pyproject.toml` (e.g. the root workspace has no Python package).

```bash
source .venv/bin/activate || source ../.venv/bin/activate
isort . --check-only
black . --check
flake8 .
# mypy must run from the workspace root with the global .venv, and against
# the project *directory* (not `-p package`). `-p` restricts mypy to the
# importable package and silently skips `tests/`, so test-dir issues (e.g.
# an unused `# type: ignore`) slip through the local gate. The directory
# invocation matches what GH Actions / Jenkins run.
(cd "$WORKSPACE_ROOT" && source .venv/bin/activate && mypy "$PROJECT_DIR/")
# The FAST run: a project that marks tests `slow` deselects them by default. The slow run
# (`pytest -m slow`) is CI's — aisland rules → "Two Test Runs, The Fast One Within 5 Minutes".
pytest
```

Use `--check-only` / `--check` rather than mutating mode — pre-flight already asserts a clean tree. If isort or black would reformat anything, that's a failure: stop and ask the user to run formatters + `/commit-all`.

Failure → STOP.

### b. Push current branch

```bash
CURRENT=$(git branch --show-current)
git push origin "$CURRENT"
```

Failure → STOP.

### c. Merge to main + push main

**Skip steps c, d** entirely if `CURRENT == "main"` (you're already there — nothing to promote, nothing to sync back). The push in (b) already covered main.

Also skip c, d silently if the current branch is at `origin/main` with no commits to ship — mark the project `SKIP`. Quality gates still ran.

```bash
git -c submodule.recurse=false checkout main
git -c submodule.recurse=false pull --ff-only origin main
git -c submodule.recurse=false merge --no-ff "$CURRENT" -m "Merge $CURRENT into main"
git push origin main
```

If the `--ff-only` pull fails (main diverged) or the merge has conflicts → STOP.

### d. Sync main back to current branch

```bash
git -c submodule.recurse=false checkout "$CURRENT"
git -c submodule.recurse=false merge --ff-only main
git push origin "$CURRENT"
```

Fast-forward is guaranteed because main now contains current's tip plus the merge commit. Failure here is unexpected → STOP.

## 3e. Mirrors (once, after the last project)

After the last project — or after the failure that stopped the workflow, since it copies only what reached the primary — run `aisland workspace mirror sync` from the workspace root. It pushes each mirrored submodule's `dev/main`, `main`, every other branch checked out locally (as the primary has it) and tags to its second host (`aisland workspace configure mirror --help`; aisland's `docs/sources.md` → "Project Mirrors"); with no mirror configured it prints one line and does nothing. Its exit 1 (a branch or tag REFUSED, a host unreachable) changes no project's `STATUS`: it is reported below, never retried, never forced — the refusal line names the merge that fixes it, and that merge is the user's.

## 4. Final summary

After every project (or after the failure that stopped the workflow), print one row per processed project:

```
PROJECT          | LAST STEP   | NEW MAIN SHA | STATUS
-----------------|-------------|--------------|----------
core             | sync-back   | abc1234      | OK
models           | quality     | —            | SKIP
app              | push-main   | 9876543      | FAILED
…                | …           | …            | NOT RUN
```

`STATUS` is one of:
- `OK` — completed all four steps.
- `SKIP` — current branch was at `origin/main` already with no commits to ship (silent skip — quality gates still ran).
- `FAILED` — stopped here.
- `NOT RUN` — skipped because an earlier project failed.

Then one row per mirror from `aisland workspace mirror sync`: project | dev/main | main | tags, in its own words (`pushed (3 commits)`, `in sync`, `REFUSED: …`).

## Stop-on-failure semantics

- The workflow stops **immediately** on the first failed step.
- Already-shipped projects stay shipped — their `main` has been advanced and the sync-back already ran. No rollback.
- The summary table makes it obvious which project + step failed.

## What this workflow does NOT do

- **No commits** — run `/commit-all` first. The sole exception is the single submodule-pointer-bump commit made in § 2a (deterministic message, only stages submodule paths).
- **No local CI** — real GH Actions on each push is the gate; check it after shipping.
- **No PRs** — direct local merge + push to `main`. Assumes your account can push to `main` (no branch protection, or you're an admin).
- **No coverage requirement** — only existing tests are run; no test-writing.
- **No automatic rollback** — if GH Actions breaks after a ship, you investigate.
