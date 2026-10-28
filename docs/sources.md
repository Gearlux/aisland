# Where each project comes from

This page covers how a workspace project is consumed (a local checkout, a branch or tag of its repository, or a published wheel) and how a submodule is mirrored to a second host.

## Project Sources (`aisland workspace configure source`)

Each workspace project can be consumed either as a **local checkout** (developed here, installed
editable) or as a **published wheel** (not checked out at all). `aisland workspace configure source` owns that choice
and drives every artifact that has to agree with it — the checkout, the shared `.venv` and the
`*.code-workspace` folder list. It never touches a CI file: a runner installs what is **released**,
whatever you checked out (see [What CI does with all this](ci.md#what-ci-does-with-all-this)).

There are **three kinds**. `local` and `github` both take a `--branch` (a branch or tag; without one, the project's
own branch in `workspace.toml`) and differ only in whether a working tree survives; `pypi` takes a `--version`
instead:

| source | directory | `.venv` | `*.code-workspace` |
|---|---|---|---|
| `local` | checked out, `git switch <branch>` | `uv pip install -e models[dev,…]` | listed |
| `github` | **deinit'd — empty** | `uv pip install git+…@<branch>` | **not listed** |
| `pypi` | **deinit'd — empty** | `uv pip install -U lib` | **not listed** |

The `local` install target is the one `aisland setup` computes for that checkout — its `dev` extra
plus every framework/tracker extra this machine selected (`models[dev,lightning]`) — so switching a
project to `local` never drops a test dependency or a training backend from the venv.

**CI never sees your source or your `--branch`.** They describe this machine; the CI files are
tracked and shared — see [What CI does with all this](ci.md#what-ci-does-with-all-this).

Pick `local` when you edit the project here, `github` when you want a specific branch or tag but
no working tree, `pypi` when a released wheel will do. The kinds are named to be told apart at a
glance because `github` and `pypi` run `git submodule deinit` and empty the project's directory.

A project with **no entry** is *unmanaged*: nothing is switched, nothing is installed
differently. Setting the first source is the only thing that
changes behaviour.

```bash
aisland workspace configure source                                         # what is set, and what is on disk
aisland workspace configure source set models local  --branch dev/main    # develop it here, on dev/main
aisland workspace configure source set tools github --branch dev/main    # pin to that branch, no checkout
aisland workspace configure source set lib pypi                        # consume the published wheel
aisland workspace configure source set lib pypi --machine-only         # this machine only; nothing committed
aisland workspace configure source unset lib                           # back to unmanaged
```

`aisland setup` applies the sources on every run.

```text
$ aisland workspace configure source
  PROJECT COMMITTED        OVERRIDE  EFFECTIVE        STATE
  lib     pypi             -         pypi             empty
  tools   github:dev/main  -         github:dev/main  empty
  models  local:dev/main   -         local:dev/main   on dev/main
```

The `github` remote URL is read from `.gitmodules`, not assumed to be on GitHub, so
it also works for the submodules hosted elsewhere; an `scp`-style `git@host:org/repo` remote is
rewritten to `git+ssh://git@host/org/repo` because pip cannot parse the scp form.

## The two configuration layers

The same split every aisland setting has (`workspace.toml` committed, `.git/config` per machine):

| layer | where | committed? |
|---|---|---|
| default | the project's table in `<workspace>/workspace.toml` | **yes** — shared between machines |
| override | `git config --local aisland.source.<project>.<key>` | no — per machine |

**Which layer drives what is load-bearing.** Every downstream artifact except the venv, the
checkout and the generated workspace file is itself a committed file, so a machine-local choice
must never reach one:

- **neither layer reaches CI** → `Jenkinsfile` and `ci.yml` read no source setting: they ask PyPI
  whether a dependency is released (see [What CI does with all this](ci.md#what-ci-does-with-all-this)).
- **effective source** (override, else committed) → submodule init/deinit + branch switch, the
  editable/git/PyPI install into the shared `.venv` (the editable one with the same `[dev,…]` extras
  `aisland setup` installs), and — through the checkout — the folder list of the generated
  `*.code-workspace` file.
- **no setting at all** → `Jenkinsfile.local`, which installs nothing and tests the venv as setup left it.

## The source settings in `workspace.toml`

Written by `aisland workspace configure source set` into the project's own table; nothing is written that the table already says:

```toml
[project."lib"]
url = "git@github.com:acme/lib.git"
branch = "dev/main"          # the project's branch: what local checks out and github installs from
source = "github"            # "local" | "github" | "pypi"
# version = ">=0.1.0"        # pypi only; omitted => latest
# distribution = "lib-x" # only when the package name differs from the project's
```

This machine's own choice goes to `.git/config`, one key each: `aisland.source.lib.source`, `.branch`,
`.version` — a missing `.branch` is the project's.

## Notes

- Both detaching kinds are verified **before** anything is emptied, because otherwise the failure
  would land after the directory was already gone:
  - `pypi` — the name must exist on PyPI **and** its published metadata must point at
    `github.com/<owner>/…`, the GitHub owner of the project's own remote (a project whose remote is not on
    GitHub cannot be matched to a release and is refused). PyPI names are global, so a bare existence check would let a workspace
    project whose name is taken by someone else install that stranger's package into the shared
    venv.
  - `github` — the `--branch` must resolve on the project's remote (`git ls-remote`).

  An unreachable index or remote warns and continues rather than blocking an offline machine.
- `git switch` and `git submodule deinit` are never forced: a submodule with uncommitted work
  refuses, is reported, and neither the venv nor `workspace.toml` is changed — a failed apply rolls
  the configuration back so the configuration never claims a state the tree does not have.
- `aisland workspace configure source set` / `unset` never regenerate a CI file — see
  [What CI does with all this](ci.md#what-ci-does-with-all-this).
- `aisland setup` applies the configuration on every run, so a fresh clone converges without any
  manual step.

## Project Mirrors (`aisland workspace configure mirror`)

A submodule can be kept on a second host — a GitHub repository and a Bitbucket one, say. The
remote `.gitmodules` names stays the **primary**: you clone, pull and push it exactly as before.
The **mirror** only receives a copy.

```bash
aisland workspace configure mirror set tools git@github.com:Org/tools.git
```
```
==> tools: mirror → git@github.com:Org/tools.git
    recorded in workspace.toml (commit it); remote `mirror` added to tools
    Note: each sync copies tools' dev/main, main, every other branch checked out here and tags from bitbucket.example.com to github.com — make sure its code may live there.
    Push to it with: aisland workspace mirror sync tools
```

`aisland workspace mirror sync` pushes `dev/main`, `main`, every other branch the project's checkout has, and
the tags to every mirror (or to the one project named). Each branch goes as the primary has it: a commit you have not
pushed does not reach the mirror, a local branch the primary has not got is reported `not on the primary`, and a
branch that exists only on the primary is not copied. The `/push-all` and `/ship` agent commands that `aisland setup` installs into the workspace run it as their last step, so after the first
`set` you rarely type it.

```
$ aisland workspace mirror sync
==> tools → git@github.com:Org/tools.git
    dev/main    pushed (3 commits)
    main        in sync
    dev/parser  pushed (new branch)
    scratch     not on the primary
    tags        pushed v0.4.0, v0.4.1
```

| command | effect |
| --- | --- |
| `aisland workspace configure mirror` | each mirror, its committed URL, this machine's override, and what a sync would push (`dev/main 3 behind · main in sync · 2 tags missing`) — pushes nothing |
| `aisland workspace configure mirror set <project> <url>` | record the mirror as the project's `mirror` in `workspace.toml` and add the remote `mirror` to the checkout |
| `aisland workspace configure mirror set <project> <url> --machine-only` | this machine only (`.git/config aisland.mirror.<project>.mirror`); nothing committed |
| `aisland workspace configure mirror unset <project> [--machine-only]` | drop the mirror (or only the override) and the remote aisland added; the mirror host is not touched |
| `aisland workspace mirror sync [<project>]` | push; exit 1 when anything was refused or a host could not be reached |

**What a sync never does.** It never forces and never deletes:

- a branch the mirror has commits on that the primary does not (someone pushed to the mirror) is
  **refused**, naming the commit and the merge that brings it to the primary:
  ```
  ==> app → git@github.com:Org/app.git
      dev/main  REFUSED: the mirror has 1 commit the primary does not (a1b2c3d "fix: …")
                merge it first: git -C app fetch mirror && git -C app merge mirror/dev/main
  ```
- a tag of the same name pointing elsewhere is refused, never overwritten;
- a branch or tag that exists only on the mirror is left alone.

**What is copied is the primary, not your checkout.** A commit you have not pushed to the primary
does not reach the mirror either, so the two hosts can only differ by the mirror being behind.

**Credentials.** Every fetch, `ls-remote` and push runs with `GIT_TERMINAL_PROMPT=0`: a host
whose credentials are missing fails the sync with git's message rather than waiting on a
password prompt. Use an SSH key, or an HTTPS token in `~/.netrc`, for both hosts.

**Notes**

- Only submodules are mirrored; the root repository is not.
- A project with no checkout here (its source is `github` or `pypi`) is skipped.
- `aisland workspace configure mirror set` refuses a project whose checkout already has a remote named `mirror` that
  aisland did not create, and prints the `git remote rename` that frees the name.
- `aisland setup` adds the remote for every committed mirror and removes the one aisland added
  for a mirror unset on another machine. Why the mirror is push-only: [architecture.md](architecture.md),
  "A mirror is push-only, fast-forward-only, and copies the primary".
