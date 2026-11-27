# Agent commands and rules

This page covers the agent harness — the commands, helper scripts and rules every AI coding session in the
workspace reads — and how a workspace brings its own, combines several, and takes them from other projects.

## Agent commands and rules in every workspace (the harness)

`aisland setup` and `aisland workspace init` install the workspace's **harness sets**. A workspace that names none
gets aisland's own:

| In the workspace | What it is |
| --- | --- |
| `.agents/workflows/*.md` | the sets' commands — aisland's: `/commit-all`, `/commit-session`, `/detach`, `/export-session`, `/import-session`, `/list-actions`, `/pull-all`, `/push-all`, `/ship` |
| `.claude/commands` | a link to `.agents/workflows`, where Claude Code looks for them — at the root and in every project folder (Claude Code reads only the folder a session starts in); a project's link is kept out of git through its repository's own `.git/info/exclude` |
| `.claude/skills/<skill>` | a link to each skill an installed project ships (`<project>/.claude/skills/<skill>/SKILL.md`, the root project's too), so a session started at the root offers it; the link goes once no installed project ships the skill. A skill two projects ship is linked from neither, and a folder of the workspace's own is kept; setup says both |
| `.agents/scripts/*` | the sets' helper scripts — aisland's: `stage_hunks.py`, which `/commit-session` runs, and `session_handoff.py`, which `/export-session` and `/import-session` run |
| `.agents/<set>/AGENTS.md` | each set's rules — aisland's in `.agents/aisland/AGENTS.md` |
| `.agents/rules.md` | an import of every set's rules, in the order the sets are listed; the workspace's `AGENTS.md` imports it with its first line |
| `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` | written once, when the workspace has no `AGENTS.md`: the import line, then room for the workspace's own rules; the other two link to it |
| anything else a set places | where its `harness.toml` says, such as Cursor's `.cursor/rules/` |

From an aisland **checkout** (a clone of its repository inside the workspace) each file is a link to its set's own
file, so an edit is made in the set; from a **wheel** each is a copy — but a set that lives in the workspace (a
project's, a folder's) is linked either way, since it is edited and committed where it lives. Every installed file is listed in the root
`.gitignore` by its own path, so a command of the workspace's own in `.agents/workflows/` is never touched and can be
committed.

A copy someone edited is kept, and setup says so:

```text
$ aisland setup
  - kept your edited .agents/workflows/ship.md — delete it to take aisland's
```

An `AGENTS.md` of the workspace's own is never edited; when it does not import the rules, setup names the line:

```text
  - AGENTS.md does not import the harness's rules — add "@.agents/rules.md" as its first line
```

## Choosing and combining sets

`workspace.toml` lists the sets, in order:

```toml
[harness]
sets = [
  "aisland",                                                   # aisland's own
  "models",                                                    # a project of the workspace
  "agents",                                                    # a folder of the workspace
  "acme-harness",                                              # a set an installed package registers
  { git = "https://github.com/acme/agent-rules", ref = "v2" },  # a repository elsewhere, at a tag, branch or commit
]
```

| A name in `sets` | The set |
| --- | --- |
| `"aisland"` | aisland's own set, in the package |
| a project of the workspace | the folder its `pyproject.toml` names in `[tool.aisland] harness`, else the project folder itself |
| a folder of the workspace | that folder |
| a name an installed package registers | the package holding the set, registered through the `aisland.harnesses` entry point |
| `{ git = "<url>", ref = "<ref>" }` | the repository, cloned into aisland's state folder at `ref`, fetched again only when the url or the ref changes; `name = "…"` names it, else the repository's name does |

Without `[harness]` the list is `["aisland"]` — in a workspace with a root project, `["aisland", "<the root project>"]`
([workspace](workspace.md#a-workspace-with-a-root-project)); `sets = []` installs nothing, and takes away what an earlier install
put. A set dropped from the list takes its files and its import with it. A git set needs the network the first time
and whenever its `ref` changes; an unreachable repository stops setup, naming the line.

A project brings its set by declaring it, with no aisland code:

```toml
# models/pyproject.toml
[tool.aisland]
harness = "harness"          # the folder of this project holding its set
```

**Two sets that place a file at the same path are refused** before anything is installed:

```text
Error: /Users/you/work/acme/workspace.toml:14: two harness sets ship .agents/workflows/ship.md: aisland and models — exclude it from one of them in [harness] sets
```

Any entry can leave out some of its set's files, as patterns of the set's own paths:

```toml
sets = [{ set = "aisland", exclude = ["commands/ship.md"] }, "models"]
```

An `exclude` that names no file of its set is refused, so a typo is not silently ignored.

## A set of your own

A set is a folder. A `harness.toml` in it says what it brings:

```toml
# agents/harness.toml
format = 1                            # the shape of this file; an aisland too old for it refuses the set
rules = "rules/AGENTS.md"             # lands in .agents/agents/AGENTS.md, which .agents/rules.md imports
commands = ["commands/*.md"]          # into .agents/workflows/
scripts = ["scripts/*.py"]            # into .agents/scripts/

[files]                               # any other file: a pattern of the set → a place in the workspace
"cursor/*.mdc" = ".cursor/rules/"     # a place ending in / is a folder
```

`format` may be left out (a set without it is format 1); an aisland that reads an older format refuses the set,
naming the line and `pip install --upgrade aisland`.

A folder without a `harness.toml` is read where agents read their files, so a project that knows nothing of aisland
can be a set: `AGENTS.md` (else `CLAUDE.md`) as its rules, `.claude/commands/*.md` as its commands,
`.cursor/rules/*` and `.github/copilot-instructions.md` as they are.

Start from a copy of aisland's own set:

```bash
aisland workspace harness export agents     # aisland's set, harness.toml included, into a new folder agents/
```

## What the workspace has

```bash
aisland workspace harness            # each set: where it comes from, its rules and every file it places
aisland workspace harness --json     # the same, as one JSON object
aisland workspace harness install    # install the sets now, as aisland setup does
```

```text
aisland  (aisland, …/site-packages/aisland/harness)
    rules  .agents/aisland/AGENTS.md
    .agents/workflows/commit-all.md
    …
agents  (folder, /Users/you/work/acme/agents)
    rules  .agents/agents/AGENTS.md
    .agents/workflows/review.md
    .cursor/rules/style.mdc
```

aisland's own set lives in the `harness/` folder of the aisland package (`harness.toml`, `AGENTS.md`, `commands/`,
`scripts/`); why the harness is built this way: [the decision record "A workspace combines harness
sets"](decisions/0075-a-workspace-combines-harness-sets.md).
