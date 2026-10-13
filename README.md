# Development Aisland

This directory contains the infrastructure and server management tools for the development environment. It is designed to be cross-platform and easily portable across development machines.

## Prerequisites
- **Python 3.13** — the version `setup.sh`'s `WORKSPACE_PYTHON` pins (exact, not a
  range: the ceiling is set by framework wheels, see the comment there). If no
  `python3.13` is on `PATH`, `setup.sh` provisions one itself via
  [uv](https://docs.astral.sh/uv/) (`uv python install 3.13`, no root, works the
  same on Linux, WSL and macOS), installing `uv` into `~/.local/bin` first when
  it is missing. A machine that already has the interpreter is left alone.
- **A Java the Jenkins WAR supports** — only the long-term releases it names (Jenkins 2.555.2:
  21 or 25). On macOS `aisland setup` installs it through Homebrew; see
  [Which Java runs Jenkins](#which-java-runs-jenkins).
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
unrelated chain). The Docker layer (Colima) has its own equivalents: a client config left
behind by an uninstalled tool, a VM whose trust store does not match the network the host is
on. Homebrew drifts too: its newest Java can be one Jenkins refuses. `aisland doctor` checks for
the skews that have actually recurred here and prints the
exact command that fixes each one:

```bash
aisland doctor
# Environment doctor — /path/to/.venv/bin/python3
#   ✓ opencv           cv2 4.13.0 imports; only opencv-python-headless=4.13.0.92 installed
#   ✓ huggingface_hub  huggingface_hub 1.12.2 satisfies transformers' import surface
#   ✓ mcp              mcp 2.0.0 exposes MCPServer — MCP servers can start
#   ✓ docker-creds     no credsStore configured — public pulls run anonymously
#   ✓ colima-tls       the VM verifies TLS to https://ghcr.io/v2/ — pulls can download
#   ✓ jenkins-java     Jenkins 2.555.2 runs on Java 25.0.3 (/opt/homebrew/opt/openjdk@25/bin/java)
#   ✓ torch-ext-lock   no torch extension build lock left behind under /Users/you/Library/Caches/torch_extensions
#
# All 7 checks passed.
```

It exits non-zero when a check fails, so it works as a CI preflight step. Current checks:

