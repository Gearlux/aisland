# A root project holds the workspace's files, and nothing records the projects' commits

*2026-11-21* [root-project] (follows "A root's two choices: a local repository, projects it does not record")

## Context

A workspace's shared files — `workspace.toml`, the rules agents read, the backlog — had one home: the root repository.
The root is also a superproject, so it records which commit of each project goes with which. Once aisland cloned the
projects from `workspace.toml` and kept each untracked one on its branch, that record stopped meaning anything, and
it kept costing commits. Measured on the workspace aisland is developed in: 738 of the root's 1258 commits (59 %) only
synced submodule pointers, 81 of the 136 made since 2026-10-21. Its pointers were stale anyway: with `track = false`
the root recorded commits 74, 60, 59 and 50 commits behind the branches of the four most active projects. A `--local`
root records nothing anyone sees, but then the shared files have no shared home.

The maintainer asked for a root project: a repository checked out into a folder like every other project, from which
`aisland setup` brings the rest, holding the workspace's instructions and `workspace.toml`, with no submodule pointer
to keep track of.

## Decision

- **The root project is a clone whose `workspace.toml` names it**: `[workspace] root = "<project>"`, its own row in
  the file's projects. `locations.root_project` finds it, standard library only: the folder `<root>/workspace.toml`
  names, else the one direct subfolder whose own file (a file, not a link) names that subfolder. `find_workspace`
  takes a root project's parent as the workspace, and a folder holding one as a workspace.
- **No root repository, no `.gitmodules`.** Every project, the root project included, is a plain clone holding its
  own repository. `root_project.rooted` is the test every step asks.
- **`workspace.toml` is the root project's, linked into the folder** (`root_project.link`, setup's first step after
  the venv, before the harness reads `[harness] sets`). Every write follows the link (`atomic.write_text`), so the
  change lands in the root project to be committed there. A file of the folder's own, or a link elsewhere, is refused.
- **The file wins.** It is what every machine shares, so `refresh` never writes a clone's `origin` over a row's url,
  and keeps every row whether this machine has the project or not; setup's activation step clones each active
  project the folder lacks (`git clone -b <branch> -- <url> <path>`) and points each clone's `origin` at its row's url.
- **A project switched off, or taken from its release, keeps its clone.** A plain clone's repository lives nowhere
  else: emptying its folder would delete it. The model leaves it out of every install instead (`Workspace.left_out`).
- **This machine's settings live in the root project's `.git/config`** (`machine_config.settings_repository`, and the
  pytest plugin's own reader): still this machine's, never committed.
- **The root project is a project for git only.** It is never installed, never in the editor's or the type checker's
  project list — whatever it holds —, and `aisland workspace projects` lists it last, so the agent commands that walk
  the list commit and ship it after the projects. The git aliases already took every clone.
- **The rules and files it brings are a harness set**: without `[harness] sets`, the sets are `["aisland", "<root>"]`.
  A set that lives in the workspace (a project's, a folder's) is linked even from a wheel: a copy would keep an edit
  made through the workspace from ever reaching it.
- **`track` is refused** — nothing records commits — and a mirror goes on a clone, its primary the row's url.
- **`aisland workspace convert`** turns a workspace of submodules into one: each repository moves from
  `.git/modules/<name>` into its folder (`core.worktree` dropped, worktrees repaired), this machine's `aisland.*`
  settings are copied into the root project, and the old root repository with the files it tracks moves into the
  backup store (`<backups>/.workspace-root/<stamp>/files/`), with the repositories of the projects that are off on this
  machine. Refused before anything moves: no root project cloned, work the root holds that no remote has, a worktree
  of the root, a project holding submodules of its own. It moves into the store as `aisland clean` does, so it is a
  top-level module beside `clean`.

Rejected: keeping a local root repository with every project an untracked submodule (the first design: it keeps
`.gitmodules` as a second list beside `workspace.toml`, and `refresh` copying `.gitmodules` into the file would undo
another machine's change); linking the root's `AGENTS.md` to the root project's (its `@.agents/rules.md` import is
relative, and a session started inside the root project would read the rules twice); an `init --root <url>` command
(`git clone` and `aisland setup` are the whole start).

## Consequences

- A workspace with a root project writes no root commit: `git pull-all` and `git push-all` treat the root project as
  one more clone, and the agent commands skip every root step.
- The folder is no git repository: a tool that needs one at the workspace's root (an editor's worktree feature) finds
  none.
- An off project's clone stays on disk; deleting it is the person's call.
- A row removed on another machine leaves this machine's clone as it is, out of the installs.

## Example

```text
~/work/acme/                    ← no repository
├── acme/                       ← the root project: workspace.toml names it [workspace] root
├── workspace.toml → acme/workspace.toml
├── models/                     ← a clone setup made
└── .venv
```

```bash
mkdir ~/work/acme && cd ~/work/acme && git clone git@github.com:acme/acme.git
uv venv --seed .venv && .venv/bin/pip install aisland && .venv/bin/aisland setup
```

## What you may change

- The wording of the lines and refusals, and which settings `convert` leaves behind (`convert._LEFT_BEHIND`).
- Never: a root repository recording commits beside a root project, a clone's origin written over the file's url, or
  an off project's clone emptied.

---

[The architecture and every decision](../architecture.md#the-decisions)
