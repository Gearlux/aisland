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
| IQEngine      | 3000 | http://localhost:3000      |

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

After adding, renaming, or removing a CLI, refresh it with **`aisland completion
install`** — a one-second command, not a re-provision. See
[Shell Completion](#shell-completion-aisland-completion) for the three CLI
families it covers, the `--target-rc` option, and how stale blocks are pruned.

Click refuses to emit a bash script for **bash < 4.4**, and macOS ships bash
3.2 as `/bin/bash` — the generated block guards on `BASH_VERSINFO`, so on a
stock-bash machine `aisland` completion is skipped rather than erroring. Install
a modern bash (`brew install bash`) to get it.

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

### Reclaiming disk space (`aisland clean`)

A checkout accumulates gigabytes that git never sees: `.venv/`, `build/`, `.mypy_cache/`,
coverage reports, `lightning_logs/`, downloaded checkpoints, run directories. `git status`
stays clean the whole time. `aisland clean <project>` removes all of it — but **moves it into
a dated backup instead of deleting it**, because in this workspace an ignored path can be
load-bearing (a provisioned `bin/`, a service's `data/`, an `.env`) and `git clean -Xdf` gives
you no second chance.

```bash
aisland clean sonair --dry-run   # what would move, and which backups expire — changes nothing
aisland clean sonair             # move it (asks first)
aisland clean sonair -y          # ...without the prompt
aisland clean sonair --keep-days 1   # sweep harder when the disk is actually full
```

```text
Git-ignored content in sonair (/Users/gert/acme/sonair)
  .mypy_cache/                                            4 files   210.9 MiB
  lightning_logs/                                       105 files   252.5 MiB
  runs/                                                 484 files     4.9 MiB
  ...                                                  1892 files     1.4 GiB  in 48 entries

Deleted 2 backup(s) older than 7 days (1.9 GiB):
  .../aisland/data/backups/sonair/20260726-101500

Moved 48 entries (1.4 GiB) into .../aisland/data/backups/sonair/20260804-164945
Restore with:  cp -R .../aisland/data/backups/sonair/20260804-164945/files/. /Users/gert/acme/sonair/
```

**Backup layout.** Everything lives under `aisland/data/backups/` (git-ignored, and *not*
touched by `aisland reset`, which wipes each **service's** own directory):

```text
aisland/data/backups/<project>/<YYYYMMDD-HHMMSS>/
├── manifest.json   # project, timestamp, every entry with its size and file count
└── files/          # the project tree, mirrored — restore with `cp -R files/. <project>/`
```

### Inspecting and undoing a clean (`aisland backups`)

`aisland clean` acts on a project's working tree; `aisland backups` acts on the store it
writes to. Two verbs:

```bash
aisland backups list                 # everything in the store, newest first
aisland backups list sonair          # just one project's
aisland backups restore sonair       # move the most recent backup back into the project
aisland backups prune                # apply the retention window without cleaning anything
```

```text
  PROJECT                BACKUP              AGE       SIZE  ENTRIES  EXPIRES
  torpaido               20260804-171924      0d    2.1 MiB       18  in 7d
  sonair                 20260804-171409      0d    1.4 GiB       47  in 7d

  2 backup(s), 1.4 GiB under /Users/you/acme/aisland/data/backups
  Restore the newest:  aisland backups restore torpaido
```

Restore **moves**, like the clean did — the disk was full enough to warrant a clean, so a
second copy of the payload is the one thing that may not fit. The backup is consumed as it is
restored: its manifest is rewritten and a backup with nothing left in it is removed.

```bash
aisland backups restore sonair --stamp 20260728-101500  # an older one
aisland backups restore sonair --entry checkpoints      # one path only (repeatable)
aisland backups restore sonair --dry-run                # what would come back
aisland backups restore sonair --force                  # replace what the project re-created
```

A path the project has a **different** copy of is skipped, never clobbered — usually a cache
or a `.venv` that rebuilt itself since the clean, and almost always the one you want to keep.
Those entries stay in the backup and the command exits **1**, so a script notices a partial
restore; `--force` replaces them instead (it asks first, `-y` skips the prompt).

A path the project has an **identical** copy of is not a collision at all: it is reported as
`already`, the backup's redundant copy is dropped, and the exit code stays 0. The comparison
is a real one — sizes and file counts first (so a rebuilt `.venv` is rejected without reading
a byte), then byte-for-byte for whatever survives that. Timestamps are deliberately not part
of it, because the case this exists for is a path you already copied back by hand, and
`cp -R` restamps every file it writes.

The manifest is what makes restore finer-grained than `cp -R`: it records the paths that
actually moved, so a backed-up `data/jenkins` merges back into a `data/` the project has
since re-created, and `--entry` can pull back a single directory.

**Pruning on its own.** `aisland clean` already sweeps on every run, so the store keeps itself
in check as long as you keep cleaning. `aisland backups prune` is that same sweep as a
standalone command, for a workspace nobody is currently cleaning:

```bash
aisland backups prune --dry-run      # what has expired
aisland backups prune                # delete it (asks first; -y skips)
aisland backups prune --keep-days 1  # tighter window on a full disk
```

It covers every project — the retention window belongs to the store, not to one project — and
confirms before deleting, because standalone it is the only thing happening: inside a clean
the sweep is bounded by the fresh backup taken right after it, while here a mistyped
`--keep-days 0` would empty the store outright.

**Retention.** Every invocation sweeps backups older than `--keep-days` (default **7**) —
across *all* projects, not just the one being cleaned. The sweep runs before the move, so a
run on a full disk frees the expiring space first. It only ever deletes directories whose
name parses as `YYYYMMDD-HHMMSS`, i.e. ones this tool created; anything else under the backup
root is left alone.

| Guard | Why |
|---|---|
| One project per run — there is no `--all` | "Delete every ignored file in the workspace" would take out the shared `.venv`, every service's data directory and `.env` in one keystroke |
| The workspace **root** repo is refused | Its ignored set *is* that `.venv` / `.env` / generated `uv.toml` + `pyrightconfig.json` |
| A directory that is not its repository's root is refused | `git status` there reports the **enclosing** repo's ignored set |
| A project with no checkout (`github` / `pypi` source) is refused | Nothing to clean; see `aisland source list` |
| The backup store itself is never moved | Cleaning `aisland` matters here: its `.gitignore` ignores `data/` wholesale and the store lives at `data/backups`, so that one entry is split and everything *except* the store moves |

Cleaning a project undoes work that has to be redone: `aisland clean aisland` takes away the
provisioned `bin/` and every service's `data/` (re-run `aisland provision` / `aisland setup`),
and cleaning any project removes caches that the next `pytest` / `mypy` run rebuilds slowly.
That is the point of the backup — `aisland backups restore <project>` puts it back, until the
retention window closes.

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

**Sharing host directories with the VM (`--mount`).** A `docker run -v <host>:<target>` binds a
path *inside the Colima VM*, and Colima shares **only `$HOME`** with it by default. A bind mount
from anywhere else — an external volume such as `$DATA_ROOT=/Volumes/...` — silently mounts an
empty directory that Docker creates inside the VM: no error, the container just lists nothing.
Share the directory first, in Colima's own `DIR[:w]` syntax (bare = read-only):

```bash
aisland docker start --mount "$DATA_ROOT:r"          # repeatable; $HOME:w is always kept
aisland docker restart --mount "$DATA_ROOT:r"        # on a running engine — mounts apply at VM boot
aisland docker info                                  # "VM mounts : /Users/me:w, /Volumes/Store:r"
```

Two things the flag handles for you (both measured on colima 0.10.3): passing *any* `--mount` to
colima **replaces** its default `$HOME` share rather than adding to it, so aisland re-states
`$HOME:w` ahead of your mounts; and colima **persists** the flags into
`~/.colima/default/colima.yaml`, so a later bare `aisland docker start` keeps them. A running
engine that lacks a requested mount is *refused* with the restart command rather than restarted
for you — a VM restart stops every container on it. A symlink under `$HOME` that points outside
the shared set (`~/data -> /Volumes/Store`) is not enough on its own: the VM sees the symlink
itself and resolves it against *its* tree (`colima ssh -- readlink ~/data` → `/Volumes/Store/`),
so the real target must be shared — `--mount` resolves symlinks for exactly that reason.

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

### IQEngine SigMF viewer (`aisland iqengine`)

[IQEngine](https://github.com/IQEngine/IQEngine) is a web viewer for SigMF recordings — the
recording table with annotation counts, and a spectrogram with the annotation boxes drawn on it.
aisland runs it as **one container over a local directory**: the image is pinned by version *and*
digest (Docker checks it; `:latest` on ghcr is a 2023 build and `:pre` moves), the directory is
bind-mounted **read-only** at the path upstream documents, and there is no database — the index
is rebuilt from the directory at every start.

```bash
aisland docker start                                            # a Docker daemon (Colima)
aisland iqengine start --recordings "$DATA_ROOT/my-sigmf"        # remembered as IQENGINE_RECORDINGS in .env
aisland iqengine start                                          # next time: same directory
aisland iqengine status | info | stop                           # stop removes the container, keeps the files
aisland iqengine reset                                          # also drops the pinned image (re-pulled on start)
```

Then open http://localhost:3000 → **Local to Backend** → click a recording → **Spectrogram**;
annotations are drawn with their `core:label` and listed under the plot (click one to jump to
it). Edits made in the UI stay in the container's in-memory index — never written to your files,
gone at the next start.

Two things make an empty table, and `start` diagnoses both instead of leaving you to guess:

- **Every `.sigmf-meta` needs its `<same name>.sigmf-data` beside it.** IQEngine's local sync
  skips a sidecar without one (and ignores SigMF's `core:dataset`). An annotation-only export —
  sidecars whose IQ lives elsewhere — lists nothing; `start` prints
  `N of M sidecars have no .sigmf-data beside them`. Write the payload next to each sidecar (the
  `core:datatype` the sidecar already declares, e.g. `cf32_le`).
- **The Docker VM must share the directory.** Colima shares only `$HOME` by default, so a bind
  mount from an external volume comes up *empty* inside the container with no error anywhere.
  `start` compares what the host and the container see and, on a mismatch, tells you the fix:
  `aisland docker restart --mount "<dir>:r"` (see *Sharing host directories with the VM* above).

The image is `linux/amd64` only; on Apple silicon it runs under emulation and answers after
~20 s. Bump `IQENGINE_VERSION` and `IQENGINE_DIGEST` in `aisland/services/iqengine.py`
together — the digest of a tag is the `docker-content-digest` of its manifest.

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

## Workspace configuration (`config.toml`)

Every **shared** decision this workspace makes lives in one tracked file at the workspace root,
`config.toml` — which frameworks to install, which tracking backends, and where each project's
package comes from:

```toml
frameworks = ["torch", "lightning", "keras"]
trackers   = ["mlflow", "tensorboard", "aim"]

[sources.confluid]
source = "local"
distribution = "confluid"
ref = "main"

[sources.loggair]
source = "pypi"
distribution = "loggair"
```

You never edit it by hand: `aisland framework set` / `aisland tracker set` / `aisland source set`
each rewrite their own section and leave the others alone. Two commands exist for the file
itself:

```bash
aisland config show      # print it
aisland config migrate   # fold a pre-2026-08 sources/frameworks/trackers.toml into it
```

**Per-machine deviations never land here.** They go to `.git/config` (`aisland.frameworks`,
`aisland.trackers`, `aisland.source.<project>`), because this file is tracked and drives every
generated CI pipeline — one developer's local narrowing must not decide what a runner installs.

> The workspace-root **`uv.toml` is a different file and stays separate.** uv reads only a file
> of its own settings and rejects anything else in it, and that file is *generated* per machine
> from the activated project set (a private index reaches it only when its project is active),
> so it is gitignored rather than committed. See [Optional submodules & the generated
> `uv.toml`](#optional-submodules--the-generated-uvtoml).

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
| `Jenkinsfile.local` | your machine, against the workspace | **identical for every config** — a `[ -f <dep>/pyproject.toml ]` guard: editable if the checkout is there, else `git@main` |
| `Jenkinsfile`, `ci.yml` | a runner, no workspace | `git@main` — unless the dep is `pypi`-sourced, in which case it is omitted and `.[dev]` resolves it from PyPI |

Two consequences worth knowing:

- **`aisland source set --ref` regenerates nothing.** A ref is a development pin ("I'm working
  against `dev/main` today"); letting it decide what a runner builds would publish a personal
  choice to every consumer's CI *and* rewrite 17 projects' committed files on every switch. Only
  crossing the `pypi` boundary changes a CI file — because that is the one bit CI reads.
- **`Jenkinsfile.local` is config-independent entirely.** The guard asks at *build* time the same
  question the config answers — "does this machine have a checkout?" — so the file is a function
  of the dependency list alone. That also makes a `--machine-only` override work correctly instead
  of failing on an empty directory.

  ```groovy
  sh "if [ -f '${env.WORKSPACE_ROOT}/loggair/pyproject.toml' ]; then ${VENV_BIN}/uv pip install --no-deps -e '${env.WORKSPACE_ROOT}/loggair'; else ${VENV_BIN}/uv pip install --no-deps git+https://github.com/Gearlux/loggair.git@main; fi"
  ```

  It tests `pyproject.toml` rather than the directory because `git submodule deinit` leaves an
  **empty directory** behind — under a `[ -d ]` test every detached dep took the editable branch
  and the build died with *"does not appear to be a Python project"*.

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
| default | the `[sources.*]` tables of `<workspace>/config.toml` | **yes** — shared between machines |
| override | `git config --local aisland.source.<project>` | no — per machine |

**Which layer drives what is load-bearing.** Every downstream artifact except the venv and the
checkout is itself a committed file, so a machine-local choice must never reach one:

- **committed source only** → the `*.code-workspace` `folders` list, and the published/not bit
  that `Jenkinsfile` and `ci.yml` read. A `--machine-only` change touches neither.
- **effective source** (override, else committed) → submodule init/deinit + branch switch, and the
  editable/git/PyPI install into the shared `.venv`.
- **neither** → `Jenkinsfile.local`, which resolves the question at build time instead.

### The `[sources]` section of `config.toml`

Written by `aisland source set`; hand edits are honoured, but the next command that writes
re-renders the file (see [Workspace configuration](#workspace-configuration-configtoml)).

```toml
[sources.loggair]
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
  the configuration back so the registry never claims a state the tree does not have.
- `aisland source set` re-scaffolds every **dependent** project's CI files **only when the
  published/not bit flips** — i.e. crossing the `pypi` boundary. A `--ref` change, or a switch
  between `local` and `github`, regenerates nothing. When it does run it uses `--force`, so it
  also absorbs any unrelated template drift those files had accumulated; pass `--no-scaffold` to
  skip it.
- `aisland setup` applies the configuration on every run, so a fresh clone converges without any
  manual step.

## Optional extras: two selection axes

Workspace packages keep their core minimal and put optional capabilities behind *extras*.
Two independent things need deciding, and each has its own command over the same machinery:

| axis | command | decides | committed key |
| --- | --- | --- | --- |
| **frameworks** | `aisland framework` | what this machine **trains with** (`recordstream[torch]`, `sonair[lightning]`/`[keras]`) | `frameworks` in `config.toml` |
| **trackers** | `aisland tracker` | where results **go** (`matrainer[mlflow]`/`[aim]`/`[tensorboard]`/`[wandb]`) | `trackers` in `config.toml` |

They are **siblings, never merged**: picking a training engine must not install a tracking
client. Both work identically — same `list` / `set` / `unset` verbs, same committed +
per-machine layers, same rule that an override never reaches a tracked build file. Everything
below is written for `framework`; substitute `tracker` and it all holds.

## ML Frameworks (`aisland framework`)

Workspace packages keep their core **framework-free** and put each ML framework behind an
*extra*: `recordstream[torch]`, `sonair[lightning]` / `[keras]`, `matrainer[torch,lightning,…]`.
That makes "install only what I actually work with" expressible — this command decides *which*.

### The commands

There are exactly three, all under `aisland framework`:

| command | what it does |
| --- | --- |
| `aisland framework list` | show what is declared, what is selected, and how to change it |
| `aisland framework set <names...>` | choose frameworks **and reinstall** the projects that declare them |
| `aisland framework unset` | clear the selection (the next `aisland setup` prompts again) |

`--machine-only` and `--no-apply` are **flags on `set` / `unset`**, not commands — typing
`aisland framework --machine-only` is an error. Always spell the verb:

```bash
aisland framework list                                  # start here
aisland framework set torch lightning                   # shared choice; CI installs this too
aisland framework set torch lightning --machine-only    # THIS machine only; nothing committed
aisland framework set torch lightning --no-apply        # record it, install later
aisland framework unset --machine-only                  # drop the override, use the shared choice
aisland framework unset                                 # clear both layers
```

`aisland framework list` prints those commands back to you, filled in with the real framework
names of this workspace:

```
  FRAMEWORK      COMMITTED   OVERRIDE   EFFECTIVE   PROJECTS
  keras          yes         -          yes         matrainer, sonair
  lightning      yes         -          yes         matrainer, sonair
  tensorflow     no          -          no          matrainer
  torch          yes         -          yes         matrainer, recordstream

  '-' = no selection recorded in that layer (config.toml has no `frameworks` key, or no override).

  Change it (--machine-only / --no-apply are FLAGS on set/unset, not commands):
    aisland framework set keras lightning                 # shared: writes config.toml; CI installs it
    aisland framework set keras lightning --machine-only  # this machine only (.git/config)
    aisland framework unset --machine-only                # drop this machine's override
    aisland framework set keras lightning --no-apply      # record it without reinstalling
```

### Switching frameworks on your own machine

Use `--machine-only`. It writes `.git/config` and **reinstalls immediately** — no `aisland setup`
afterwards, and nothing tracked is modified, so your choice never reaches CI or a teammate:

```bash
$ aisland framework set keras --machine-only
==> This machine only: keras
==> Installing recordstream (/w/recordstream[dev])...
==> Installing matrainer (/w/matrainer[dev,keras])...
==> Installing sonair (/w/sonair[dev,keras])...

$ aisland framework unset --machine-only          # ...and back again
==> Cleared this machine's override; the committed selection applies.
==> Installing recordstream (/w/recordstream[dev,torch])...
==> Installing matrainer (/w/matrainer[dev,torch,lightning,keras])...
==> Installing sonair (/w/sonair[dev,lightning,keras])...
```

**Switching does not uninstall.** `uv` has no notion of "the packages that belonged to this
extra" — they are ordinary dependencies, possibly shared — so after switching to `keras` the
`pytorch-lightning` and `timm` wheels are still present and `LightningClassifier` still imports.
The selection controls what is *declared and installed*, not what is *removed*. For a genuinely
clean environment, recreate the `.venv` and run `aisland setup --framework keras`.

> **Some projects require a framework rather than offering a choice**, and they keep `torch` as a
> plain hard dependency on purpose: `signals` (its record builder enforces `torch.complex64`
> IQ), `deltaid` (a PyTorch-Lightning segmenter), `raidar` and `raidar-ultralytics` (torch
> detection trainers). Selecting `keras` therefore still installs torch on a full workspace —
> narrowing what a *multi-backend* project installs is what this command is for. Moving their
> deps behind an extra was considered and rejected: it would make `pip install raidar` yield a
> package whose `__init__` raises `ImportError`, which is dishonest packaging for a project that
> genuinely needs torch.

### Choosing at setup time

`aisland setup` takes `--framework` (repeatable), which **records** the selection so you only
pass it once:

```bash
aisland setup --framework lightning --framework keras
```

With nothing recorded, `aisland setup` prompts and saves your answer. `aisland setup -y`
(non-interactive) selects everything rather than hanging.

### A project declares which of its extras are frameworks

Selection needs to know which extras *are* frameworks — `dev`, `http`, `viz`, `notebook` and
`all` live in the same table as `torch` and `keras`, so a name match would install a docs extra
on a machine that asked for a training engine. Each project declares them itself:

```toml
# sonair/pyproject.toml
[tool.aisland]
frameworks = ["lightning", "keras"]
```

Declare only what is **implemented**: sonair keeps `tensorflow` / `jax` / `mlx` extras so its
CLI templates can print a real install command, but declaring them would install three
frameworks nothing can run yet. A project with **no** declaration is unaffected by any
selection — it installs exactly as it always did.

The names line up across projects on purpose (`keras` is `keras` everywhere), so one selection
drives every project with no per-project mapping table. Each project then installs what it
declares ∩ what is selected:

| selection | recordstream | matrainer | sonair |
| --- | --- | --- | --- |
| `torch lightning keras` | `[dev,torch]` | `[dev,torch,lightning,keras]` | `[dev,lightning,keras]` |
| `keras` | `[dev]` | `[dev,keras]` | `[dev,keras]` |
| *(none)* | `[dev]` | `[dev]` | `[dev]` |

### The two configuration layers

Same split as [Project Sources](#project-sources-aisland-source), for the same reason:

| layer | where | drives |
| --- | --- | --- |
| **committed** | the `frameworks` key of `config.toml` | the shared default **and** what every generated CI pipeline installs |
| **per-machine** | `git config --local aisland.frameworks` | this machine's `.venv` only |

A machine-local choice must never reach a tracked build file. If one developer narrows to torch
locally and that reached CI, every runner would silently stop testing the Keras backend — so
`aisland jenkins scaffold` reads the **committed** layer only. Verified 2026-07-30 that the full
set co-resolves (torch 2.13 + tensorflow 2.20 + keras 3.15 + pytorch-lightning 2.6.5 on one
numpy), so committing everything and letting each machine narrow is a workable default.

### Notes

- **Unset is not empty.** No `frameworks` key in `config.toml` and no override means *nobody has chosen*,
  and `aisland setup` prompts (recording the answer); a recorded empty selection means *no
  frameworks, I mean it*, and is honoured silently. `aisland setup -y` with nothing recorded
  selects everything rather than hanging or producing a workspace that cannot train.
- **Reinstalling does not uninstall.** Dropping `keras` from the selection leaves the keras wheel
  in the `.venv` — guessing which packages "belonged" to an extra is unsound, and a stale wheel
  costs disk, not correctness. Recreate the `.venv` for a truly clean set.
- **Order matters, and is handled.** `aisland framework set` reinstalls in dependency order,
  because a consumer's extra can name a sibling's (`sonair[keras]` requires `matrainer[keras]`);
  installing the consumer first makes uv warn `does not have an extra named 'keras'` and quietly
  install without it.
- **Both axes go into one install.** Changing the framework selection reinstalls with the
  selected *tracker* extras too, and vice versa — a target naming only the axis you changed
  would describe a package you never asked for.

## Tracking backends (`aisland tracker`)

The sibling axis. Experiment-tracking backends live behind extras
(`matrainer[mlflow]` / `[aim]` / `[tensorboard]` / `[wandb]`) and each imports its library
*lazily*, so code runs with none of them installed — the local-file backend always works. This
command decides which a machine actually installs.

```bash
aisland tracker list                          # start here
aisland tracker set mlflow tensorboard aim    # the free, self-hostable set
aisland tracker set mlflow --machine-only     # THIS machine only; nothing committed
aisland tracker unset --machine-only          # drop the override, use the shared choice
```

```
  TRACKER        COMMITTED   OVERRIDE   EFFECTIVE   PROJECTS
  aim            yes         -          yes         matrainer
  mlflow         yes         -          yes         matrainer
  tensorboard    yes         -          yes         matrainer
  wandb          yes         -          yes         matrainer
```

A project declares its backends the same way it declares frameworks — a sibling key, not an
addition:

```toml
[tool.aisland]
frameworks = ["torch", "lightning", "keras"]   # what it can train with
trackers   = ["mlflow", "tensorboard", "aim", "wandb"]  # what it can track to
```

Two things worth knowing:

- **A service will not install its own backend.** `aisland mlflow start` and `aisland aim start`
  fail with `aisland tracker set <name>` rather than pip-installing behind your back — an
  ad-hoc install is not recorded anywhere and disappears at the next `aisland setup`.
- **CI installs the committed selection**, for the same reason as frameworks, and it matters
  more here: tests needing a backend `importorskip` it, so a runner that stops installing
  `mlflow` produces a green build that quietly checks less than it used to.

### The Aim UI (`aisland aim`)

Aim needs no server infrastructure: runs live in an `.aim` repository directory and `aim up`
serves the comparison UI from it.

```bash
aisland tracker set aim              # install the backend (once)
aisland aim start --background       # serve http://localhost:43800
aisland aim info                     # URL + which repository it is serving
aisland aim stop
```

**The repository path is the whole contract.** `aisland aim start` publishes it to the
workspace `.env` as `AIM_REPO`, so a run config points its tracker at the same place:

```yaml
trackers:
  - !class:AimTracker { save_dir: $AIM_REPO }
```

Without that, a run writes to one repository and the UI serves another — the failure looks
like "my runs aren't showing up". `--repo <dir>` serves a different one and republishes the
variable to match.

If a freshly-finished run does not appear, it is Aim's index rather than a lost write: the UI
indexes when it starts, so `aisland aim restart` shows it. Nothing is missing from disk.

### The ClearML server (`aisland clearml`)

Seven containers (apiserver, webserver, fileserver, elasticsearch, mongo, redis, agent-services).
aisland does **not** reimplement that topology — it fetches upstream's compose file at a pinned
release, checks it against a pinned SHA-256, and layers a generated override on top.

```bash
aisland tracker set clearml     # install the backend (once)
aisland docker start            # the stack needs a Docker daemon
aisland clearml start           # ~2 GB of images on first run
aisland clearml status          # the containers
aisland clearml stop            # keeps the data
aisland clearml reset           # tears down AND deletes the state
```

**Why the override exists.** Upstream hardcodes `/opt/clearml/...` as the host side of every
bind mount, which would put service state outside the workspace and need root to create.
Compose merges a service's `volumes` **by container target**, so the generated override
replaces each host path with one under `aisland/data/clearml/state`. It is derived *from*
upstream's file, so bumping the pinned version cannot leave a stale path behind.

**Credentials are minted for you.** `aisland clearml start` enables a fixed user (generating a
password on first use and recording it), waits for the API, logs in and creates SDK credentials
— so the workspace `.env` ends up with everything a tracker needs and there is no browser
sign-up step:

```
CLEARML_WEB_HOST / CLEARML_API_HOST / CLEARML_FILES_HOST   where the server is
CLEARML_USERNAME / CLEARML_PASSWORD                        the web login
CLEARML_API_ACCESS_KEY / CLEARML_API_SECRET_KEY            what the SDK uses
```

Source `.env` and a `ClearMLTracker` connects with no further setup. Minting is best-effort: if
the API is not up yet it says so and the next `aisland clearml start` completes it — a stack
that is otherwise running is never failed over a convenience.

Two things worth knowing:

- **You may not need the server at all.** `ClearMLTracker(offline=True)` records a full session
  to local disk with no server and no credentials — worth trying first.
- **ClearML rejects very short task names.** A `run_name` of one or two characters fails at task
  creation with `Data validation error: name=String value is too short`. matrainer's generated
  names are well clear of it; a hand-written `run_name: r` is not.

On Apple silicon the images are `linux/amd64` and run under emulation; expect the web UI to take
~30 s on first boot while Elasticsearch comes up.

## Shell Completion (`aisland completion`)

Installs tab-completion for **every** CLI in the workspace in one command:

```bash
aisland completion install
```

It is idempotent — every block it writes is marker-delimited and rewritten in place — so run
it again whenever you add a project, rather than re-running the whole of `setup.sh` (which
calls it for you at provisioning time).

```
==> /path/to/workspace/.project.bashrc.completion (bash)
    liquifai apps: annotaide, purifai, raidar, recordstream, sonair, wf, ...
    other CLIs:    aisland, streamstudio
    pruned stale:  matrainer
```

**Three families, because the workspace has three kinds of CLI**, and a CLI that is none of
them silently gets no completion at all:

| Family | Who | Mechanism |
|---|---|---|
| liquifai apps | `recordstream`, `sonair`, `annotaide`, `raidar`, … | the `liquifai.apps` entry-point group |
| click apps | `aisland` | click's `_<PROG>_COMPLETE` protocol |
| argparse apps | `streamstudio` | `argcomplete` |

**Where it writes.** By default, the workspace-local `.project.bashrc.completion`, which
`project.bashrc` sources — so your global `~/.bashrc` stays clean and the tracked tree stays
free of diffs. Point it somewhere else with `--target-rc`:

```bash
aisland completion install --target-rc ~/.bashrc     # into your own shell rc
aisland completion install --target-rc project.bashrc
aisland completion install --shell zsh               # default: detected from $SHELL
```

Writing to an existing rc appends (and refreshes) only its own marked blocks; the rest of the
file is untouched. If nothing sources the target yet, the command prints the line to add.

**Stale blocks are pruned.** liquifai's installer merges by marker and leaves blocks it did
not write alone, so a renamed or removed CLI would keep a dead block forever. Any block whose
program no longer exists is dropped — unless *no* liquifai app was discovered at all, which
means discovery is broken rather than everything being uninstalled, and pruning then would
empty the file. Keep a stale block deliberately with `--no-prune`.

Programs are looked up in the active venv's `bin` before `PATH`, so the command behaves the
same whether or not you have sourced `project.bashrc`.

> **Verifying.** `complete -p aisland` in a new shell should print a completion function. If
> it does not, check that something sources the target file — the command tells you when
> nothing does.

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