| Check | Invariant | Symptom when violated |
|---|---|---|
| `opencv` | Only `opencv-python-headless` is installed (the root `uv.toml` override drops the GUI wheel — both distributions own `cv2/`) | `Library not loaded: @loader_path/libsrt.1.5.3.dylib` during pytest **collection** |
| `huggingface_hub` | `transformers`' import surface (`is_offline_mode`) resolves | `ImportError` while importing `pytorch_lightning` via `torchmetrics` |
| `mcp` | The installed SDK exposes `mcp.server.mcpserver.MCPServer` | An MCP server entry point dies on import (`No module named 'mcp.server.fastmcp'`) while the CLI built on the same package keeps working |
| `docker-creds` | `~/.docker/config.json` names no credential helper that is missing from PATH (Docker Desktop's `credsStore: "desktop"` outlives its uninstall) | `exec: "docker-credential-desktop": executable file not found in $PATH` on **every** `docker pull`, even of public images |
| `colima-tls` | The Colima VM verifies TLS on the host's network (a TLS-intercepting proxy's CA must be in the VM's trust store, not just the macOS keychain) | `tls: failed to verify certificate: x509: certificate signed by unknown authority` during `docker pull`, while the same URL works on the host |
| `jenkins-java` | An installed JDK is one the downloaded Jenkins WAR accepts ([Which Java runs Jenkins](#which-java-runs-jenkins)); passes with a note before Jenkins is downloaded | `aisland jenkins start` stops with `Jenkins 2.555.2 runs on Java 21 or 25, and none is installed (found 26.0.2.1)` |
| `torch-ext-lock` | No torch JIT-extension build lock (`<torch extensions root>/<py>_<accelerator>/<ext>/lock`, the root `$TORCH_EXTENSIONS_DIR` or torch's per-user cache) is older than two minutes — a build takes seconds, and torch waits on an existing lock without a timeout, deleting it only when the compiling process lives through the build; a younger lock is a build in progress and passes | An import that compiles the extension sleeps forever with no error (about 8 s of CPU, then nothing) — a server's first request that loads such a graph, a catalogue helper that never answers; the fix line is the `rm` of the file |

Add a check by writing a `check_*` function in `aisland/doctor.py` and listing it in
`CHECKS`. A check must be cheap, read-only, and specific: it reports a known-recurring skew
with the command that fixes it, never a vague "something looks off".

### Test-run statistics (`aisland tests report`)

Every project has two test runs: plain `pytest` is the **fast run** (within 5 minutes — what
you run before and after a change), `pytest -m slow` the **slow run** (CI's), `pytest -m ''`
both. Which tests belong in which run is read from statistics, never guessed — so every run is
recorded:

- **Local runs.** aisland installs a pytest plugin (`aisland_test_stats`, loaded by every
  `pytest` in the workspace `.venv`). Each run of a workspace project appends to
  `aisland/data/test-stats/<project>/<date>.jsonl` (git-ignored; kept 90 days): one line for the
  run — which run it was (`fast`, `slow`, `all`, `other`), whether it was the whole run or a
  file / `-k` / `-x` part of it, its seconds, exit status and commit — and one line per test —
  its seconds, `passed` / `failed` / `error` / `skipped`, and whether it is marked `slow`.
- **CI.** The local Jenkins keeps every build's JUnit results; the report reads them beside the
  local runs.

```bash
aisland tests report aisland                  # the last 30 days
aisland tests report signals-wlan --days 90
aisland tests report raidar --json            # the same report as data
pytest -p no:aisland_test_stats               # one run left out of the record
```

```text
aisland - the last 30 days: 2 local runs, 3 Jenkins builds
Fast run: 1m 00s (median of 1 whole run; last 1m 00s) - budget 5m 00s, within

Slowest fast-run tests:
       1.8 s  0 of 4 failed  tests/test_doctor.py::TestReporting::test_every_registered_check_returns_a_result
       1.7 s  0 of 4 failed  tests/test_setup_git_aliases.py::test_rerun_is_idempotent
       ...

Tests that failed:
    2 of 4    last 2026-09-12  fast  tests/test_workspace_dependency_specs.py::test_the_real_workspace_has_no_unsatisfiable_internal_specs
```

A fast run over 5 minutes adds a list headed `Move to the slow run (never failed, slowest
first) to fit 5m 00s:` — the tests whose seconds cover the overrun; and a slow test that failed
is listed under `Slow tests that failed - move into the fast run:`. Moving one is an edit: add
or remove its `@pytest.mark.slow` (or, in a signal project, its file name in `FAST_SAMPLE`).
The fast run's time counts only complete runs of the whole fast run. Why it is built this way:
[`docs/architecture.md`](docs/architecture.md) → "Every pytest run of a workspace project is
recorded".

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

### Wheels for a project installed outside the workspace (`aisland wheels`)

A project can be installed on a machine without the workspace only when everything it needs can
be found there. The workspace projects on PyPI can be found; the others cannot. `aisland wheels`
builds a wheel of each workspace project the named project needs that is not on PyPI, writes the
wheels into that project's `wheels/` folder, and checks the projects it takes from PyPI against
this checkout:

```bash
aisland wheels signals-bridge
aisland wheels signals-bridge --include-published   # also the projects it needs that ARE on PyPI
aisland wheels signals-bridge --all                 # every workspace project with a checkout here
aisland wheels signals-bridge --json                # the same report as data
```

| Flag | Built | Installed by default |
|---|---|---|
| *(none)* | the projects it needs that are not on PyPI | all of them |
| `--include-published` | every project it needs that has a checkout here — the commit, not the release | all of them |
| `--all` | every workspace project that has a checkout here | the ones it needs |

A project the flag covers that has no checkout on this machine is not built: the report says so
(`Not built: loggair (no checkout here — taken from PyPI 0.2.0)`) and it comes from PyPI.

What it printed for signals-bridge on 2026-10-08 (53 s; exit 1):

```text
signals-bridge needs 4 workspace projects that are not on PyPI. Their wheels are in signals-bridge/wheels (1.3 MB):
  signals           0.1.0  dev/main 75e9dee
  signals-5gnr      0.1.0  dev/main abe8d10
  signals-analysis  0.1.0  dev/main d544812
  signals-lte       0.1.0  dev/main d63b739

It takes 4 workspace projects from PyPI, compared with this checkout:
  confluid      PyPI 0.4.0    behind: 6 files differ (confluid/configurator.py, confluid/decorators.py, confluid/engine.py, …)
  liquifai      PyPI 0.3.0    behind: 13 files differ (liquifai/bridge/__init__.py, liquifai/bridge/group.py, liquifai/bridge/policies.py, …)
  loggair       PyPI 0.2.0    not compared: no checkout here
  recordstream  PyPI 0.1.0a2  behind: 18 files differ (recordstream/__init__.py, recordstream/core/stream.py, recordstream/draws.py, …)

Installed signals-bridge in an empty venv (its wheels + PyPI) and imported every module of its 5 packages:
  signals.analysis.fivegnr  ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  signals.analysis.lte      ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  signals.fivegnr           ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  signals.generators        ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  signals.bridge.labelling  ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  signals.lte               ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  signals.occupancy         ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  signals.rfuav.ops         ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  signals.sources           ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)
  149 other modules imported

Release first: recordstream
Behind, nothing breaks: confluid, liquifai
```

- **Behind**: the wheel this checkout builds differs from the one on PyPI, file by file. The
  wheels were built against this checkout, so they may use what no release has yet.
- **Release first**: a module failed to import in an empty venv, and the module it missed
  belongs to that project. Release it, then run `aisland wheels` again. Every module is
  imported, not only the packages: `signals.bridge.labelling` above is reached by no package's
  own import. Exit status 1 means the install failed, a package failed to import, or a module
  missed a workspace project's module; 0 means none of that, even if some projects are behind.
- **Not imported, missing what no workspace project ships**: a module below a package that
  imported, missing something no workspace project ships — most often an optional extra's
  dependency, which the empty venv does not install. It is listed and does not change the exit
  status.

What `--include-published` printed for signals-bridge on 2026-10-09 (1 min 46 s; exit 0):

```text
signals-bridge needs 8 workspace projects; 7 are built from this checkout into signals-bridge/wheels (1.9 MB):
  confluid             0.4.0    dev/main 302e2ed  on PyPI 0.4.0
  liquifai             0.3.0    dev/main 0cb9dd7  on PyPI 0.3.0
  recordstream         0.1.0a2  dev/main 73b9772  on PyPI 0.1.0a2
  signals           0.1.0    dev/main 75e9dee
  signals-5gnr      0.1.0    dev/main ba9e6f0
  signals-analysis  0.1.0    dev/main 17991b2
  signals-lte       0.1.0    dev/main 1eb1815
Not built: loggair (no checkout here — taken from PyPI 0.2.0)

It takes 1 workspace project from PyPI, compared with this checkout:
  loggair  PyPI 0.2.0  not compared: no checkout here

Installed signals-bridge in an empty venv (its wheels + PyPI) and imported every module of its 8 packages:
  351 modules imported

Not imported, missing what no workspace project ships (an optional extra's?):
  recordstream.keras  ModuleNotFoundError: No module named 'keras'

PyPI is up to date for signals-bridge.
```

#### Installing on the other machine: `wheels/install.py` and `wheels/wheels.yaml`

Beside the wheels, `aisland wheels` writes `wheels.yaml` — each wheel, the commit it was built
from, whether the project needs it, the version PyPI has, and `install:` — and `install.py`, one
generic installer that reads it. `install:` is yours: set it to choose what installs; a rerun of
`aisland wheels` keeps every value you set (a wheel no longer built drops out).

```yaml
  - wheel: "confluid-0.4.0-py3-none-any.whl"
    project: "confluid"
    commit: "dev/main 302e2ed"
    needed: true
    on_pypi: "0.4.0"
    install: true
```

From the project's own folder (the folder's `README.md` says the same):

```bash
python wheels/install.py                 # the wheels marked install: true, then the project, editable
python wheels/install.py --all           # every wheel listed
python wheels/install.py --needed        # the wheels the project needs
python wheels/install.py --only recordstream,signals
python wheels/install.py --no-project    # the chosen wheels only
python wheels/install.py --reinstall     # take rebuilt wheels that kept their version
python wheels/install.py --dry-run       # print the command, run nothing
python wheels/install.py --installer uv  # uv pip instead of pip
python wheels/install.py -- --index-url https://…   # everything after -- goes to pip / uv
```

It installs into the interpreter that runs it, and installs PyYAML first when it is missing.
It names each chosen wheel by its FILE. That is what makes a wheel built from a checkout win over
PyPI's wheel of the same version: given only `--find-links wheels`, pip takes PyPI's
recordstream 0.1.0a2 and the module the checkout added is missing; named by its file, the
checkout's is installed (both measured 2026-10-09). The empty-venv check runs this same script.

How each wheel is made, and what is refused:

| Rule | Why |
|---|---|
| Each wheel is built from the project's last **commit**, never from its folder | A `build/lib` that setuptools leaves in a checkout puts files the project deleted long ago into the wheel |
| A project with uncommitted changes is refused, naming its files — with `--all`, also one the project does not need | Its wheel would not contain them |
| An unreadable `wheels.yaml` stops the run at its line, before anything is built | Rewriting it would drop the `install:` values it holds |
| A needed project that is not on PyPI and has no checkout here is refused | There is nothing to build it from; the message names `aisland source set <project> local` |
| "On PyPI" counts pre-releases | pip installs `recordstream 0.1.0a2` when no final release exists — unlike `aisland jenkins scaffold`, which asks about final releases only |
| A PyPI name owned by someone else is reported | pip takes the higher version of the two; the report says when that is theirs |
| Every environment marker is followed | The wheels serve another machine, whose platform is not this one's |
| The old `*.whl` in `wheels/` go, the folder's other files stay | |
| An unchanged project rebuilds byte for byte | Its wheel carries its commit's time, so git sees no change |
| The empty venv uses the project's own `uv.toml` when it has one | A private index it needs; the workspace's root `uv.toml` is never read |

A rebuild keeps each wheel's version (an unreleased project stays at `0.1.0`), so an
environment that already has the wheels takes new ones with `python wheels/install.py --reinstall`.
Nothing is committed; review `git status` in the project and commit `wheels/` yourself. Why it is built
this way: [`docs/architecture.md`](docs/architecture.md) → "A project installed outside the
workspace gets its unpublished dependencies as wheels".

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
- **No installs at start**: a service whose package is missing stops and names
  the command that installs it — `aisland tracker set <name>` for MLflow, Aim and
  ClearML, `uv pip install fiftyone` for FiftyOne. It never installs into the
  shared venv itself.
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

#### Which Java runs Jenkins

Jenkins runs only on the long-term Java releases its launcher names, and refuses any other
before it starts. Homebrew's `openjdk` formula follows the newest Java, and a shell profile
usually puts it first on `PATH`, so aisland never starts Jenkins with the bare `java`. It asks
the downloaded WAR file instead: every installed JDK is tried with `java -jar jenkins.war
--version`, newest first, and Jenkins starts on the first one the WAR accepts, by its full path:

```text
$ java -version                        # the shell's java: Homebrew openjdk, first on PATH
openjdk version "26.0.2.1" 2026-08-18
$ aisland jenkins start --background
Jenkins 2.555.2 runs on Java 25.0.3 (/opt/homebrew/opt/openjdk@25/bin/java)
Jenkins: http://localhost:9080
```

The JDKs it looks at, in this order: `$JAVA_HOME`, the `java` on `PATH`, each Homebrew
`openjdk*` keg, `/Library/Java/JavaVirtualMachines/*` (macOS) and `/usr/lib/jvm/*` (Linux).
`aisland jenkins info` prints the same line. When none is accepted, `aisland jenkins start`
stops before starting anything and names the fix:

```text
Error: Jenkins 2.555.2 runs on Java 21 or 25, and none is installed (found 26.0.2.1, 11.0.29).
  Java 26.0.2.1: /opt/homebrew/opt/openjdk/bin/java
  Java 11.0.29: /Library/Java/JavaVirtualMachines/amazon-corretto-11.jdk/Contents/Home/bin/java
Fix: brew install openjdk@25
```

On macOS, `aisland setup` (and `aisland provision`) installs that Java for you. It downloads the
WAR if it is missing, asks it, and installs the `openjdk@N` formula for the newest release
named. A machine with no Java that can ask (none at all, or only Java 8) first gets the newest
`openjdk@N` to ask with. Setup never installs or upgrades the bare `openjdk` formula, and
never changes the `java` on your `PATH`. On Linux, install the JDK the message names and set
`JAVA_HOME` to it. `aisland doctor` reports the same problem as the `jenkins-java` check.

**The nightly run.** The local orchestrator job (`<workspace>.local`, seeded by
`aisland jenkins init --local`) builds every project at **01:00 every night** — each project's
fast run and slow run — so the slow tests run and their results land in Jenkins' record, which
`aisland tests report <project>` reads. It needs Jenkins running and the machine awake at that
minute: Jenkins does not make up a night it missed.

**A manual run is the fast one.** **Build Now** on the orchestrator (or on one project's
`<project>.local` job) runs the fast tests only — the quality gates, the fast run, the examples
and notebooks. To run the slow tests too, use **Build with Parameters** and tick **SLOW_TESTS**:

| how the orchestrator is started | each project with slow tests runs |
|---|---|
| its 01:00 timer | the fast run and the slow run |
| Build Now | the fast run only |
| Build with Parameters, SLOW_TESTS ticked | the fast run and the slow run |

**The morning check.** Each weekday at 09:30 the Jenkins job `<workspace>.nightly-check`
(seeded by `aisland jenkins init --local`) runs `aisland tests nightly`. It reads every 01:00 run
since the previous report, writes `aisland/data/nightly/<date>.md` — each project's result, and
for a failed one its stage, its failing tests with their first error line, a quality gate's
offending lines — and, when a night failed, had no run, is still building or Jenkins did not
answer, shows a desktop notification and ends red. A manual run of the orchestrator is never
reported as a night.

```bash
aisland tests nightly                     # what the job runs
aisland tests nightly --json --no-notify  # the same report as data, no banner
```

```text
# Nightly runs — 2026-10-06

Jenkins http://localhost:9080/ · acme.local timer runs since 2026-10-05T11:50+02:00

**No 01:00 run** of acme.local started in that window.
```

At 09:45 a scheduled task of the Claude app, `acme-nightly-check`, reads that report and moves
tests between the fast and slow runs by the workspace rule; its edits stay uncommitted for
review. It runs while the app is open, or at its next launch.

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
- **Local orchestrator:** `aisland jenkins init --local` also seeds `<workspace>.local` (e.g. `acme.local`), which triggers every project's `<project>.local` job phase by phase in dependency order, the projects of one phase in parallel. The orchestrator itself holds no executor, so a phase's builds share only the built-in node's executors (two by default) — never one of them with the orchestrator.

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
and drives every artifact that has to agree with it — the checkout, the shared `.venv` and the
`*.code-workspace` folder list. It never touches a CI file: a runner installs what is **released**,
whatever you checked out (see [What CI does with all this](#what-ci-does-with-all-this)).

There are **three kinds**. `local` and `github` both take a `--ref` (branch or tag) and differ
only in whether a working tree survives; `pypi` takes a `--version` instead:

| source | directory | `.venv` | `*.code-workspace` |
|---|---|---|---|
| `local` | checked out, `git switch <ref>` | `uv pip install -e loggair[dev,…]` | listed |
| `github` | **deinit'd — empty** | `uv pip install git+…@<ref>` | **commented out** |
| `pypi` | **deinit'd — empty** | `uv pip install -U loggair` | **commented out** |

The `local` install target is the one `aisland setup` computes for that checkout — its `dev` extra
plus every framework/tracker extra this machine selected (`sonair[dev,lightning]`) — so switching a
project to `local` never drops a test dependency or a training backend from the venv.

**CI never sees your source or your `--ref`.** They describe this machine; the CI files are
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

Generated CI files are **tracked and shared**, so they read none of your source configuration.
A runner installs an internal dependency by asking one question — **is it released on PyPI?**

| file | runs where | how it installs an internal dep |
|---|---|---|
| `Jenkinsfile.local` | your machine, against the workspace | **it doesn't** — it tests the `.venv` that `aisland setup` provisioned, exactly as it is |
| `Jenkinsfile`, `ci.yml` | a runner, no workspace | **released** → no install line; `.[dev]` resolves the release from PyPI. **Not released** → `git+https://github.com/Gearlux/<dep>.git@main` |

"Released" means PyPI holds a **final** version (not only a pre-release such as `0.1.0a1`, not
only yanked files) whose metadata points at `github.com/Gearlux/…` — a stranger's package under
the same name does not count. `aisland jenkins scaffold` asks PyPI while generating; when PyPI
cannot be reached it stops and names the package instead of guessing:

```text
Error: cannot tell whether 'liquifai' is released: could not reach PyPI (…). The CI files were not
written — generating them needs PyPI, because a guess would flip every released dependency to a
GitHub install line. Retry when PyPI is reachable.
```

The accepted cost: a consumer that uses something a released dependency has only on `main` fails
its CI until that dependency is released — it builds against the release, never your checkout.

The dependency list itself is the closure of **exactly what CI installs**: the project's own base
dependencies plus the extras its CI steps install (`dev`, the best-effort `notebook`, and the
selected framework/tracker extras), then each reached dep's base dependencies transitively. An
extras-carrying spec like `matrainer[keras]` also pulls what that named extra needs — but a
transitive dep's *other* extras contribute nothing, because CI never requests them: each dep
installs bare (`--no-deps`), and `.[dev]` resolves only its base deps. So a sibling named only in
a dependency's `dev` extra never reaches your pipeline.

Two consequences worth knowing:

- **`aisland source set` / `unset` regenerate nothing.** A source and a ref are development
  choices ("I'm working on liquifai here, on `dev/main`"); letting them decide what a runner builds
  would publish a personal choice to every consumer's CI *and* rewrite the committed files of every
  dependent on each switch. Releasing a dependency is what changes a CI file — on the next
  `aisland jenkins scaffold`.
- **`Jenkinsfile.local` installs nothing and reads no configuration.** Every source decision was
  already applied to your `.venv` by `aisland setup`, so the local pipeline just runs the quality
  gates and tests of the checkout against that venv — no `aisland source` change of any kind
  rewrites it. Its `Initialize` stage only checks that the venv has *this* checkout installed:

  ```text
  + /Users/you/acme/.venv/bin/python3 -P - torpaido /Users/you/acme/torpaido
  torpaido -> /Users/you/acme/torpaido
  ```

  and stops the build when it does not, instead of testing another copy:

  ```text
  torpaido is not installed from /Users/you/acme/torpaido (found: not installed) - run `aisland setup`
  torpaido is not installed from /Users/you/acme/torpaido (found: file:///Users/you/other/torpaido) - run `aisland setup`
  ```

  The same applies to a dependency you just added to a project's `pyproject.toml`: until
  `aisland setup` has run, the local build fails with `ModuleNotFoundError` — run setup, the
  pipeline will not install it for you. See [architecture: the local pipeline tests the venv, it
  does not install](docs/architecture.md#the-local-pipeline-tests-the-venv-it-does-not-install).


### A `playwright` dev dependency installs its browser

Playwright's package alone cannot open a page — the browser is a separate download — so a
test that drives the page would `skip` wherever only pip ran, and CI would go green while
checking less than it used to. Listing `playwright` under the `dev` extra is therefore the
**whole declaration** (the same shape as a registered `slow` pytest marker: no aisland knob):

```toml
# <project>/pyproject.toml
[project.optional-dependencies]
dev = ["pytest>=7.0.0,<9.0.0", "playwright>=1.40.0,<2.0.0"]
```

With that one line, `aisland setup` runs `<venv>/bin/playwright install chromium` right after
the project's editable install, and `aisland jenkins scaffold` emits the same step after
`.[dev]` in every runner artifact that runs the tests (`Jenkinsfile.local` needs none — it
tests the venv setup already provisioned):

```groovy
// Jenkinsfile — one venv serves every stage, so once
sh "${VENV_BIN}/uv pip install -e .[dev]"
sh "${VENV_BIN}/playwright install chromium"
```

```yaml
# ci.yml — in each job that runs pytest (unit-tests, and slow-tests when the marker is registered)
uv pip install --system -e ".[dev]"
playwright install --with-deps chromium   # --with-deps: the OS libraries an Ubuntu runner lacks
```

Chromium only: it is the one browser a workspace test asks for. A project without the
dependency gets byte-identical output, and the browser cache is per user (`~/Library/Caches/
ms-playwright` on macOS), so a Jenkins agent downloads it once, not per build.


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
  editable/git/PyPI install into the shared `.venv` (the editable one with the same `[dev,…]` extras
  `aisland setup` installs).
- **neither** → `Jenkinsfile.local`, which installs nothing and tests the venv as setup left it.

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
- `aisland source set` / `unset` never regenerate a CI file — see
  [What CI does with all this](#what-ci-does-with-all-this).
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
| click apps | `aisland`, `hydraide` | click's `_<PROG>_COMPLETE` protocol |
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

## MCP client configuration (`aisland mcp`)

Every workspace project that ships an MCP server is registered with the MCP clients in use,
from ONE discovery: a `[project.scripts]` entry targeting `<pkg>.server:main` whose module
imports the SDK's server package (`mcp.server`). The commands are absolute venv paths, which
is why both files are gitignored and regenerated per machine — `aisland setup` writes them
after the editable installs, and each has a standalone command:

```bash
aisland mcp gemini    # .gemini/settings.json — the Gemini CLI
aisland mcp claude    # .mcp.json            — Claude Code's project-scoped servers
```

```json
{"mcpServers": {"annotaide":  {"command": "/…/.venv/bin/annotaide-mcp"},
                "navigaitor": {"command": "/…/.venv/bin/navigaitor"},
                "vendorcli":     {"command": "/…/.venv/bin/vendorcli-mcp"}}}
```

Claude Code reads `.mcp.json` when a session starts in the workspace and asks once whether
to use the project's servers (`enableAllProjectMcpServers` in its settings skips the prompt);
`claude mcp list` then shows them. A server whose annotaide, say, is not running still starts
— its tools answer in-band with the command that starts the service.

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

The aliases below (`pull-all`, `push-all`, `switch-all`, `merge-all`, `tag-all`, `status-all`, `safe`) are installed by `aisland/setup_git.sh` into the **local** config of the root repository *and* every active submodule. They assume the acme mono-repo layout and toolchain, so they are deliberately not written to the global config — that would leak workspace-specific behavior into unrelated repositories (the script also removes any global copies left by earlier versions of itself). The definitions live in `setup_git.sh` (`pull-all` calls [`git_pull_all.sh`](git_pull_all.sh) beside it); to (re)install them — including after activating a new submodule — run:

```bash
bash aisland/setup_git.sh
```

### Pulling Everything (`pull-all`)

`git pull-all` brings the whole workspace up to date with origin: every active submodule, then the root, on the root's branch. Type it in the root or inside any submodule — it always pulls the whole workspace.

```bash
git pull-all                # merge what came in; list what needs you
git pull-all --autostash    # also set aside uncommitted edits that are in the way, and put them back
```

A plain `git pull` (even with `submodule.recurse`) only moves each submodule to the commit the root *records*; a commit pushed to a submodule that nobody recorded in the root is never pulled. And when two machines both commit in a submodule and record it, the root's merge stops on the pointer even though the submodule merges cleanly:

```
CONFLICT (submodule): Merge conflict in signals
Automatic merge failed; fix conflicts and then commit the result.
```

`git pull-all` handles both. In order, it:

1. **fetches** the root and every submodule from `origin`, in parallel, before anything merges;
2. **merges** `origin/<branch>` into each submodule that is on the root's branch;
3. **merges** `origin/<branch>` into the root. A pointer conflict is settled to the submodule's checked-out commit **only when that commit contains both sides** (the commit git itself names as "a possible merge resolution"), and the merge is committed;
4. **records** the submodules that hold commits from origin the root does not record yet, in one commit — `chore: sync submodule pointers after pull-all (<submodules>)` — leaving anything else you staged staged. A pointer never moves backwards, and a submodule whose only unrecorded commits are your own local ones is left for you (`/commit-all` records it).

The same situation as above, with `git pull-all`:

```
fetching the root and 25 submodules…
signals             merged origin/dev/main (1 commit)
root                   pointer conflict signals → 6cb3d7f (holds both sides)
root                   merged origin/dev/main (1 commit)
```

What it cannot do is listed under `needs you:` and it exits 1; fix the item and run it again — it continues where it stopped (it also finishes a root merge a plain `git pull` left on a pointer conflict):

| listed as | what happened | what to do |
| --- | --- | --- |
| `CONFLICT in <files>` | both sides changed the same lines; that repository is left mid-merge (a submodule's conflict also holds back the root, whose pointers depend on it) | resolve, `git add`, `git commit`, run `git pull-all` again |
| `not merged: uncommitted edits to <files> would be overwritten` | the merge changes files you are editing; nothing was touched | commit them, or run `git pull-all --autostash` |
| `your uncommitted edits to <files> clash with the pulled changes` | `--autostash` put your edits back over lines the pull changed; the file has conflict markers, and a copy is in `git stash list` as `git pull-all autostash (applied with conflicts)` | fix the markers, `git reset -q -- <file>`, then `git stash drop` that copy |
| `detached HEAD — not pulled` / `on <branch>, the root is on <branch> — not pulled` | that submodule is checked out elsewhere on purpose | `git switch <branch>` in it to have it pulled |
| `not fetched: …` | its remote could not be reached (e.g. off VPN); the others still pull | reconnect and run again |
| `the root records <sha>, which <submodule>'s <branch> does not contain` | the root points at a commit outside that branch; the pointer is left as recorded | check the submodule's branch |

`--autostash` sets aside only the files the merge needs, in a stash named `git pull-all autostash`, merges, and puts them back. If the merge itself conflicts, your edits stay aside until you commit the merge; the next `git pull-all` puts them back.

The merges are explicit (`git fetch` + `git merge`, never `git pull`), so the result does not depend on a machine's `pull.rebase` setting. The logic lives in [`git_pull_all.sh`](git_pull_all.sh) (why it is shaped this way: [docs/architecture.md](docs/architecture.md), "`pull-all` settles a submodule pointer only to a commit holding both sides"). In Claude Code, `/pull-all` runs it and finishes the listed conflicts for you; `/push-all` runs `/pull-all`, the quality gates of each project with commits to push, and pushes.

### Pushing Everything (`push-all`)

`git push-all` pushes the current branch of every submodule, then the root's — the root only when every submodule push succeeded, so the root on origin never records a submodule commit origin does not have.

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
