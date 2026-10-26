# Agent commands and rules

This page covers the agent commands and generic agent rules that aisland installs into every workspace.

## Agent commands and rules in every workspace (the harness)

`aisland setup` and `aisland workspace init` give every workspace the same agent commands and the same generic agent
rules, so Claude Code (and Gemini CLI) behave alike in all of them:

| In the workspace | What it is |
| --- | --- |
| `.agents/workflows/*.md` | the commands `/commit-all`, `/commit-session`, `/list-actions`, `/pull-all`, `/push-all`, `/ship` |
| `.claude/commands` | a link to `.agents/workflows`, where Claude Code looks for them — at the root and in every project folder (Claude Code reads only the folder a session starts in); a project's link is kept out of git through its repository's own `.git/info/exclude` |
| `.agents/scripts/stage_hunks.py` | the helper `/commit-session` runs to stage only some hunks of a file |
| `.agents/aisland/AGENTS.md` | the generic rules; the workspace's `AGENTS.md` imports them with its first line |
| `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` | written once, when the workspace has no `AGENTS.md`: the import line, then room for the workspace's own rules; the other two link to it |

From an aisland **checkout** (a clone of its repository inside the workspace) each file is a link into aisland's package, so an edit is made in aisland and committed
there; from a **wheel** each is a copy. Every installed file is listed in the root `.gitignore` by its own path, so a
command of the workspace's own in `.agents/workflows/` is never touched and can be committed.

A copy someone edited is kept, and setup says so:

```text
$ aisland setup
  - kept your edited .agents/workflows/ship.md — delete it to take aisland's
```

An `AGENTS.md` of the workspace's own is never edited; when it does not import the rules, setup names the line:

```text
  - AGENTS.md does not import the aisland rules — add "@.agents/aisland/AGENTS.md" as its first line
```

The rules and commands live in the `harness/` folder of the aisland package (`AGENTS.md`, `commands/`, `scripts/`); why they are installed this
way: [architecture.md](architecture.md) → "Every workspace gets aisland's agent commands and rules".
