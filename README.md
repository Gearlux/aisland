# aisland

**One workspace of many Python projects, set up the same way on every machine.** aisland keeps a git repository
whose projects are submodules, installs every project editable into the workspace's one virtual environment, and
gives the workspace what its projects share: git aliases that pull and push them all, local services (Jenkins,
and the ones the projects bring, such as MLflow) under one supervisor, generated CI files, an environment check,
and the agent rules every AI coding session in the workspace reads.

## Install

```bash
uv tool install aisland    # or: pipx install aisland
aisland --version          # which aisland runs: aisland, version <its version>
```

Install aisland once, as a tool: that copy only creates workspaces. Each workspace then gets its own aisland in
its `.venv`, which `aisland setup`, the git aliases and CI use, so every workspace keeps the version it was set up
with. A project is any git repository with a `pyproject.toml`. `uv tool install` and `pipx install` give the tool an
environment of its own; a plain `pip install aisland` works only inside a virtual environment, since the system
Python of Homebrew or Debian refuses it (`externally-managed-environment`).

`aisland setup` also installs rule files for AI coding agents: an `AGENTS.md` in the root (`CLAUDE.md` and
`GEMINI.md` link to it), the rules and commands in `.agents/`, and a `.claude/commands` link in the root and in every
project. To have none of them, write `[harness] sets = []` into `workspace.toml`
([harness](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/harness.md)).

