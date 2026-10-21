---
description: Commit all uncommitted code across the root repository and all submodules with appropriate, automatically generated comments.
model: sonnet
allowed-tools: [Bash(git add:*), Bash(git commit:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*), Bash(git branch:*), Bash(git -C * branch:*), Bash(git -C * add:*), Bash(git -C * commit:*), Bash(git -C * status:*), Bash(git -C * diff:*), Bash(git -C * log:*), Bash(cd:*), Bash(for *), Bash(while *), Bash(if *), Bash(python3:*), Bash(python:*), Bash(aisland:*)]
---
<!-- Shipped by aisland (aisland/harness/commands/commit-all.md), installed into .agents/workflows/ by `aisland setup` (aisland/harness.py). Change it in aisland; the rules it cites are aisland's (.agents/aisland/AGENTS.md). -->

# Workflow: Commit All (Recursive Submodule Commit)

When the user invokes the `/commit-all` command (or requests to commit all changes):

0a. **Branch check, before anything is staged** (aisland rules → "Everything Goes To `dev/main` — Never Create A Branch"): for the root and every repository with uncommitted changes, `git -C <repo> branch --show-current`. Any that prints something other than `dev/main` — another branch, or nothing (a detached HEAD) — → stage and commit NOTHING anywhere; report each such repository with its branch to the user and stop.

0b. **Files someone else already staged, before anything is staged:** for the root and every submodule, `git -C <repo> diff --cached --name-only`. `/commit-all` has staged nothing yet, so anything listed was staged by another session — often one halfway through `/commit-session`, its files not finished — or by the user. Then stage and commit NOTHING anywhere; show each repository with its staged files, e.g.

   ```text
   Staged before /commit-all ran — not mine to commit:
     aisland: aisland/selection.py, tests/test_selection.py
   ```

   and ask the user whether to commit them too or to wait until that session is done. Run `ListAgents` first and name the busy sessions, so the user can see whose they likely are. Go on only on the user's answer.

1. **Dependency Analysis:** Determine the project processing order to ensure submodules are committed before the root repository update.
   - Command, run with the workspace `.venv` active: `python3 -c "from aisland.manager import get_install_order, get_workspace_root; print([p['path'].name for p in get_install_order(get_workspace_root())])"` — it prints the project FOLDERS, dependencies first (not their distribution names).

2. **Per-Project Processing:** For each submodule in the resolved order:
   a. **Status Check:** Check for uncommitted changes (`git status --short`).
   b. **Message Generation:** If changes exist, analyze the diff (`git diff HEAD`) and the status to generate a concise, professional commit message (following the `feat:`, `fix:`, `chore:` convention).
   c. **Commit:** Stage all changes (`git add .`) and commit with the generated message. No quality checks (isort, black, mypy, pytest) are performed in this shortcut workflow.
   d. **Feedback:** Inform the user of the commit hash and message for each project.

3. **Root Repository Processing:**
   a. **Submodule Updates:** Stage all updated submodule pointers (`git add <submodule_path>`).
   b. **Other Changes:** Check for any other uncommitted changes in the root repository.
   c. **Final Commit:** Generate a summary commit message (e.g., `chore: sync submodules and update [files]`) and commit all changes to the root repository.

4. **Safety Mandate:** NEVER perform a `git push` as part of this workflow unless explicitly requested by the user in a follow-up instruction.

5. **Final Summary:** Provide a concise table of all committed projects, their branch, and their new commit SHAs.

6. **Tell the other sessions afterwards** (aisland rules → "Sessions On This Machine Coordinate Before They Edit"): commits never wait, so no session was held. Run `ListAgents` and send each **busy** peer one `SendMessage` naming the commits (repository, SHA) that took its uncommitted files, so it knows its work is in history.
