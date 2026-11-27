# Worktrees of the whole workspace

In a [workspace with a root project](workspace.md#a-workspace-with-a-root-project), a session started at the
workspace's root can work in a **worktree of the whole workspace**: a folder beside the workspace holding a git
worktree of every project, each on a branch of its own. The session edits, tests and commits there, and the main
checkouts stay as they are.

## Setting it up

Once per machine, at the workspace's root:

```bash
cd ~/work/acme
aisland workspace init .     # the root a repository that records no project
aisland setup                # the worktree hooks, in .claude/settings.json
```

`init` makes the root a repository that records nothing ([workspace](workspace.md#a-root-repository-that-records-no-project-aisland-workspace-init)):
an AI client offers a worktree only in a repository. `setup` writes three hook entries into the root's
`.claude/settings.json`, which have Claude Code ask aisland for a session's worktree instead of running
`git worktree` itself — a worktree of the root's repository would hold no project's code:

```json
{
  "hooks": {
    "WorktreeCreate": [{"hooks": [{"type": "command", "command": "\"/Users/you/work/acme/.venv/bin/aisland\" workspace worktree create --hook"}]}],
    "WorktreeRemove": [{"hooks": [{"type": "command", "command": "\"/Users/you/work/acme/.venv/bin/aisland\" workspace worktree remove --hook"}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "\"/Users/you/work/acme/.venv/bin/aisland\" workspace worktree env --hook"}]}]
  }
}
```

Every other key and hook in the file is yours and stays as it is; setup replaces only these three entries, and takes
them away again in a workspace without a root project.

Then start a session at the root in a worktree: turn on **Worktree** in the desktop app, run
`claude --worktree <name>`, or ask a session to work in a worktree.

## What a worktree holds

```text
~/work/acme-worktrees/bold-oak/    ← beside the workspace, outside every repository
├── acme/                          ← git worktree of the root project, branch dev/bold-oak
├── models/                        ← git worktree of models, branch dev/bold-oak
├── tools/                         ← … every project this machine has
├── workspace.toml → acme/workspace.toml
├── TASKS.md → acme/TASKS.md       ← the root project's files, from this worktree's acme
├── AGENTS.md, CLAUDE.md           ← the workspace's rules
├── .venv → ~/work/acme/.venv
└── .env → ~/work/acme/.env
```

- **Every project this machine has**, the root project's included, is a git worktree on `dev/<name>`, from the
  commit its clone is on. A project switched off here is left out.
- **The root project's files** — `workspace.toml` and what its `harness.toml` places — come from the worktree's own
  root project, so an edit of `TASKS.md` there lands on its branch.
- **The workspace's venv, `.env`, MCP servers, shell file, rules and Claude Code settings** are links to the
  workspace's own.
- **The worktree's code is what runs.** The venv's installs point at the main checkouts, so a session in a worktree
  gets the worktree's project folders first on its `PYTHONPATH` (the `SessionStart` hook). In a shell of your own:

  ```bash
  eval "$(aisland workspace worktree env)"
  ```

## The commands

Claude Code runs them through the hooks; each also works by hand.

```bash
aisland workspace worktree create bold-oak
```

```text
  - acme: a worktree on dev/bold-oak
  - models: a worktree on dev/bold-oak
/Users/you/work/acme-worktrees/bold-oak
```

The folder is the one line on stdout; the progress goes to stderr. A name is letters, digits, `.`, `_` and `-`; a
name in use, or a `dev/<name>` branch a project already has, is refused before anything is made.

```bash
aisland workspace worktree list
```

```text
bold-oak  /Users/you/work/acme-worktrees/bold-oak
    uncommitted work: models
    commits on dev/bold-oak: acme (2)
```

```bash
aisland workspace worktree remove ~/work/acme-worktrees/bold-oak
```

```text
kept dev/bold-oak in acme: 2 commits since the worktree was made
```

`remove` takes each project's worktree and the folder away, and deletes each `dev/<name>` branch that got no commit;
a branch with commits is kept and named, to merge or push. It removes nothing while a project holds uncommitted work,
or the folder holds a file of no project:

```text
Error: /Users/you/work/acme-worktrees/bold-oak is kept: uncommitted work in models (1 file) — commit or discard it, then remove the worktree again
```

Each verb takes `--hook`, which reads the JSON Claude Code hands a hook on stdin: `create` the worktree's name,
`remove` its folder, `env` the session's folder (and appends the line to the file `$CLAUDE_ENV_FILE` names).

## Limits

- Inside a worktree the git aliases still name the workspace's root: `git status-all` typed in a worktree's project
  shows the main checkouts.
- Claude Code keeps a subagent's worktree when the subagent ends; `list` shows it, `remove` takes it away.
- A worktree's `PYTHONPATH` lists the project folders, as the editor's `extraPaths` do; a project whose package lives
  elsewhere in its folder (a `src/` layout) still imports the main checkout's.