aisland runs on macOS and Linux with Python 3.12 or newer, git, [uv](https://docs.astral.sh/uv/) and bash (the git
aliases and the shell start-up file `project.bashrc` are bash), and on macOS [Homebrew](https://brew.sh), through which
it installs tools. `aisland setup` installs the system tools your projects declare. aisland's own are installed the
first time a command needs one, after asking you — on macOS through Homebrew: process-compose for the services, Colima
and Docker, SwiftBar, act, and the Java Jenkins accepts; on Linux only process-compose, into aisland's own `bin/`
folder, while Docker and Java are yours to install. Without a terminal the command stops and prints the install line
instead. Three extras add optional parts:

| Extra | Adds |
|---|---|
| `aisland[mcp]` | `aisland-mcp`, an MCP server that offers a chosen set of aisland's commands to an AI client |
| `aisland[connect]` | `aisland tunnel connect`, a terminal on a remote machine over its VS Code tunnel |
| `aisland[prune]` | `aisland wheels prune` and `aisland wheels reset`, which rewrite a project's history with git-filter-repo |

Installing aisland also registers a pytest plugin, `aisland_test_stats`. It records every test run of a project
in an aisland workspace (how long it took, and each test's outcome) in the workspace's `.aisland/data/test-stats/`;
a project outside a workspace records nothing. `aisland tests report` reads the record. To turn it off
([tests](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/tests.md#the-pytest-plugin) says what it writes):

```bash
aisland workspace test-stats off [PROJECT…] [--machine-only]   # for good
pytest -p no:aisland_test_stats                                # for one run
```

## A first workspace

The `aisland` tool creates the workspace; the workspace's own aisland, installed into its `.venv`, sets it up:

```bash
aisland workspace init ~/work/acme --add git@github.com:acme/models.git
cd ~/work/acme
uv venv --seed .venv && .venv/bin/pip install aisland   # the workspace's own aisland
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
    .agents/                  harness: aisland (6 commands, 1 script, rules) (`.claude/commands` links to its commands)
    AGENTS.md                 written (imports the rules; CLAUDE.md and GEMINI.md link to it)
```

`aisland setup` then installs the system tools the projects declare, installs every project editable into `.venv`, and writes the
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
format = 1
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
aisland workspace projects                                  # the project folders, dependencies first
```

A project tells aisland what it brings — a selection to choose from, a service to run, the system tools it
needs — in its own `pyproject.toml`, under `[tool.aisland]`. It never imports aisland.

## What aisland assumes

aisland comes with the habits of the workspace it grew in. Each one can be left alone, or turned off:

- **Two branches per project**: work on `dev/main`, release from `main`. The git aliases and
  `aisland workspace mirror sync` know both by name. Other branches work too, but they get no special handling.
- **Two test runs**: a fast `pytest` that stays under five minutes, and a slow `pytest -m slow`.
  `aisland tests report` proposes which tests to move between them, measured against that budget.
- **Python with machine-learning dependencies**: the generated root `uv.toml` installs `opencv-python-headless` in place
  of `opencv-python` everywhere. `aisland doctor` also checks for known problems with OpenCV, `huggingface_hub` and
  torch's extension builds; a check passes with a note where its package is not installed.

## Common tasks

| To … | Run | Page |
|---|---|---|
| create a workspace, or set up an existing repository | `aisland workspace init <folder>` | [workspace](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/workspace.md) |
| bring the workspace in line after a change | `aisland setup` | [workspace](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/workspace.md) |
| see which of the projects' variables this machine's `.env` lacks | `aisland workspace env-example --missing` | [workspace](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/workspace.md#the-machines-environment-envcomplete-envexample-and-env) |
| choose where a project comes from | `aisland workspace configure source set <project> local\|github\|pypi` | [sources](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/sources.md) |
| choose the extras the projects install | `aisland workspace configure extras set <names…>` | [selections](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/selections.md) |
| choose whether the root records a project's commits, and see what is set | `aisland workspace track set\|unset [<project>]`, `aisland workspace track status` | [workspace](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/workspace.md) |
| pull, push or tag every project | `git pull-all`, `git push-all`, `git tag-all <tag>` | [git](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/git.md) |
| run the local services | `aisland services start [NAME…]` | [services](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/services.md) |
| check the environment | `aisland doctor` | [doctor](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/doctor.md) |
| see where a command failed | `aisland --debug <command>` (every error is one `Error:` line, exit 1; this adds its traceback and aisland's debug log) | [doctor](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/doctor.md) |
| check every project, or one, before a push | `aisland check [<project>]` (isort, black, flake8, mypy, pytest, `act`; `--write` lets isort and black rewrite) | [ci](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/ci.md#check-quality-gates) |
| write a project's CI files | `aisland jenkins scaffold <project>` | [ci](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/ci.md) |
| see a project's test runs and slowest tests | `aisland tests report <project>` | [tests](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/tests.md) |
| carry a project to a machine without the workspace | `aisland wheels build <project>` | [wheels](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/wheels.md) |
| drop old committed wheels from a project's history | `aisland wheels prune <project> --apply` | [wheels](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/wheels.md#old-committed-wheels-aisland-wheels-prune-and-aisland-wheels-reset) |
| reclaim a project's disk, and undo it | `aisland clean <project>`, `aisland backups restore` | [clean](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/clean.md) |
| refresh shell completion | `aisland completion install` | [completion](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/completion.md) |
| give the editor window its own theme | `aisland workspace theme set <name>` | [editor](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/editor.md) |
| list the workspace's MCP servers for an AI client | `aisland mcp claude` | [mcp](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/mcp.md) |
| keep notebooks in git without their outputs | nothing: `aisland setup` registers the git filter `aisland notebooks clean` and adds `*.ipynb filter=ipynb-cleaner` to the root's `.gitattributes` (a project adds the line to its own) | [notebooks](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/notebooks.md) |
| reach a remote machine from your editor or a terminal, with no sshd | `aisland tunnel start` there, then `aisland tunnel open <workspace>` or `aisland tunnel connect <workspace>` here | [tunnel](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/tunnel.md) |

Every command has `--help`; [commands](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/commands.md) lists them all, with every option.

## Environment variables

None is needed; each changes one default. `env.complete` lists them with every variable the projects read.

| Variable | What it changes |
|---|---|
| `AISLAND_WORKSPACE` | aisland installed from a wheel: the workspace it works on (default: the one the current folder belongs to) |
| `AISLAND_TUNNEL_HOME` | where `aisland tunnel` keeps its state (default: `~/.aisland/tunnel`) |
| `WORKSPACE` | the name `aisland tunnel start` gives the tunnel when none is given (default: the machine's hostname) |
| `DATA_ROOT`, `MODEL_ROOT`, `RUN_DIR` | the folders the `cdd`, `cdm` and `cdr` helpers of `project.bashrc` jump to (`cdd models` goes to `$DATA_ROOT/models`); set them in the workspace's `.env` |
| `TORCH_EXTENSIONS_DIR` | the folder `aisland doctor` checks for a stale torch build lock (default: torch's own cache) |

## Uninstall

aisland keeps what it writes in the workspace, apart from the tunnel's state and the programs it installed with
your consent. To remove it from a workspace:

1. Stop what it runs: `aisland services stop`, and `aisland tunnel stop` on a machine that serves a tunnel.
2. Uninstall the package: `.venv/bin/pip uninstall aisland`, or delete `.venv`.
3. Delete what it generated in the root: `env.complete`, `env.example`, `project.bashrc`, `.project.bashrc.completion`,
   `pyrightconfig.json`, `uv.toml`, `.mcp.json`, `.gemini/settings.json`, `<name>.code-workspace`, the agent files
   (`.agents/`, the `.claude/commands` link, and `AGENTS.md` with its `CLAUDE.md` and `GEMINI.md` links unless you
   wrote your own), and from a wheel `.aisland/`, which holds the services' data and the tools aisland downloaded (from
   a checkout they are in the checkout's `bin/`, `data/` and `tools/`).
4. Decide about the starting files `workspace init` wrote, which are yours from then on: `.gitignore` (setup appends
   the agent files' paths to it), `.env`, `mypy.ini` (setup appends the projects to its `mypy_path`),
   `vscode-init-bash.sh` and `workspace.toml`. setup adds `*.ipynb filter=ipynb-cleaner` to the root's
   `.gitattributes`.
5. In each project: delete `pyrightconfig.json` and the `.claude/commands` link, and drop the lines `/pyrightconfig.json`,
   `/**/*.egg-info/` and `/.claude/commands` from its `.git/info/exclude`.
6. Remove the git settings from the local `.git/config` of the root and of each project: the workspace aliases
   (`git config --local --get-regexp '^alias\.'` lists them), `filter.ipynb-cleaner.*`, `submodule.recurse`,
   `pull.rebase` and the `aisland.*` keys. aisland adds nothing to the global git config; it removes a global alias
   named like one of the workspace's.
7. Outside the workspace: `~/.aisland/tunnel` (or `$AISLAND_TUNNEL_HOME`), SwiftBar's plugins folder setting on macOS,
   and the Homebrew formulas aisland installed after asking (`process-compose`, `docker`, `colima`, `act`, `swiftbar`,
   and the `openjdk@<N>` Jenkins runs on), which `brew uninstall` removes.

## Documentation

- [A workspace and its setup](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/workspace.md) — `init`, `setup`,
  `workspace.toml`, optional and untracked projects, system tools, a workspace of plain folders
- [Where each project comes from](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/sources.md) — `local`, `github`,
  `pypi`, and mirrors
- [Selections](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/selections.md) — the extras a project offers and the
  workspace chooses
- [Git across the workspace](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/git.md) — `pull-all`, `push-all` and
  the other aliases
- [Services](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/services.md) — the supervisor, Jenkins, Docker
  and the others, and a service a project declares
- [Jenkins](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/jenkins.md) — starting it, its GitHub credential and
  the pipelines `aisland jenkins init` seeds
- [CI](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/ci.md) — the generated `Jenkinsfile`, `Jenkinsfile.local`
  and `ci.yml`, and the quality gates
- [The environment doctor](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/doctor.md)
- [Test-run statistics](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/tests.md)
- [Wheels for another machine](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/wheels.md)
- [Reclaiming disk space](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/clean.md) — `clean` and `backups`
- [Shell completion](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/completion.md)
- [The editor's files](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/editor.md) — the VS Code workspace file,
  Pyright's files, the window's theme and colours
- [MCP](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/mcp.md) — the client files and `aisland-mcp`
- [Agent commands and rules](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/harness.md)
- [Worktrees of the whole workspace](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/worktrees.md)
- [Notebooks](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/notebooks.md)
- [Remote machines](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/tunnel.md)
- [Stability](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/stability.md) — what a release keeps, and what may
  change in any release
- [Architecture](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/architecture.md) — for contributors: what aisland
  is built of, and the decision records behind each mechanism
- [Contributing](https://github.com/Gearlux/aisland/blob/v0.1.2/CONTRIBUTING.md) — a checkout that runs the tests, the
  layers, where a change goes, the release

The examples in [`examples/`](https://github.com/Gearlux/aisland/tree/v0.1.2/examples) run as written and check what
the pages show.

## Licence

MIT — see [LICENSE](https://github.com/Gearlux/aisland/blob/v0.1.2/LICENSE). Changes are listed in the
[CHANGELOG](https://github.com/Gearlux/aisland/blob/v0.1.2/CHANGELOG.md).
