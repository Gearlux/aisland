# aisland

**One workspace of many Python projects, set up the same way on every machine.** aisland keeps a git repository
whose projects are submodules, installs every project editable into the workspace's one virtual environment, and
gives the workspace what its projects share: git aliases that pull and push them all, local services (Jenkins,
MLflow, …) under one supervisor, generated CI files, an environment check, and the agent rules every AI coding
session in the workspace reads.

## Install

```bash
pip install aisland
```

aisland runs on macOS and Linux with Python 3.12 or newer. `aisland setup` installs the system tools a workspace
needs (process-compose for the services; through Homebrew on macOS). Two extras add optional parts:

| Extra | Adds |
|---|---|
| `aisland[mcp]` | `aisland-mcp`, an MCP server that offers a chosen set of aisland's commands to an AI client |
| `aisland[connect]` | `aisland tunnel connect`, a terminal on a remote machine over its VS Code tunnel |

Installing aisland also registers a pytest plugin, `aisland_test_stats`, which every `pytest` in that environment
loads. It records each run of a project — its seconds and each test's outcome — in `.aisland/data/test-stats/` in
the folder **above** the project: the workspace, or for a project elsewhere its parent folder (`~/code/app` records
into `~/code/.aisland/`). `aisland tests report` reads the record. Turn it off with `aisland workspace test-stats off [PROJECT…]
[--machine-only]`, or for one run with `pytest -p no:aisland_test_stats`; what it writes, and every way to switch it
off:
[tests](https://github.com/Gearlux/aisland/blob/main/docs/tests.md#the-pytest-plugin).

## A first workspace

```bash
aisland workspace init ~/work/acme --add git@github.com:acme/models.git
cd ~/work/acme
uv venv --seed .venv && .venv/bin/pip install aisland
.venv/bin/aisland setup
```

`init` creates the repository and its starting files and adds each `--add` project as a submodule:

```text
==> Created workspace acme in /Users/you/work/acme
    .git/                     git init
    models/                   submodule git@github.com:acme/models.git
    .gitignore                written
    .env                      written
    mypy.ini                  written
    vscode-init-bash.sh       written
    workspace.toml            the projects, how to start another machine from them
    git aliases               pull-all, push-all, … in the local git config
    .agents/                  aisland's commands and agent rules (`.claude/commands` links to them)
    AGENTS.md                 written (imports the rules; CLAUDE.md and GEMINI.md link to it)
```

`aisland setup` then installs the system tools, installs every project editable into `.venv`, and writes the
editor's workspace file and `project.bashrc`, the shell start-up file:

```bash
source project.bashrc        # activates .venv and exports .env
aisland completion install   # shell completion for every tool in .venv; project.bashrc loads it from now on
git pull-all                 # every project and the workspace, merged
aisland doctor               # the environment checks, each with the command that fixes it
```

`init` never writes over a file that exists, and `setup` can be run again at any time: it brings the workspace in
line with `workspace.toml` and the projects' own declarations.

## The same workspace on another machine

`workspace.toml` is committed: it lists every project (its folder, its repository, its branch) and starts with the
commands that create the workspace on a machine that has no aisland yet:

```toml
[workspace]
name = "acme"

[project."models"]
url = "git@github.com:acme/models.git"
branch = "main"
```

In an empty folder holding only that file:

```bash
uv venv --seed .venv && .venv/bin/pip install aisland
.venv/bin/aisland workspace init . --from workspace.toml   # clones every project on its branch
.venv/bin/aisland setup
```

## Configure

Where a project comes from, whether this machine has it and which extras the projects install are settings of
`aisland workspace configure`. A setting is committed in `workspace.toml`, or kept for this machine only with
`--machine-only`:

```bash
aisland workspace configure source set models pypi          # install models from PyPI instead of its checkout
aisland workspace configure source set models local --machine-only
aisland workspace configure extras set dev                  # the extras every project installs
aisland workspace show                                      # what the projects declare to aisland
```

A project tells aisland what it brings — a selection to choose from, a service to run, the system tools it
needs — in its own `pyproject.toml`, under `[tool.aisland]`. It never imports aisland.

## Common tasks

| To … | Run | Page |
|---|---|---|
| create a workspace, or set up an existing repository | `aisland workspace init <folder>` | [workspace](https://github.com/Gearlux/aisland/blob/main/docs/workspace.md) |
| bring the workspace in line after a change | `aisland setup` | [workspace](https://github.com/Gearlux/aisland/blob/main/docs/workspace.md) |
| choose where a project comes from | `aisland workspace configure source set <project> local\|github\|pypi` | [sources](https://github.com/Gearlux/aisland/blob/main/docs/sources.md) |
| choose the extras the projects install | `aisland workspace configure extras set <names…>` | [selections](https://github.com/Gearlux/aisland/blob/main/docs/selections.md) |
| pull, push or tag every project | `git pull-all`, `git push-all`, `git tag-all <tag>` | [git](https://github.com/Gearlux/aisland/blob/main/docs/git.md) |
| run the local services | `aisland services start [NAME…]` | [services](https://github.com/Gearlux/aisland/blob/main/docs/services.md) |
| check the environment | `aisland doctor` | [doctor](https://github.com/Gearlux/aisland/blob/main/docs/doctor.md) |
| write a project's CI files | `aisland jenkins scaffold <project>` | [ci](https://github.com/Gearlux/aisland/blob/main/docs/ci.md) |
| see a project's test runs and slowest tests | `aisland tests report <project>` | [tests](https://github.com/Gearlux/aisland/blob/main/docs/tests.md) |
| carry a project to a machine without the workspace | `aisland wheels build <project>` | [wheels](https://github.com/Gearlux/aisland/blob/main/docs/wheels.md) |
| drop old committed wheels from a project's history | `aisland wheels prune <project> --apply` | [wheels](https://github.com/Gearlux/aisland/blob/main/docs/wheels.md#old-committed-wheels-aisland-wheels-prune-and-aisland-wheels-reset) |
| reclaim a project's disk, and undo it | `aisland clean <project>`, `aisland backups restore` | [clean](https://github.com/Gearlux/aisland/blob/main/docs/clean.md) |
| refresh shell completion | `aisland completion install` | [completion](https://github.com/Gearlux/aisland/blob/main/docs/completion.md) |
| give the editor window its own theme | `aisland workspace theme set <name>` | [editor](https://github.com/Gearlux/aisland/blob/main/docs/editor.md) |
| list the workspace's MCP servers for an AI client | `aisland mcp claude` | [mcp](https://github.com/Gearlux/aisland/blob/main/docs/mcp.md) |
| keep notebooks out of git without their outputs | `aisland notebooks clean` | [notebooks](https://github.com/Gearlux/aisland/blob/main/docs/notebooks.md) |
| reach a remote machine from your editor | `aisland tunnel start`, `aisland tunnel open <workspace>` | [tunnel](https://github.com/Gearlux/aisland/blob/main/docs/tunnel.md) |

Every command has `--help`.

## Reaching a remote machine (`aisland tunnel`)

A VS Code Remote Tunnel to a box published only through an HTTPS ingress — your editor or a plain terminal on it,
with nothing exposed and no sshd:

```bash
aisland tunnel start                  # on the remote machine
aisland tunnel open <workspace>       # your editor, from your machine
aisland tunnel connect <workspace>    # a terminal, from your machine
```

Signing in, a box that cannot download the VS Code CLI, remembered workspaces, and what `connect` needs on your
machine: [docs/tunnel.md](https://github.com/Gearlux/aisland/blob/main/docs/tunnel.md).

## Documentation

- [A workspace and its setup](https://github.com/Gearlux/aisland/blob/main/docs/workspace.md) — `init`, `setup`,
  `workspace.toml`, optional and untracked projects, system tools, a workspace of plain folders
- [Where each project comes from](https://github.com/Gearlux/aisland/blob/main/docs/sources.md) — `local`, `github`,
  `pypi`, and mirrors
- [Selections](https://github.com/Gearlux/aisland/blob/main/docs/selections.md) — the extras a project offers and the
  workspace chooses
- [Git across the workspace](https://github.com/Gearlux/aisland/blob/main/docs/git.md) — `pull-all`, `push-all` and
  the other aliases
- [Services](https://github.com/Gearlux/aisland/blob/main/docs/services.md) — the supervisor, Jenkins, MLflow, Docker
  and the others, and a service a project declares
- [Jenkins](https://github.com/Gearlux/aisland/blob/main/docs/jenkins.md) — starting it, its GitHub credential and
  the pipelines `aisland jenkins init` seeds
- [CI](https://github.com/Gearlux/aisland/blob/main/docs/ci.md) — the generated `Jenkinsfile`, `Jenkinsfile.local`
  and `ci.yml`, and the quality gates
- [The environment doctor](https://github.com/Gearlux/aisland/blob/main/docs/doctor.md)
- [Test-run statistics](https://github.com/Gearlux/aisland/blob/main/docs/tests.md)
- [Wheels for another machine](https://github.com/Gearlux/aisland/blob/main/docs/wheels.md)
- [Reclaiming disk space](https://github.com/Gearlux/aisland/blob/main/docs/clean.md) — `clean` and `backups`
- [Shell completion](https://github.com/Gearlux/aisland/blob/main/docs/completion.md)
- [The editor's files](https://github.com/Gearlux/aisland/blob/main/docs/editor.md) — the VS Code workspace file,
  Pyright's files, the window's theme and colours
- [MCP](https://github.com/Gearlux/aisland/blob/main/docs/mcp.md) — the client files and `aisland-mcp`
- [Agent commands and rules](https://github.com/Gearlux/aisland/blob/main/docs/harness.md)
- [Notebooks](https://github.com/Gearlux/aisland/blob/main/docs/notebooks.md)
- [Remote machines](https://github.com/Gearlux/aisland/blob/main/docs/tunnel.md)
- [Design decisions](https://github.com/Gearlux/aisland/blob/main/docs/architecture.md) — why each mechanism is the
  way it is

The examples in [`examples/`](https://github.com/Gearlux/aisland/tree/main/examples) run as written and check what
the pages show.

## Licence

MIT — see [LICENSE](https://github.com/Gearlux/aisland/blob/main/LICENSE). Changes are listed in the
[CHANGELOG](https://github.com/Gearlux/aisland/blob/main/CHANGELOG.md).
