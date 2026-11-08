# A root's two choices: a local repository (`--local`), projects it does not record (`track unset`)

*2026-10-23* [local-workspace]

## Context

`aisland workspace init` always made a root repository and added each project as a submodule, and refused a folder
that already was one. A workspace whose projects are separate repositories, with no root anyone shares, then showed
every starting file as untracked (`?? AGENTS.md`, `?? mypy.ini`, …) and every project's new commit as
` M <project>`, and `git pull-all` committed `chore: sync submodule pointers` into a root nobody reads. A shared root
wanted the second half alone: its projects' commits neither shown nor recorded. Dropping the root
repository (a folder of clones) would lose the per-machine settings kept in its `.git/config` (the editor's theme,
the `active` choices, mirrors) and a `git pull-all` typed in the root.

## Decision

Two independent choices. `init --local` makes a new root that tracks only `.gitmodules` and the projects (`.gitignore`
`/*`, marked `aisland.workspace.local` in `.git/config`, the first commit made by `workspace.local.settle`, which
commits `.gitmodules` and the projects again whenever they change). Tracking is per project, git's own
`ignore = all`: `aisland workspace track unset|set [PROJECT…] [--machine-only]` writes the committed choice to
`workspace.toml` (`track = false`) and `.gitmodules`, or this machine's to `aisland.track.<project>.track` and
`.git/config`; `init --untracked` untracks every project; `git pull-all` never records an untracked submodule. `init`
in an existing repository sets it up in place instead of refusing it (`--from` and `--local` refused there).

## Consequences

- `git status` in the root no longer shows an untracked project; every per-machine setting and `git pull-all` from
  the root keep working.
- The root keeps the commit it last recorded: a `git submodule update` typed there would put the project back on
  it, detached. aisland runs it only for an empty folder, so it never does.
- `git add <project>` skips an untracked project with a hint suggesting `--force`; `/commit-all` and
  `/commit-session` are told not to take it.
- `git submodule add` refuses a `--local` root's ignored path without `-f`; the `active` plugin passes it, a person
  types it.
- An untracked project follows its branch: on no branch (a fresh clone's `git submodule update`), `aisland setup`,
  `track unset` and `git pull-all` fetch it and switch it to `.gitmodules`' branch (`.` = the root's), else origin's
  default — never forced. So a shared root's recorded commits stop mattering for its untracked projects: a new
  machine needs only the clone and `aisland setup`.
- `track status` reads the two layers and git's `ignore` back and writes nothing. It prints the workspace's choice and
  only the projects that differ from it or that git does not yet agree with: a workspace of 31 alike projects printed 31
  identical lines, and the project `git status` still shows is the line worth reading. Two or more alike are counted.

## Example

```text
$ aisland workspace init . --untracked
==> Set up workspace acme in /Users/you/work/acme
    .git/                     kept (an existing repository)
    ...
    models                    untracked: the root records none of its commits
$ aisland workspace track set models --machine-only
==> tracked on this machine: models (nothing committed)
  - models: tracked on this machine
$ aisland workspace track status
==> the workspace default: untracked ([workspace] track = false in workspace.toml)
  - models: tracked (this machine's aisland.track.models.track = true; git: applied)
  - tools: untracked ([workspace] track = false in workspace.toml; git: applied)
```

## What you may change

- The commit message, the report rows and the wording of the `track status` lines.
- The commands are one group, `track set`, `track unset` and `track status`. `unset` is `set` to false: it writes
  `track = false` and never removes a setting, and a bare `track` changes nothing.
- Never: `update = none` for the projects (setup would no longer check out an optional one), a pull-all that
  records an untracked submodule, or an `ignore` value aisland did not write overwritten.

---

[The architecture and every decision](../architecture.md#the-decisions)
