# `workspace.toml`: the workspace written down like `.gitmodules`, kept current by setup

*2026-10-22* [workspace-file] — supersedes the `<name>.workspace.toml` written by `aisland workspace export`.

## Context

Setting a workspace up on another machine meant knowing three things nobody had written down: how to make a venv
aisland works from, how to install aisland into it, and every project's address and branch, typed one `--add` at a
time. The first answer, of 2026-10-20, was a `<name>.workspace.toml` holding both halves — how-to comments for a
machine without aisland, then a `[projects]` table of `name = { url = …, branch = … }` (with an `alias`) for
`init --from`. It was written on request (`aisland workspace export`) and read only by `init --from`, so it went stale
the moment a submodule was added, and it sat beside `config.toml` as a second committed description of the same
workspace in a second format.

## Decision

One file, `workspace.toml`, at the root and committed, shaped like `.gitmodules`: the how-to comments, `[workspace]`,
then a `[project."<name>"]` per project with `path`, `url` and `branch`. `aisland setup` keeps the projects current
(`workspace.file.refresh`): new ones added, a changed url or path followed, a branch written once kept (a project
the workspace lacks: the amendment below). `init --from` clones from it and copies it in. `export` is gone. The later
steps of the redesign moved `config.toml`'s settings into the same tables, each a key of the plugin that owns it.

What the 2026-10-20 file got right carried over. The how-to stays comments, because nothing can read the file before
aisland is installed. TOML, because the standard library reads it. A file `init --from` refuses leaves the folder as
it was, every refusal naming the file and the line (the old `[projects]` layout among them). And there is no
venv-name setting: `.venv` is read in some twenty places, a name of one's own is had with `--prompt` or a link, and
`init`'s `.gitignore` writes `.venv` rather than `.venv/` so a link is ignored too.

Rejected: keeping `export` (a file nobody refreshes is wrong by the next submodule); generating `.gitmodules` from
the file (git and every git tool read `.gitmodules`; the file follows it instead); keeping a project gone from the
workspace with a flag (its entry would clone a project the workspace no longer has); a script that runs the
how-to's steps (they differ per machine: uv or pip, PyPI or a wheel).

## Consequences

- Every workspace has the file after its next `aisland setup` (the workspace aisland is developed in: 29 projects, 173 lines).
- A branch changed in `.gitmodules` does not reach the file once a branch is written there; the source plugin
  changes it (`TASKS.md`).

## Example

```toml
[project."models"]
path = "models"
url = "git@github.com:acme/models.git"
branch = "dev/main"
```
```python
from aisland import workspace, workspace_file
workspace.file.refresh(Path("~/work/acme").expanduser())   # ['workspace.toml: added models']
workspace.init_workspace(Path("acme2"), from_file=Path("~/work/acme/workspace.toml").expanduser())
```

## What you may change

- The how-to comments (`workspace.file._header`) and the wording of `refresh`'s lines.
- A new key of a project belongs to a plugin of the redesign, never to `workspace/file.py`.

**Amendment (2026-10-28, release review): a row with a url is a project to add, and setup leaves projects clean.**
Writing `[project."lib"]` with a url into the file and running setup dropped the row as "no longer in the workspace" —
and with it the file, when that was its only row. The activation step already cloned any listed project the
workspace lacked (the optional ones); `refresh` had removed this one before it got there. Now `refresh` keeps a listed
project with a url the workspace lacks, saying `lib is not in the workspace yet; setup adds it`, and only a row
nothing could clone is dropped. The cost, accepted by the user: a project leaves the workspace only when its row
goes too (a `git rm` alone is undone by the next setup). In the same review, setup stopped dirtying the projects it
sets up: it created a `.gitignore` in each one for its `pyrightconfig.json`, and the editable install left
`<name>.egg-info/` — two untracked entries that made `aisland wheels build` refuse every project needing it. Both are
now lines in the project's own `.git/info/exclude` (`git_exclude.exclude`, which the agent harness already used for
its link): per machine, never committed, and no project file is created or edited. A `/pyrightconfig.json` line a
project already committed stays; it is the project's.

```python
git_exclude.exclude(project_dir, ["pyrightconfig.json", "**/*.egg-info/"])
# <project>/.git/info/exclude gains:  /pyrightconfig.json   /**/*.egg-info/
```

---

[The architecture and every decision](../architecture.md#the-decisions)
