# A root repository that records nothing, and a worktree of the whole workspace

*2026-11-27* [worktrees] (amends "A root project holds the workspace's files, and nothing records the projects'
commits")

## Context

A workspace with a root project had no repository at its root (decision 0083). Two things need one there:

- **git's workspace aliases.** `git status-all` and `git pull-all` are aliases in a repository's local config, so in a
  folder that is no repository they are typed inside a clone, never at the root.
- **An editor's worktrees.** An AI client starts a session in a worktree of the folder it is opened in, and needs a
  repository there to offer one (Claude Code's desktop app: "In a project that's a Git repository, turn on
  Worktree"). A plain `git worktree` of the root would still hold no project's code.

The maintainer made the root a repository with `aisland workspace init --untracked`, which built the layout 0083
rejected: every clone added as an untracked submodule, a `.gitmodules` beside the root project. With a `.gitmodules`
there, aisland took the folder for a workspace of submodules still to convert, and several of its steps changed at
once. Measured on the workspace aisland is developed in, the night after:

- setup installed aisland's harness set alone, without the root project's, and took away the eight links that set
  places — `mypy.ini` and `.flake8` among them — so every project's nightly lint stage ran without its config and
  failed;
- this machine's settings were read from the root's `.git/config`, which held none of the 13 in the root project's,
  so `setup -y` would have switched off the six optional projects this machine had on;
- `status-all` and `pull-all` in submodule mode walked the 33 submodules and left out the root project, which is none.

What a hook-made worktree may be was measured on the same machine: Claude Code's `WorktreeCreate` hook ran for
`claude --worktree` and for the desktop app's Worktree option at that root, a repository with no commit; a desktop
session then read the root's `.claude/settings.json` and a CLI session the worktree's. Its documentation requires a
folder a hook makes to sit outside every repository. A checkout of every project of that workspace was 95 MB in 4,490
files and took a second.

## Decision

- **The root may be a repository that records nothing.** `aisland workspace init` in a folder holding a root project
  makes one: `git init`, `/*` in `.git/info/exclude` so nothing is ever staged, no `.gitmodules`, no submodule, the
  clones left plain. `--add`, `--from`, `--local` and `--rewrite` are refused there, naming where a project is added
  instead (a row of the root project's `workspace.toml`).
- **One layout test**, `locations.rooted`: a root project and no `.gitmodules`, whether or not the folder is a
  repository. Every step that asked 0083's question asks it, and this machine's settings stay in the root project's
  `.git/config` in such a workspace (`machine_config.settings_repository`, `pytest_stats`). Without a `.gitmodules`,
  the git aliases at the root are the folder of clones' ones, which walk every clone, the root project's included.
- **A root project beside a `.gitmodules` stays a workspace still to convert** (a machine half way through
  `aisland workspace convert` must keep working). Setup's line now names both ways on: `convert`, or taking the
  `.gitmodules` away. `convert` finds nothing to do in a root repository that records nothing.
- **A worktree of the whole workspace**, `aisland workspace worktree create|remove|list|env` (`setup/worktrees.py`).
  `create <name>` makes `<workspace>-worktrees/<name>/` beside the workspace — outside every repository — holding a
  git worktree of each project this machine has, the root project's included, each on `dev/<name>` from the clone's
  commit. The root project's files come from the worktree's own root project (setup's harness step, run there), so an
  edit lands on its branch; the venv, `.env`, MCP servers, shell file, rules and Claude Code settings are links to the
  workspace's. `remove` refuses while a project holds uncommitted work or the folder holds a file of no project,
  deletes each branch that got no commit and keeps, naming, the others. `env` is the session's `PYTHONPATH`: the
  shared venv imports the main checkouts, so the worktree's project folders go first.
- **Setup writes three hook entries into the root's `.claude/settings.json`** (`setup/claude_hooks.py`):
  `WorktreeCreate` and `WorktreeRemove` run `create|remove --hook`, `SessionStart` runs `env --hook`, which appends the
  line to `$CLAUDE_ENV_FILE`. A worktree links its `.claude/settings.json` to the root's, so a CLI session there finds
  the same hooks. Only aisland's entries are written or taken away; the person's keys and hooks are kept, and a file
  aisland cannot read is left and named. In a workspace without a root project aisland's entries go.

Rejected: a `.gitmodules` of untracked submodules (0083's reason stands: a second list beside `workspace.toml`, and
the root project is no submodule); a plain `git worktree` of the root, with or without the submodules checked out (no
code, or a fresh clone of every project per session); a worktree of the projects a session names only (a session finds
out what it edits as it goes); a separate venv per worktree (the installs of every framework, per session).

## Consequences

- `git status-all` and `git pull-all` work at the root; the root repository has no origin, so `pull-all` and
  `push-all` skip it.
- Inside a worktree, the git aliases still name the workspace's root: `status-all` typed in a worktree's clone shows the
  main checkouts.
- Claude Code does not run the `WorktreeRemove` hook for a subagent's worktree: those stay until
  `aisland workspace worktree remove`, and `list` shows them.
- A worktree's `PYTHONPATH` is the project folders, as the editor's `extraPaths`; a project whose package sits elsewhere
  (a `src/` layout) still imports the main checkout's.

## Example

```text
~/work/acme/                    ← a repository that records nothing (.git/info/exclude: /*)
├── acme/                       ← the root project
├── models/
└── .claude/settings.json       ← WorktreeCreate, WorktreeRemove, SessionStart → aisland workspace worktree …
~/work/acme-worktrees/bold-oak/ ← a session's worktree
├── acme/                       ← git worktree of acme, branch dev/bold-oak
├── models/                     ← git worktree of models, branch dev/bold-oak
├── workspace.toml → acme/workspace.toml
└── .venv → ~/work/acme/.venv
```

## What you may change

- The words of the lines and refusals, the worktrees' folder name, and which of the workspace's files a worktree
  links (`worktrees.SHARED`).
- Never: a `.gitmodules` or a staged project in the root repository beside a root project, a worktree folder inside a
  repository, a removal that drops uncommitted work or a branch with commits, or an entry of the person's in
  `.claude/settings.json` changed.

---

[The architecture and every decision](../architecture.md#the-decisions)
