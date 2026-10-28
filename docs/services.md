# Services

The services aisland runs for a workspace — Jenkins, Docker and, on macOS, the SwiftBar menu — with the supervisor
that keeps them running and the commands that start, stop and inspect each. A project of the workspace can bring more
(MLflow, Label Studio, IQEngine, Aim and ClearML below are examples); they run the same way, and start only when
named.

## Default Ports

| Service       | Port | URL                        |
|---------------|------|----------------------------|
| Jenkins       | 9080 | http://localhost:9080      |
| MLflow (a project's)        | 5005 | http://localhost:5005      |
| Label Studio (a project's)  | 9081 | http://localhost:9081      |
| IQEngine (a project's)      | 3000 | http://localhost:3000      |
| Aim (a project's)           | 43800 | http://localhost:43800    |
| ClearML web (a project's)   | 8080 | http://localhost:8080      |

Jenkins, MLflow, Label Studio, Aim and IQEngine take another port with `--port` (`aisland mlflow start --port 5006`).

## Check Status
Retrieve service information for all infrastructure components:
```bash
aisland services                  # the supervisor and every service
aisland services start -b         # start the default ones in the background
aisland services start mlflow aim # or only those named, a project's among them
```

A project can bring a service of its own, declared in its `pyproject.toml` — no aisland code:

```toml
[tool.aisland.service.notes]
command = ["notes-server", "--port", "8123"]   # what the supervisor runs
port = 8123                                    # checked free before it starts …
ready = "/health"                              # … and asked until it answers
needs = { tracker = "aim" }                    # optional: offered only while this choice is made
```

It gets its own `aisland notes start|stop|restart|status|info|reset` and joins `aisland services`, started when
named (`aisland services start notes`), never by default. A name aisland already uses — one of its services or a
command — is refused, naming the file and the line.

A project can also implement a service in its own code, importing nothing of aisland: an entry point in the
`aisland.plugins` group whose object has `kind = "service"` and a `start(ctx)` method.

```toml
[project.entry-points."aisland.plugins"]
viewer = "tools.viewer_service:Viewer"
```

```python
class Viewer:
    kind = "service"

    def start(self, ctx):
        port = ctx.port or 5151
        ctx.write_env({"VIEWER_URL": f"http://localhost:{port}"})
        ctx.run(["viewer-app", "--port", str(port)], port=port, ready="/")
```

`ctx` holds the service's `name`, the `workspace` and its `workspace_name`, its `data_dir`, the `port` asked for
(`None` when not given), `background` and the values of the class's own `options`, and offers
`run(command, env=, port=, ready=)`, `read_env()`, `write_env({...})`, `ports()`, `choose_command(selection, name)`
(the `aisland workspace configure` command that adds a choice and keeps the others), `echo(text)` and
`fail(message)` (one `Error:` line). A class may declare `options = {"repo": "the repository to serve"}`, offered as
`--repo` on `start` and `restart`, and `supervised = False` when its processes are not the supervisor's (Docker runs
them); its `info(ctx)` then stands alone. `stop`, `status`, `info` and `reset` are aisland's own unless the class has
them; its `info(ctx)` adds lines after aisland's. Once the project is installed it gets
`aisland viewer start|stop|restart|status|info|reset`, with `--port` on `start` and `restart`, and joins
`aisland services`, started when named.

## Service Supervision (process-compose)

Aisland delegates process supervision (spawn, liveness, readiness, crash
recovery, log capture, port discovery) to
[process-compose](https://github.com/F1bonacc1/process-compose) — a
single-binary, native-process orchestrator with docker-compose semantics. The
`aisland <service> start/stop/status` commands drive it:

- **Desired state**: `<workspace>/.aisland/data/process-compose.yaml` (generated,
  gitignored; `aisland/data/` when aisland runs from a checkout, as in every path below)
  records which services should run. `start` enables a service's
  entry and applies the document; `stop` disables it. The supervisor server
  runs detached on a unix socket in `.aisland/data/` (TCP on Windows — port
  configurable via `AISLAND_SUPERVISOR_PORT`, default 9089), so multiple
  workspace checkouts never collide.
- **Readiness, not just liveness**: every service carries an HTTP readiness
  probe (Jenkins `/login`, MLflow `/health`, Label Studio `/health`).
  `aisland services` shows `ready: Ready` once the service actually
  answers, not merely when its process exists.
- **Crash recovery**: services restart automatically on failure (up to 3
  times); a manual `aisland <service> stop` is always respected.
- **Port pre-flight**: `start` refuses to launch when the service's port is
  already taken and names the owning PID — typically an instance started
  outside the supervisor.
- **No tracker installs at start**: MLflow, Aim or ClearML with its package missing stops and names
  the command that adds it to the workspace's `tracker` choice (`aisland workspace configure tracker set …`,
  see [selections](selections.md)). It never installs a tracker into the shared venv itself.
- **Foreground semantics**: `aisland <service> start` without `--background`
  streams the service log; Ctrl+C stops the service. The process
  itself always runs detached under the supervisor, so a dropped terminal
  never kills a service.
- **Logs**: per-service logs stay at `.aisland/data/<service>.log`; the
  supervisor's own log is `.aisland/data/process-compose.log`.

Supervisor-level commands:

```bash
aisland supervisor status   # supervisor state + process table
aisland supervisor attach   # process-compose TUI dashboard (q to detach)
aisland supervisor down     # stop ALL supervised services + the supervisor
```

**Installation**: the first command that needs process-compose asks before installing it — macOS via Homebrew
(`f1bonacc1/tap/process-compose`), Linux into aisland's `bin/` folder (no sudo), verified against a pinned
per-architecture SHA-256 before extraction. Without a terminal, or on a "no", the command stops and installs nothing:

```text
Error: aisland's services need process-compose, which is not installed. Install it: brew install f1bonacc1/tap/process-compose
```

`aisland docker start` asks the same way for colima and docker, `aisland swiftbar start` for SwiftBar, and
`aisland workspace provision install` installs every one of them at once.

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
   process: `aisland supervisor down && aisland services start --background`.
   Restarting only a service (or only Jenkins) is NOT enough.

The grant is recorded against the **versioned Cellar path**
(`/opt/homebrew/Cellar/process-compose/<version>/...`), so `brew upgrade
process-compose` silently orphans it and the failures return — pin the formula
(`brew pin process-compose`) so upgrades are deliberate; after upgrading, re-add the new
path and restart. aisland checks this at supervisor start and prints the exact path to grant
when the grant is missing. The check is best-effort: reading the TCC database
itself requires Full Disk Access on the invoking shell, so an unreadable
database skips the check silently rather than crying wolf.

## Manage Jenkins
For a complete guide on setting up project pipelines and credentials, see the [Jenkins Setup Workflow](jenkins.md).

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

### Which Java runs Jenkins

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

On macOS, `aisland jenkins start` asks before it downloads the WAR and before it installs that Java (`aisland
workspace provision install` does both without asking). It asks the WAR, and installs the `openjdk@N` formula for the
newest release named. A machine with no Java that can ask (none at all, or only Java 8) first gets the newest
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
since the previous report, writes `.aisland/data/nightly/<date>.md` — each project's result, and
for a failed one its stage, its failing tests with their first error line, a quality gate's
offending lines, the line a stage past its time limit wrote — and, when a night failed, had no run, is still building or Jenkins did not
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

**A long stage has a time limit.** Each stage of `Jenkinsfile.local` that runs the project's own
code is held to a limit. Past it the stage fails, the build fails, and the orchestrator moves on to
the next project, so one hung test cannot hold the nightly run. Initialize and the quality gates run tools, not the project's code, and have
none. Waiting for an executor is not counted: the node is held before the first stage.

| stage | limit | longest healthy run (01:00 runs) |
|---|---|---|
| Unit Tests | 15 min | 6.0 min |
| Slow Tests | 60 min | 29.6 min |
| Verify Examples | 15 min | 0.5 min |
| Verify Notebooks | 15 min | 1.4 min |

A timed-out stage ends the build's console with the line the morning report quotes:

```text
Cancelling nested steps due to timeout
Sending interrupt signal to process
After 20s process did not stop
...
ERROR: Slow Tests timed out after 60 min - its limit in Jenkinsfile.local (STAGE_LIMITS in aisland/services/jenkins.py)
Finished: FAILURE
```

The limits are one table in aisland, the same for every project. Abort clicked before the
limit stays an abort. The process the stage was waiting on may outlive it: Jenkins gives it 20 s
and goes on without it. A pytest killed this way writes no report, and each pytest step removes
the previous build's report and coverage files before it runs, so Jenkins never records
yesterday's tests for a build that ran out of time. The push pipelines (`Jenkinsfile`, `ci.yml`)
carry no limit; a GitHub job is bounded at six hours by GitHub.

## The MLflow tracking server (`aisland mlflow`)

`aisland mlflow` exists while a project of the workspace brings MLflow as a service class; aisland itself ships none.
It serves the runs from a SQLite store and an artifact folder inside its data folder, `data/mlflow/`.

```bash
aisland workspace configure tracker set mlflow tensorboard   # install the backend (once; the whole list)
aisland mlflow start --background    # serve http://localhost:5005
aisland mlflow info                  # URL, backend store, artifacts folder
aisland mlflow stop
```

A run config points its tracker at it with `tracking_uri: http://localhost:5005`.

## Manage Docker
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

Two things the flag handles for you: passing *any* `--mount` to
colima **replaces** its default `$HOME` share rather than adding to it, so aisland re-states
`$HOME:w` ahead of your mounts; and colima **persists** the flags into
`~/.colima/default/colima.yaml`, so a later bare `aisland docker start` keeps them. A running
engine that lacks a requested mount is *refused* with the restart command rather than restarted
for you — a VM restart stops every container on it. A symlink under `$HOME` that points outside
the shared set (`~/data -> /Volumes/Store`) is not enough on its own: the VM sees the symlink
itself and resolves it against *its* tree (`colima ssh -- readlink ~/data` → `/Volumes/Store/`),
so the real target must be shared — `--mount` resolves symlinks for exactly that reason.

## Label Studio (`aisland labelstudio`)

`aisland labelstudio` exists while a project of the workspace brings Label Studio as a service class; aisland itself
ships none. Label Studio is that project's extra, installed only when named: without it, `start` prints the command
that adds it to the `extras` selection, keeping the extras already chosen.

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

**Credentials — the workspace `.env` is the single source of truth.** On every start, the service resolves each credential as *explicit CLI flag → existing `.env` value → derived default*, and writes the result back to the workspace `.env`:

| `.env` key | Default | Purpose |
|---|---|---|
| `LABELSTUDIO_USERNAME` | git email | Login user (created on first start) |
| `LABELSTUDIO_PASSWORD` | workspace folder name | Login password |
| `LABELSTUDIO_TOKEN` | generated from workspace name | Legacy API token for programmatic clients (a viewer, a pipeline) |
| `LABELSTUDIO_URL` | `http://localhost:<port>` | Base URL clients read |
| `LABELSTUDIO_LOCAL_FILES_ROOT` | `.aisland/data/labelstudio/local-files` | Directory LS serves via `/data/local-files/?d=<rel>` |

To change a credential, edit `.env` and restart (`aisland labelstudio stop && aisland labelstudio start --background`), or pass `--username/--password/--token` once — the flags update `.env` for subsequent starts. Credentials never appear on the server's command line (`ps`) or as literals in the generated `data/process-compose.yaml`: the supervisor document stores `${LABELSTUDIO_*}` references that aisland expands from `.env` on every apply, and the server process receives them through its environment only.

## IQEngine SigMF viewer (`aisland iqengine`)

`aisland iqengine` exists while a project of the workspace brings IQEngine as a service class; aisland itself ships
none.

[IQEngine](https://github.com/IQEngine/IQEngine) is a web viewer for SigMF recordings — the
recording table with annotation counts, and a spectrogram with the annotation boxes drawn on it.
It runs as **one container over a local directory**: the image is pinned by version *and*
digest (Docker checks it; `:latest` on ghcr is a 2023 build and `:pre` moves), the directory is
bind-mounted **read-only** at the path upstream documents, and there is no database — the index
is rebuilt from the directory at every start.

```bash
aisland docker start                                            # a Docker daemon (Colima)
aisland iqengine start --recordings "$DATA_ROOT/my-sigmf"        # remembered as IQENGINE_RECORDINGS in .env
aisland iqengine start                                          # next time: same directory
aisland iqengine status | info | stop                           # stop removes the container, keeps the files
aisland iqengine reset                                          # drops the container and the pinned image, never the files
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
~20 s.

## The Aim UI (`aisland aim`)

`aisland aim` exists while a project of the workspace brings Aim as a service class; aisland itself ships none.

Aim needs no server infrastructure: runs live in an `.aim` repository directory and `aim up`
serves the comparison UI from it.

```bash
aisland workspace configure tracker set mlflow tensorboard aim   # install the backend (once; the whole list)
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

## The ClearML server (`aisland clearml`)

`aisland clearml` exists while a project of the workspace brings ClearML as a service class; aisland itself ships none.

Seven containers (apiserver, webserver, fileserver, elasticsearch, mongo, redis, agent-services).
aisland does **not** reimplement that topology — it fetches upstream's compose file at a pinned
release, checks it against a pinned SHA-256, and layers a generated override on top.

```bash
aisland workspace configure tracker set mlflow clearml   # install the backend (once; the whole list)
aisland docker start            # the stack needs a Docker daemon
aisland clearml start           # ~2 GB of images on first run
aisland clearml status          # the containers
aisland clearml stop            # keeps the data
aisland clearml reset           # tears down AND deletes the state
```

**Why the override exists.** Upstream hardcodes `/opt/clearml/...` as the host side of every
bind mount, which would put service state outside the workspace and need root to create.
Compose merges a service's `volumes` **by container target**, so the generated override
replaces each host path with one under `.aisland/data/clearml/state`. It is derived *from*
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
  creation with `Data validation error: name=String value is too short`. Generated
  run names are well clear of it; a hand-written `run_name: r` is not.

On Apple silicon the images are `linux/amd64` and run under emulation; expect the web UI to take
~30 s on first boot while Elasticsearch comes up.
