# A folder of projects is a workspace: its subfolders are read as submodules (2026-10-19)

## Context

Every project list aisland kept came from `.gitmodules`. A folder of projects without one — plain
folders, or each project its own git clone — was no workspace at all. Measured with aisland from a
wheel in `acme/{alpha,beta}` (beta needing alpha), no git anywhere:

```text
$ aisland status
Error: No workspace here: …/acme is not inside a git repository. …
$ AISLAND_WORKSPACE=…/acme aisland wheels build beta
Error: no project 'beta' in the workspace …/acme
```

The maintainer asked for such a folder to work "as if that directory contains all subdirectories as
submodules", `git pull-all` and `git push-all` included.

## Decision

- **The project list** is `projects.workspace_folders`: `.gitmodules`' submodules when it exists, else
  every direct, non-hidden subfolder holding a `pyproject.toml`, active, named by its folder. Readers
  of "the projects" (wheels, the test statistics) use it; `submodule_active_map` stays what it says —
  submodules — for what only a submodule has (activation, deinit, a `.gitmodules` remote).
- **Finding the workspace** (`locations.find_workspace`), first hit upwards from where aisland runs:
  `.gitmodules`; the start folder when it holds projects; a folder holding a `pyproject.toml` (a
  project — the workspace is its parent, so a project that is its own clone is never the
  workspace); a `.git` folder (a repository with no project yet). The "holds projects" test looks at
  the start folder only.
- **What needs a submodule is refused, naming the reason**: a `github`/`pypi` source empties the
  folder and has nothing to check it out again from; a wheel is built from a commit, so a project in
  no git repository has none.
- **The git aliases**: without `.gitmodules`, every subfolder that is its own clone, and the root
  when it is a repository, gets `pull-all` and `push-all`, the root written into the alias (from
  inside a clone git cannot find it). `pull-all` merges each clone with origin on its OWN branch —
  independent clones share none, where submodules follow the root's — then the root; `push-all`
  pushes each clone, then the root only when all pushed. The other aliases (`status-all`,
  `switch-all`, `merge-all`, `tag-all`, `safe`) fan out with `git submodule foreach` in a
  superproject; here `git_folder_all.sh <verb>` loops over the same clones, and `safe` takes the
  workspace's venv and runs mypy from its root, which a clone cannot find on its own.

Rejected: generating a `.gitmodules` for the folder (it would claim submodules git does not have,
and the root may not be a repository); every ancestor holding projects counting as a workspace (a
home folder holding one project would become the workspace of every folder under it); building a
wheel from a folder outside git (the wheels record's reason — a working tree carries what a commit
does not).

## Consequences

- A workspace with `.gitmodules` reads exactly as before; a stray non-submodule folder in it is
  still not a project.
- From a folder that is neither a project nor its parent (`acme/docs`), there is no workspace:
  `AISLAND_WORKSPACE` names it.
- The clones' aliases name the scripts and the root by path: moving the workspace or recreating the
  venv needs `aisland setup` again.

## Example

```text
$ cd acme/beta && aisland setup        # among its steps:
==> Configuring Git Aliases (no .gitmodules: the clones alpha, beta)...
$ git pull-all
fetching 2 repositories…
alpha                  merged origin/main (1 commit)
```

## What you may change

- Which subfolders count as projects (`workspace_folders`, `locations.holds_projects` — keep the two
  the same test), with `tests/test_folder_workspace.py`.
- Which aliases a folder workspace gets, with `tests/test_folder_git_aliases.py`.
- Never: a second project list beside `workspace_folders`, or `.gitmodules` overruled by the folders.

---

[The architecture and every decision](../architecture.md#the-decisions)
