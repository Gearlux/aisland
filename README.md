# Development Aisland

This directory contains the infrastructure and server management tools for the development environment. It is designed to be cross-platform and easily portable across development machines.

## Prerequisites
- **Python 3.12+**
- **Java OpenJDK 17 or higher** (Required for Jenkins)
- **process-compose** (service supervisor — installed automatically by `aisland setup` / `setup.sh`; see [Service Supervision](#service-supervision-process-compose))

## Default Ports

| Service       | Port | URL                        |
|---------------|------|----------------------------|
| Jenkins       | 9080 | http://localhost:9080      |
| Label Studio  | 9081 | http://localhost:9081      |
| MLflow        | 5005 | http://localhost:5005      |

All ports are configurable via `--port` flag.

## Setup & Usage

### Bootstrap

`bash aisland/setup.sh` (run from the workspace root) provisions the
`.venv`, installs aisland in editable mode, hands off to `aisland setup`
for project provisioning, installs shell completion for every workspace
CLI into a workspace-local `.project.bashrc.completion`
file (sourced by `project.bashrc`, gitignored — so the user's global
`~/.bashrc` stays clean and re-running setup never dirties the tree),
and finally runs `setup_git.sh` to configure submodule + git aliases.

### Shell completion

Enable it by starting a shell that sources `project.bashrc`:

```bash
source project.bashrc      # activates .venv AND exports .env AND loads completion
aisland <TAB>              # check docker fiftyone info jenkins labelstudio mcp mlflow ...
streamstudio exp<TAB>      # export export-workflow
recordstream <TAB>         # run
```

The workspace has **three kinds of CLI**, so `setup.sh` installs three
completion families into the one generated file:

| family | apps | mechanism |
| --- | --- | --- |
| liquifai apps | `recordstream`, `sonair`, `annotaide`, `raidar`, … (the `liquifai.apps` entry-point group) | `liquifai-install-completions` |
| click apps | `aisland` | click's own `_AISLAND_COMPLETE=bash_source` protocol |
| argparse apps | `streamstudio` | [argcomplete](https://kislyuk.github.io/argcomplete/) (`register-python-argcomplete`) |

Click refuses to emit a bash script for **bash < 4.4**, and macOS ships bash
3.2 as `/bin/bash` — the generated block guards on `BASH_VERSINFO`, so on a
stock-bash machine `aisland` completion is skipped rather than erroring. Install
a modern bash (`brew install bash`) to get it.

Completion blocks are keyed by marker comments and merged in place, so a
renamed or uninstalled CLI would otherwise leave a dead block behind forever.
`setup.sh` therefore **prunes any block whose program is no longer on `PATH`**
before regenerating — that is what clears e.g. a pre-rename `fluxstudio` block.
Re-run `bash aisland/setup.sh` after adding, renaming, or removing a CLI.

`aisland setup` also writes **`pyrightconfig.json`** files so Pyright /
Pylance resolve cross-project imports against the same project set
installed editable into the `.venv` — one at the workspace root
(`venvPath: "."`, plus each project dir on `extraPaths`) and one inside
**every project dir** (`venvPath: ".."` back to the shared `.venv`,
sibling projects on `extraPaths`) for when you open a single project as
its own editor window. All copies use relative paths only, so they are
portable across machines — and they are gitignored + regenerated on
every setup (setup appends the `/pyrightconfig.json` ignore entry to a
project's `.gitignore` if missing), so they always track the live
project list and never dirty a tree. Don't hand-edit them; add a
project's `pyproject.toml` and re-run `aisland setup`. Don't commit
editor `extraPaths` in `.vscode/settings.json` either — pyright ignores
those whenever a `pyrightconfig.json` is present, and the committed
copies this mechanism replaced had silently drifted.

The root **`mypy.ini`** is the opposite: committed, hand-maintained policy
(the per-module `[mypy-*]` sections). Setup only keeps its `mypy_path`
line complete — any discovered project missing from it is **appended**
(never removed: inactive submodules stay listed and mypy ignores path
entries that don't exist on a given machine, so the committed file stays
identical across machines).

#### Optional submodules & the generated `uv.toml`

Some projects are **optional** — they ship their own `uv.toml` (a private
package index) or are marked `active = false` in `.gitmodules` (e.g. the
corporate-only `vendorcli` / `signals-bridge`). `aisland setup` **prompts**
whether to activate each one and remembers the answer **per-machine** in
`.git/config` (`submodule.<name>.active`), never in the committed
`.gitmodules`. A decided module is honored silently on later runs; a *new*
optional module is prompted **even with `--non-interactive/-y`** (a machine
must consciously opt into a private project). Drive it non-interactively with
`--activate <name>` / `--deactivate <name>` (repeatable). Activating runs
`git submodule update --init`; deactivating deinits the working tree.

```bash
aisland setup                          # interactive: prompts for optional submodules
aisland setup -y                       # reuse saved decisions; still ask about new modules
aisland setup --activate vendorcli        # opt this machine into vendorcli (no prompt)
aisland setup --deactivate vendorcli      # opt out (deinits its working tree)
```

From the activated set, setup **generates the workspace-root `uv.toml`** =
a workspace base (`override-dependencies` — the unified `lightning` and the GUI
`opencv-python` are dropped workspace-wide via uv's never-true-marker idiom, and
headless opencv is pinned to the one range every consumer shares) merged with each
*activated* project's own `uv.toml` (union of `extra-index-url`, etc.). Like
`pyrightconfig.json`, it is **gitignored and regenerated** on every setup, so
a private index (the Company Artifactory in `vendorcli/uv.toml`) lands in the
root file **only on a machine that activates that project** — a non-corporate
machine that skips it never tries to contact the unreachable index.

### 1. Check Status
Retrieve service information for all infrastructure components:
```bash
aisland status
```

### Environment doctor

The shared `.venv` is built incrementally by every project, so one install can leave a
package pair inconsistent — and those failures surface far from their cause (a `dlopen`
error inside a *pytest collection* step, an `ImportError` three libraries deep in an
unrelated chain). `aisland doctor` checks for the skews that have actually recurred here and
prints the exact command that fixes each one:

```bash
aisland doctor
# Environment doctor — /path/to/.venv/bin/python3
#   ✓ opencv           cv2 4.13.0 imports; only opencv-python-headless=4.13.0.92 installed
#   ✓ huggingface_hub  huggingface_hub 1.12.2 satisfies transformers' import surface
#
# All 2 checks passed.
```

It exits non-zero when a check fails, so it works as a CI preflight step. Current checks:

| Check | Invariant | Symptom when violated |
|---|---|---|
| `opencv` | Only `opencv-python-headless` is installed (the root `uv.toml` override drops the GUI wheel — both distributions own `cv2/`) | `Library not loaded: @loader_path/libsrt.1.5.3.dylib` during pytest **collection** |
| `huggingface_hub` | `transformers`' import surface (`is_offline_mode`) resolves | `ImportError` while importing `pytorch_lightning` via `torchmetrics` |

Add a check by writing a `check_*` function in `aisland/doctor.py` and listing it in
`CHECKS`. A check must be cheap, read-only, and specific: it reports a known-recurring skew
with the command that fixes it, never a vague "something looks off".

## Service Supervision (process-compose)

Aisland delegates process supervision (spawn, liveness, readiness, crash
recovery, log capture, port discovery) to
[process-compose](https://github.com/F1bonacc1/process-compose) — a
single-binary, native-process orchestrator with docker-compose semantics. The
`aisland <service> start/stop/status` UX is unchanged; under the hood:

- **Desired state**: `aisland/data/process-compose.yaml` (generated,
  gitignored) records which services should run. `start` enables a service's
  entry and applies the document; `stop` disables it. The supervisor server
  runs detached on a unix socket in `aisland/data/` (TCP on Windows — port
  configurable via `AISLAND_SUPERVISOR_PORT`, default 9089), so multiple
  workspace checkouts never collide.
- **Readiness, not just liveness**: every service carries an HTTP readiness
  probe (Jenkins `/login`, MLflow `/health`, Label Studio `/health`, FiftyOne
  `/`). `aisland status` shows `ready: Ready` once the service actually
  answers, not merely when its process exists.
- **Crash recovery**: services restart automatically on failure (up to 3
  times); a manual `aisland <service> stop` is always respected.
- **Port pre-flight**: `start` refuses to launch when the service's port is
  already taken and names the owning PID — typically an instance started
  outside the supervisor.
- **Foreground semantics**: `aisland <service> start` without `--background`
  streams the service log; Ctrl+C stops the service (as before). The process
  itself always runs detached under the supervisor, so a dropped terminal
  never kills a service.
- **Logs**: per-service logs stay at `aisland/data/<service>.log`; the
  supervisor's own log is `aisland/data/process-compose.log`.

Supervisor-level commands:

```bash
aisland supervisor status   # supervisor state + process table
aisland supervisor attach   # process-compose TUI dashboard (q to detach)
aisland supervisor down     # stop ALL supervised services + the supervisor
```

**Installation**: macOS via Homebrew (`f1bonacc1/tap/process-compose`,
declared in aisland's `[tool.aisland.sys-dependencies]` and installed by
`aisland setup` / `setup.sh`); Linux via the userspace registry into
`aisland/bin/` (no sudo). Userspace downloads are verified against a pinned
per-architecture SHA-256 before extraction — a mismatch aborts the install
rather than running an unverified binary. If missing at `start` time, aisland
installs it on demand.

**Migrating from the PID-file implementation**: services started by a
pre-supervisor aisland are invisible to the supervisor. Stop them before
upgrading (or kill the process manually); the port pre-flight check will
otherwise point you at the offending PID on the next `start`.

### macOS: Full Disk Access for the supervisor (external data volumes)

macOS TCC attributes a daemon tree's file access to the tree's **root**
process — for every supervised service that root is the `process-compose`
server itself. When workspace data lives on an external volume
(`DATA_ROOT=/Volumes/...` in the workspace `.env`), the **supervisor binary**
must therefore hold a Full Disk Access grant. Grants held by processes further
down the tree (Jenkins' `java`, a notebook's `python`) are ignored, and the
denial surfaces as a misleading `PermissionError: [Errno 1] Operation not
permitted` deep inside the failing service — canonically `huggingface_hub`
cache reads during a Jenkins notebook build — while the *same command works
from an interactive shell* (whose TCC root is the already-granted terminal).
The kernel-log signature is
`kernel (Sandbox) System Policy: Python(NNN) deny(1) file-read-data /Volumes/...`.

1. Resolve the binary: `readlink -f "$(which process-compose)"`
2. System Settings → Privacy & Security → Full Disk Access → **+** → paste the
   resolved path (Cmd+Shift+G in the file picker) and enable it.
3. Restart the **whole supervisor** so the grant applies to a fresh root
   process: `aisland supervisor down && aisland start --background`.
   Restarting only a service (or only Jenkins) is NOT enough.

The grant is recorded against the **versioned Cellar path**
(`/opt/homebrew/Cellar/process-compose/<version>/...`), so `brew upgrade
process-compose` silently orphans it and the failures return — the formula is
kept `brew pin`ned so upgrades are deliberate; after upgrading, re-add the new
path and restart. `aisland` pre-flights this at supervisor start
(`supervisor._preflight_full_disk_access`) and prints the exact path to grant
when the grant is missing. The check is best-effort: reading the TCC database
itself requires Full Disk Access on the invoking shell, so an unreadable
database skips the check silently rather than crying wolf.

### 2. Manage Jenkins
For a complete guide on setting up project pipelines and credentials, see the [Jenkins Setup Workflow](JENKINS_WORKFLOW.md).

```bash
# Start Jenkins (foreground)
aisland jenkins start

# Start Jenkins (background)
aisland jenkins start --background

# Stop/Status
aisland jenkins stop
aisland jenkins status
```
Default: [http://localhost:9080](http://localhost:9080)

### 3. Manage MLflow
```bash
# Start MLflow (foreground)
aisland mlflow start

# Start MLflow (background)
aisland mlflow start --background

# Stop/Status
aisland mlflow stop
aisland mlflow status
```
Default: [http://localhost:5005](http://localhost:5005)

### 4. Manage Docker
The `act` tool and other containerized services require a Docker engine. Aisland uses **Colima** for a lightweight, CLI-first Docker experience on macOS.

```bash
# Start Docker engine
aisland docker start

# Stop Docker engine
aisland docker stop

# Check Docker status
aisland docker status
```

### 5. Manage Label Studio
```bash
# Start Label Studio (foreground)
aisland labelstudio start

# Start Label Studio (background)
aisland labelstudio start --background

# Stop/Status
aisland labelstudio stop
aisland labelstudio status
```
Default: [http://localhost:9081](http://localhost:9081)

**Credentials — the workspace `.env` is the single source of truth.** On every start, aisland resolves each credential as *explicit CLI flag → existing `.env` value → derived default*, and writes the result back to the workspace `.env`:

| `.env` key | Default | Purpose |
|---|---|---|
| `LABELSTUDIO_USERNAME` | git email | Login user (created on first start) |
| `LABELSTUDIO_PASSWORD` | workspace folder name | Login password |
| `LABELSTUDIO_TOKEN` | generated from workspace name | Legacy API token for programmatic clients (annotaide, signals) |
| `LABELSTUDIO_URL` | `http://localhost:<port>` | Base URL clients read |
| `LABELSTUDIO_LOCAL_FILES_ROOT` | `aisland/data/labelstudio/local-files` | Directory LS serves via `/data/local-files/?d=<rel>` |

To change a credential, edit `.env` and restart (`aisland labelstudio stop && aisland labelstudio start --background`), or pass `--username/--password/--user-token` once — the flags update `.env` for subsequent starts. Credentials never appear on the server's command line (`ps`) or as literals in the generated `data/process-compose.yaml`: the supervisor document stores `${LABELSTUDIO_*}` references that aisland expands from `.env` on every apply, and the server process receives them through its environment only.

## Jenkins Configuration
- **JENKINS_HOME:** All data, jobs, and configurations are stored in `aisland/data/jenkins`. This directory is ignored by Git to keep your local environment clean.
- **Pipelines:** Each project (e.g., `loggair/`) contains its own `Jenkinsfile`. When creating a new job in Jenkins, point it to the local repository path and select "Pipeline from SCM" with the script path set to `loggair/Jenkinsfile`.

### 6. Check Quality Gates
You can run automated quality checks across projects using the `check` command. This includes formatting (`isort`, `black`), linting (`flake8`), type checking (`mypy`), unit tests (`pytest`), and GitHub Actions validation (`act`).

```bash
# Run all checks for all projects
aisland check

# Run checks for a specific project
aisland check confluid

# Run a specific tool for all projects
aisland check pytest

# Run a specific tool for a specific project
aisland check pytest confluid
```

## GitHub Actions Local Testing

The `act` tool is installed to allow you to run and test GitHub Actions locally. 

```bash
# List all workflows and jobs in the current project
act --list

# Run all workflows for the current push event
act

# Run a specific job
act -j unit-tests
```
*Note: Running `act` requires Docker to be installed and running on your machine.*

## Project Sources (`aisland source`)

Each workspace project can be consumed either as a **local checkout** (developed here, installed
editable) or as a **published wheel** (not checked out at all). `aisland source` owns that choice
and drives every artifact that has to agree with it — the checkout, the shared `.venv`, the
`*.code-workspace` folder list, and every dependent project's generated CI files.

There are **three kinds**. `local` and `github` both take a `--ref` (branch or tag) and differ
only in whether a working tree survives; `pypi` takes a `--version` instead:

| source | directory | `.venv` | `*.code-workspace` | consumer CI install line |
|---|---|---|---|---|
| `local` | checked out, `git switch <ref>` | `uv pip install -e loggair` | listed | `git+…/loggair.git@main` |
| `github` | **deinit'd — empty** | `uv pip install git+…@<ref>` | **commented out** | `git+…/loggair.git@main` |
| `pypi` | **deinit'd — empty** | `uv pip install -U loggair` | **commented out** | *(none — `.[dev]` resolves it from PyPI)* |

Note the last column: **CI never sees your `--ref`.** It is a development pin, and these files are
tracked and shared — see [What CI does with all this](#what-ci-does-with-all-this).

Pick `local` when you edit the project here, `github` when you want a specific branch or tag but
no working tree, `pypi` when a released wheel will do. The names are deliberately far apart:
these commands are one word apart and two of them run `git submodule deinit`.

A project with **no entry** is *unmanaged*: nothing is switched, nothing is installed
differently, and CI keeps the historical `@main`. Setting the first source is the only thing that
changes behaviour.

```bash
aisland source list                                # what is configured, and what is on disk
aisland source set loggair local  --ref dev/main   # develop it here, on dev/main
aisland source set loggair github --ref dev/main   # pin to that branch, no checkout
aisland source set loggair pypi                    # consume the published wheel
aisland source set loggair pypi --machine-only     # this machine only; nothing committed
aisland source unset loggair                       # back to unmanaged
aisland source apply                               # re-apply the config (what `aisland setup` runs)
```

```text
$ aisland source list
  PROJECT                COMMITTED        OVERRIDE         EFFECTIVE        STATE
  confluid               pypi             -                pypi             empty
  loggair                github:dev/main  -                github:dev/main  empty
  recordstream           local:dev/main   -                local:dev/main   on dev/main
```

The `github` remote URL is read from `.gitmodules`, not assumed to be `github.com/Gearlux/…`, so
it also works for the submodules hosted elsewhere; an `scp`-style `git@host:org/repo` remote is
rewritten to `git+ssh://git@host/org/repo` because pip cannot parse the scp form.

### What CI does with all this

Generated CI files are **tracked and shared**, so they read as little of your configuration as
possible — exactly one bit, and never your `--ref`:

| file | runs where | how it installs an internal dep |
|---|---|---|
| `Jenkinsfile.local` | your machine, against the workspace | **identical for every config** — a `[ -d ]` guard: editable if the directory exists, else `git@main` |
| `Jenkinsfile`, `ci.yml` | a runner, no workspace | `git@main` — unless the dep is `pypi`-sourced, in which case it is omitted and `.[dev]` resolves it from PyPI |

Two consequences worth knowing:

- **`aisland source set --ref` regenerates nothing.** A ref is a development pin ("I'm working
  against `dev/main` today"); letting it decide what a runner builds would publish a personal
  choice to every consumer's CI *and* rewrite 17 projects' committed files on every switch. Only
  crossing the `pypi` boundary changes a CI file — because that is the one bit CI reads.
- **`Jenkinsfile.local` is config-independent entirely.** The `[ -d ]` guard asks at *build* time
  the same question the config answers — "does this machine have a checkout?" — so the file is a
  function of the dependency list alone. That also makes a `--machine-only` override work
  correctly instead of failing on an empty directory.

  ```groovy
  sh "if [ -d '${env.WORKSPACE_ROOT}/loggair' ]; then ${VENV_BIN}/uv pip install --no-deps -e '${env.WORKSPACE_ROOT}/loggair'; else ${VENV_BIN}/uv pip install --no-deps git+https://github.com/Gearlux/loggair.git@main; fi"
  ```

  The one cost: a `pypi`-sourced dep is built from `main` here rather than from the released
  wheel, so this file alone diverges from your `.venv` and from `ci.yml`. That is deliberate —
  the local pipeline's job is "does my workspace code build", and testing against `main` catches
  breakage *before* it is released.

"Available on PyPI" is decided once, when you run `aisland source set <p> pypi` — that command
verifies the name and its provenance against PyPI before touching anything. Scaffolding does not
re-probe the index: if it did, the content of committed files would depend on network
reachability, and an offline scaffold would flip every project to git URLs.

### The two configuration layers

Mirrors the `.gitmodules active` + `.git/config submodule.<name>.active` split used for optional
submodules:

| layer | where | committed? |
|---|---|---|
| default | `<workspace>/project-sources.toml` | **yes** — shared between machines |
| override | `git config --local aisland.source.<project>` | no — per machine |

**Which layer drives what is load-bearing.** Every downstream artifact except the venv and the
checkout is itself a committed file, so a machine-local choice must never reach one:

- **committed source only** → the `*.code-workspace` `folders` list, and the published/not bit
  that `Jenkinsfile` and `ci.yml` read. A `--machine-only` change touches neither.
- **effective source** (override, else committed) → submodule init/deinit + branch switch, and the
  editable/git/PyPI install into the shared `.venv`.
- **neither** → `Jenkinsfile.local`, which resolves the question at build time instead.

### `project-sources.toml`

Written by `aisland source set`; hand edits are honoured but the header asks you not to.

```toml
[loggair]
source = "github"         # "local" | "github" | "pypi"
distribution = "loggair"  # PyPI/dist name — recorded because a deinit'd dir has no pyproject.toml
ref = "dev/main"          # local/github only; omitted => "main"
# version = ">=0.1.0"     # pypi only; omitted => latest
```

### Notes

- Both detaching kinds are verified **before** anything is emptied, because otherwise the failure
  would land after the directory was already gone:
  - `pypi` — the name must exist on PyPI **and** its published metadata must point at
    `github.com/Gearlux/…`. PyPI names are global, so a bare existence check would let a workspace
    project whose name is taken by someone else install that stranger's package into the shared
    venv.
  - `github` — the `--ref` must resolve on the project's remote (`git ls-remote`).

  An unreachable index or remote warns and continues rather than blocking an offline machine.
- `git switch` and `git submodule deinit` are never forced: a submodule with uncommitted work
  refuses, is reported, and neither the venv nor the registry is changed — a failed apply rolls
  the configuration back so `project-sources.toml` never claims a state the tree does not have.
- `aisland source set` re-scaffolds every **dependent** project's CI files **only when the
  published/not bit flips** — i.e. crossing the `pypi` boundary. A `--ref` change, or a switch
  between `local` and `github`, regenerates nothing. When it does run it uses `--force`, so it
  also absorbs any unrelated template drift those files had accumulated; pass `--no-scaffold` to
  skip it.
- `aisland setup` applies the configuration on every run, so a fresh clone converges without any
  manual step.

## Submodule Management

This repository uses Git submodules to manage its constituent projects.

## Jupyter Notebook Management

To keep the repository size small and diffs clean, all Jupyter notebooks (`*.ipynb`) are automatically processed by a Git filter that clears their code cell outputs before staging.

### Marking as Documentation
If you have a notebook that serves as documentation and you **want** to preserve its outputs (e.g., visualizations, benchmark results), you must mark it as such in its metadata:

1. Open the notebook in VS Code or Jupyter.
2. Edit the notebook metadata (in VS Code: `Notebook Information` -> `Open Metadata`).
3. Add `"is_documentation": true` to the `metadata` object:
```json
{
  "metadata": {
    "is_documentation": true,
    ...
  },
  ...
}
```

### Manual Cleaning
You can also manually clean a notebook using the CLI:
```bash
cat my_notebook.ipynb | aisland notebooks clean > cleaned_notebook.ipynb
```

## Working with Submodules

To execute commands across all submodules (e.g., installing all projects in editable mode), use `git submodule foreach`:

```bash
# Install all projects in editable mode
git submodule foreach 'pip install -e .'

# Run tests in all submodules
git submodule foreach 'pytest'

# Check the status of all submodules
git submodule foreach 'git status'
```

### Initializing Submodules

If you've just cloned the repository or pulled new changes, ensure all submodules are up to date:

```bash
git submodule update --init --recursive
```

### Recommended Workspace Configuration

To ensure that your local submodules automatically stay in sync with the **parent repository's recorded hashes**, enable recursive submodule operations:

```bash
git config submodule.recurse true
```

*With this on, if a team member commits a new submodule hash to the parent repository, your next `git pull` will automatically download their updated submodule code.*

### Workspace Git Aliases

The aliases below (`pull-all`, `push-all`, `switch-all`, `merge-all`, `tag-all`, `status-all`, `safe`) are installed by `aisland/setup_git.sh` into the **local** config of the root repository *and* every active submodule. They assume the acme mono-repo layout and toolchain, so they are deliberately not written to the global config — that would leak workspace-specific behavior into unrelated repositories (the script also removes any global copies left by earlier versions of itself). The definitions live in `setup_git.sh`; to (re)install them — including after activating a new submodule — run:

```bash
bash aisland/setup_git.sh
```

### Pulling Unrecorded Submodule Changes (`pull-all`)

If `submodule.recurse` keeps things in sync, why do you need `pull-all`? 

Because a standard `git pull` only syncs submodules to the exact snapshots recorded in the **parent repository**. If a teammate pushes a brand new commit directly to the `recordstream` submodule's remote branch, but *nobody* has updated the parent repository to point to it yet, a standard `git pull` will completely ignore that new `recordstream` commit.

The `pull-all` alias is for **eagerly fetching unrecorded changes**. It reaches out directly to all submodule remote repositories, bypasses the parent's locked hashes, and forces your submodules to fast-forward to the absolute latest commits on their tracked branches.

To use it (e.g., to grab everything latest before starting a new feature):

```bash
git pull-all
```

### Pushing All Submodule Changes (`push-all`)

When you have committed changes across multiple submodules and the parent repository, you can use `push-all` to push the current branch of every project to its respective remote in one command.

To use it:

```bash
git push-all
```

### Switching All Submodules (`switch-all`)

When moving between development streams (e.g., switching from a feature branch back to `dev/main`), you often need to update the branch context for both the parent repository and all its constituent submodules simultaneously.

To use it:

```bash
git switch-all dev/main
```

### Merging All Submodules (`merge-all`)

When you need to merge a base branch (e.g., `main` or `dev/main`) into your current feature branch across all projects in the workspace, you can use `merge-all`. This ensures your entire dependency tree stays synchronized with upstream changes.

To use it (e.g., to merge `dev/main` into your current branch everywhere):

```bash
git merge-all dev/main
```

### Tagging All Submodules (`tag-all`)

When you want to mark a workspace-wide snapshot (a release, or a proof-of-concept milestone), you need the *same* tag on the root repository and every active submodule, then pushed to each remote. `tag-all` is the cross-repo analogue of a single `git tag`: it creates one **annotated** tag at the current `HEAD` of the parent and each active submodule, then pushes it. Tags are created everywhere **first** (cheap, local) before any push, so an unreachable remote (e.g. an off-VPN private host) leaves every local tag intact — you simply re-run to push the rest. The tag message defaults to the tag name if omitted.

To use it (creates the tag locally **and** pushes it to every remote in one command):

```bash
git tag-all 'v1.0.0' 'First stable release'
```

> Tags are non-forcing: if `<tag>` already exists, the command aborts before pushing. Delete it first (`git tag -d <tag>` / `git submodule foreach 'git tag -d <tag>'`) to re-tag.

### Checking Status of All Submodules (`status-all`)

To quickly see the status and current branch of the root repository and all its constituent submodules, you can use the `status-all` alias.

To use it:

```bash
git status-all
```

### Safely Pushing Changes (`safe`)

A custom Git alias exists to enforce the workspace mandates before pushing code. Run it from inside any project submodule (or the workspace root): it activates the shared workspace `.venv`, formats the code with `isort` and `black`, lints with `flake8`, type-checks with `mypy` (executed **from the workspace root** so the root `mypy.ini`'s `mypy_path` is honored), and runs `pytest`. If everything passes, it commits and pushes safely to the remote.

To use the shortcut:

```bash
git safe "feat(loggair): fixed the configuration parser bug"
```
