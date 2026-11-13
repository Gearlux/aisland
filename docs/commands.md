<!-- Generated from aisland's commands; do not edit by hand. To write it again:
     AISLAND_WRITE_COMMANDS=1 .venv/bin/pytest -n 0 tests/test_docs.py -k command_reference -->
# Command reference

Every aisland command, with its usage, its help and its options, as its `--help` shows them. The services a
workspace's projects bring (`aisland <service> start|stop|…`) and the selections they declare (`aisland workspace
configure <selection>`) are that workspace's own: `aisland --help` and `aisland workspace configure` list them there.
The pages linked from the [README](../README.md) explain each command at length.

## `aisland`

```text
aisland [OPTIONS] COMMAND [ARGS]...
```

One workspace of many Python projects, set up the same way on every machine: a shared venv, local services, generated CI and git aliases.

Options:

- `--version` — Show the version and exit.
- `--debug` — Show an error's traceback, not only its Error line, and aisland's debug log on stderr.
- `--help` — Show this message and exit.

Commands: `backups`, `check`, `clean`, `completion`, `docker`, `doctor`, `jenkins`, `mcp`, `notebooks`, `services`, `setup`, `supervisor`, `swiftbar`, `tests`, `tunnel`, `wheels`, `workspace`.

### `aisland backups`

```text
aisland backups [OPTIONS] COMMAND [ARGS]...
```

Inspect and restore the backups `aisland clean` takes.

A separate group from `clean` on purpose: `aisland clean <project>` acts on a project's working tree, while these act on the STORE under `.aisland/data/backups` (`aisland/data/backups` from a checkout). Keeping the destructive verb one word long also keeps it from ever being typed by accident.

Options:

- `--help` — Show this message and exit.

Commands: `list`, `prune`, `restore`.

#### `aisland backups list`

```text
aisland backups list [OPTIONS] [PROJECT]
```

Show every backup in the store (newest first), optionally just PROJECT's.

Options:

- `--keep-days INTEGER RANGE` — Retention window the EXPIRES column is computed against. [default: 7; x\>=0]
- `--help` — Show this message and exit.

#### `aisland backups prune`

```text
aisland backups prune [OPTIONS]
```

Apply the retention window to the whole store, without cleaning anything.

`aisland clean` already sweeps on every run; this is the same sweep on its own, for a workspace nobody is currently cleaning. It covers every project — the retention window is a property of the store, not of one project.

```text
aisland backups prune --dry-run    # what has expired
aisland backups prune              # delete it (asks first)
aisland backups prune --keep-days 1
```

Options:

- `--keep-days INTEGER RANGE` — Delete backups older than this, for every project. [default: 7; x\>=0]
- `--dry-run` — Show what would be deleted; change nothing.
- `-y, --yes` — Skip the confirmation prompt.
- `--help` — Show this message and exit.

#### `aisland backups restore`

```text
aisland backups restore [OPTIONS] PROJECT
```

Move a backup's content back into PROJECT.

Moves rather than copies — the disk was full enough to run a clean, so a second copy is the one thing that may not fit. The backup is consumed as it is restored: its manifest is rewritten and an emptied backup is removed. A path the project already has is SKIPPED (exit 1) unless `--force` replaces it.

```text
aisland backups restore models                       # the most recent backup, whole
aisland backups restore models --stamp 20260804-164945
aisland backups restore models --entry checkpoints   # just this one, repeatable
aisland backups restore models --force               # overwrite what came back on its own
```

Options:

- `--stamp TEXT` — Backup to restore from [default: the most recent]
- `--entry TEXT` — Restore only this path (repeatable).
- `--force` — Replace a path the project already has, instead of skipping it.
- `--dry-run` — Show what would be restored; change nothing.
- `-y, --yes` — Skip the confirmation --force asks for.
- `--help` — Show this message and exit.

### `aisland check`

```text
aisland check [OPTIONS] [TOOL] [PROJECT]
```

Run the quality gates (isort, black, flake8, mypy, pytest, act): every one, or one TOOL, of every PROJECT or one.

Each of the two words is a gate or a project, in either order; one alone may be either.

isort and black only check, and show the change they would make; --write rewrites the files instead.

```text
aisland check                 every project, every tool
aisland check models          every tool for models
aisland check pytest          pytest for every project
aisland check pytest models   pytest for models
aisland check --write models  format models, then run the other gates
```

Options:

- `--write` — Let isort and black rewrite the files they find; without it they only check.
- `--help` — Show this message and exit.

### `aisland clean`

```text
aisland clean [OPTIONS] PROJECT
```

Move PROJECT's git-ignored content into a dated backup under .aisland/data/backups.

Reclaims the disk a checkout accumulates (`.venv/`, `build/`, caches, run dirs) without the irreversibility of `git clean -Xdf`: everything is renamed into the backup store and only really disappears when the retention sweep expires it. ONE project per invocation — there is no `--all`, and the workspace root is refused.

```text
aisland clean models --dry-run     # what would move, and which backups expire
aisland clean models               # move it, after confirming
aisland clean models --keep-days 1 # sweep harder on a full disk
```

Options:

- `--keep-days INTEGER RANGE` — Delete backups older than this; the sweep runs on EVERY invocation. [default: 7; x\>=0]
- `--dry-run` — Show what would be moved and swept; change nothing.
- `-y, --yes` — Skip the confirmation prompt.
- `--help` — Show this message and exit.

### `aisland completion`

```text
aisland completion [OPTIONS] COMMAND [ARGS]...
```

Install shell completion for every CLI in the workspace.

Options:

- `--help` — Show this message and exit.

Commands: `install`.

#### `aisland completion install`

```text
aisland completion install [OPTIONS]
```

Install completion for every workspace CLI — liquifai apps, click apps, argparse apps.

Idempotent: every block is marker-delimited and rewritten in place, so re-run this after installing a new project rather than re-running the whole of `aisland setup`.

Options:

- `--target-rc PATH` — Rc file to write into. Default: the workspace `.project.bashrc.completion`, which project.bashrc sources. Pass ~/.bashrc (or project.bashrc) to install elsewhere.
- `--shell [bash|zsh|fish]` — Default: the shell that reads the rc file (bash for the workspace file), else $SHELL's.
- `--no-prune` — Keep blocks for programs that are no longer installed.
- `--help` — Show this message and exit.

### `aisland docker`

```text
aisland docker [OPTIONS] COMMAND [ARGS]...
```

Manage the Docker engine: in Colima's VM on macOS, the system's own engine on Linux.

Options:

- `--help` — Show this message and exit.

Commands: `info`, `reset`, `restart`, `start`, `status`, `stop`.

#### `aisland docker info`

```text
aisland docker info [OPTIONS]
```

Show detailed Docker configuration (socket paths, colima status).

Options:

- `--help` — Show this message and exit.

#### `aisland docker reset`

```text
aisland docker reset [OPTIONS]
```

Stop Docker and wipe all its data: the Colima VM, with every container, image and volume in it.

Options:

- `-y, --yes` — Do not ask first.
- `--help` — Show this message and exit.

#### `aisland docker restart`

```text
aisland docker restart [OPTIONS]
```

Restart Docker (the way to change VM mounts on a running engine).

Options:

- `--mount DIR[:w]` — macOS: share a host directory with the Colima VM so containers can bind-mount it: DIR (read-only) or DIR:w (writable); repeatable. $HOME is always kept. Colima shares ONLY $HOME by default, so an external volume needs this — e.g. --mount "$DATA_ROOT:r". Applies at VM boot.
- `--help` — Show this message and exit.

#### `aisland docker start`

```text
aisland docker start [OPTIONS]
```

Start Docker (optionally sharing extra host directories with the VM).

Options:

- `--mount DIR[:w]` — macOS: share a host directory with the Colima VM so containers can bind-mount it: DIR (read-only) or DIR:w (writable); repeatable. $HOME is always kept. Colima shares ONLY $HOME by default, so an external volume needs this — e.g. --mount "$DATA_ROOT:r". Applies at VM boot.
- `--help` — Show this message and exit.

#### `aisland docker status`

```text
aisland docker status [OPTIONS]
```

Show Docker status.

Options:

- `--help` — Show this message and exit.

#### `aisland docker stop`

```text
aisland docker stop [OPTIONS]
```

Stop Docker.

Options:

- `--help` — Show this message and exit.

### `aisland doctor`

```text
aisland doctor [OPTIONS]
```

Check the shared .venv and the Docker/Colima setup for known recurring skew (exit 1 if any check fails).

Options:

- `--help` — Show this message and exit.

### `aisland jenkins`

```text
aisland jenkins [OPTIONS] COMMAND [ARGS]...
```

Manage local Jenkins CI/CD server.

Options:

- `--help` — Show this message and exit.

Commands: `info`, `init`, `remove`, `reset`, `restart`, `scaffold`, `start`, `status`, `stop`, `templates`.

#### `aisland jenkins info`

```text
aisland jenkins info [OPTIONS]
```

Show detailed Jenkins configuration (URL, JENKINS_HOME, admin password).

Options:

- `--help` — Show this message and exit.

#### `aisland jenkins init`

```text
aisland jenkins init [OPTIONS] [PROJECT]
```

Initialize Jenkins with Multi-branch pipelines for a project or all projects.

Options:

- `--local` — Initialize pipeline using local filesystem path and Jenkinsfile.local.
- `--help` — Show this message and exit.

#### `aisland jenkins remove`

```text
aisland jenkins remove [OPTIONS] [PROJECT]
```

Remove a specific project pipeline or all pipelines from Jenkins.

Options:

- `-y, --yes` — Confirm the action without prompting.
- `--help` — Show this message and exit.

#### `aisland jenkins reset`

```text
aisland jenkins reset [OPTIONS]
```

Stop Jenkins and wipe its data (or just a specific project's data).

Options:

- `--project TEXT` — Only reset data for a specific project pipeline.
- `-y, --yes` — Confirm the action without prompting.
- `--help` — Show this message and exit.

#### `aisland jenkins restart`

```text
aisland jenkins restart [OPTIONS]
```

Restart Jenkins.

Options:

- `--port INTEGER` — HTTP port for Jenkins (default: the port it last ran on, else 9080).
- `-b, --background` — Run in background.
- `--help` — Show this message and exit.

#### `aisland jenkins scaffold`

```text
aisland jenkins scaffold [OPTIONS] [PROJECT]
```

Generate standard Jenkinsfile, Jenkinsfile.local, and GitHub Actions for a project or all projects.

Options:

- `-f, --force` — Overwrite existing files.
- `--help` — Show this message and exit.

#### `aisland jenkins start`

```text
aisland jenkins start [OPTIONS]
```

Start Jenkins.

Options:

- `--port INTEGER` — HTTP port for Jenkins (default: the port it last ran on, else 9080).
- `-b, --background` — Run in background.
- `--help` — Show this message and exit.

#### `aisland jenkins status`

```text
aisland jenkins status [OPTIONS]
```

Show Jenkins status.

Options:

- `--help` — Show this message and exit.

#### `aisland jenkins stop`

```text
aisland jenkins stop [OPTIONS]
```

Stop Jenkins.

Options:

- `--help` — Show this message and exit.

#### `aisland jenkins templates`

```text
aisland jenkins templates [OPTIONS] COMMAND [ARGS]...
```

The template set `aisland jenkins scaffold` writes a project's CI files from.

aisland's own set by default; a workspace names another in workspace.toml — a folder of the workspace, or the name an installed package registers:

```text
[ci]
templates = "ci-templates"
[ci.variables]
agent_label = "linux-pod"
```

Options:

- `--help` — Show this message and exit.

Commands: `export`.

#### `aisland jenkins templates export`

```text
aisland jenkins templates export [OPTIONS] FOLDER
```

Copy aisland's own template set into FOLDER (new or empty), to start a set of your own from it.

```text
aisland jenkins templates export ci-templates
```

Options:

- `--help` — Show this message and exit.

### `aisland mcp`

```text
aisland mcp [OPTIONS] COMMAND [ARGS]...
```

Manage MCP client configuration for workspace MCP servers.

Options:

- `--help` — Show this message and exit.

Commands: `claude`, `gemini`.

#### `aisland mcp claude`

```text
aisland mcp claude [OPTIONS]
```

Generate .mcp.json (Claude Code's project servers) for the workspace MCP servers (gitignored; regenerated).

Options:

- `--help` — Show this message and exit.

#### `aisland mcp gemini`

```text
aisland mcp gemini [OPTIONS]
```

Generate .gemini/settings.json for the workspace MCP servers (gitignored; regenerated).

Options:

- `--help` — Show this message and exit.

### `aisland notebooks`

```text
aisland notebooks [OPTIONS] COMMAND [ARGS]...
```

Manage Jupyter notebooks in the workspace.

Options:

- `--help` — Show this message and exit.

Commands: `clean`.

#### `aisland notebooks clean`

```text
aisland notebooks clean [OPTIONS]
```

Clear outputs of code cells from standard input. Can be used as a git filter to avoid committing notebook outputs.

Options:

- `--help` — Show this message and exit.

### `aisland services`

```text
aisland services [OPTIONS] [COMMAND] [ARGS]...
```

Every aisland service at once, or the ones named. Alone: the supervisor and each service's state.

With no name, start/stop/restart/reset act on the services started by default (docker, jenkins, swiftbar); status and info cover every service, and a service a project brings starts when named. Each service also has its own group (`aisland jenkins start --port 9081`).

```text
aisland services start -b
aisland services start jenkins docker
aisland services stop
```

Options:

- `--help` — Show this message and exit.

Commands: `info`, `reset`, `restart`, `start`, `status`, `stop`.

#### `aisland services info`

```text
aisland services info [OPTIONS] [NAMES]...
```

URLs, credentials and paths of the services named (default: every service).

Options:

- `--help` — Show this message and exit.

#### `aisland services reset`

```text
aisland services reset [OPTIONS] [NAMES]...
```

Stop the services named and wipe their data (default: the services started by default). It asks once, naming each service and what its reset deletes — the container engine's deletes every container, image and volume.

Options:

- `-y, --yes` — Do not ask first.
- `--help` — Show this message and exit.

#### `aisland services restart`

```text
aisland services restart [OPTIONS] [NAMES]...
```

Restart the services named (default: the services started by default).

Options:

- `-b, --background` — Run in the background.
- `--help` — Show this message and exit.

#### `aisland services start`

```text
aisland services start [OPTIONS] [NAMES]...
```

Start the services named (default: the services started by default).

Options:

- `-b, --background` — Run in the background.
- `--help` — Show this message and exit.

#### `aisland services status`

```text
aisland services status [OPTIONS] [NAMES]...
```

Each named service's state (default: every service).

Options:

- `--json` — Print the states as one JSON object.
- `--help` — Show this message and exit.

#### `aisland services stop`

```text
aisland services stop [OPTIONS] [NAMES]...
```

Stop the services named (default: the services started by default, the container engine last).

Options:

- `--help` — Show this message and exit.

### `aisland setup`

```text
aisland setup [OPTIONS]
```

Full workspace setup: provision tools, ask about the optional projects, install the active ones.

Which projects this machine has: `aisland workspace configure active` (a project with an `active` value in workspace.toml is optional, and setup asks about it once).

What each project installs is a selection — aisland's own `extras`, and each one the projects declare (`aisland workspace show` lists them) — asked once and committed in workspace.toml; choose beforehand with `aisland workspace configure <selection> set`, or take every extra with --all.

```text
aisland setup --all
aisland workspace configure extras set dev --machine-only && aisland setup -y
```

Options:

- `-y, --non-interactive` — Reuse this machine's saved answers; still ask about an optional project never answered.
- `--all` — Install every extra every project defines, without asking, but those a project installs only when named ([tool.aisland] manual-extras). Computed from the projects' own [project.optional-dependencies]; does not need an extra named `all`.
- `--help` — Show this message and exit.

### `aisland supervisor`

```text
aisland supervisor [OPTIONS] COMMAND [ARGS]...
```

Manage the process-compose supervisor backing aisland services.

Options:

- `--help` — Show this message and exit.

Commands: `attach`, `down`, `status`.

#### `aisland supervisor attach`

```text
aisland supervisor attach [OPTIONS]
```

Attach the process-compose TUI to the running supervisor.

Options:

- `--help` — Show this message and exit.

#### `aisland supervisor down`

```text
aisland supervisor down [OPTIONS]
```

Stop every supervised service and the supervisor process.

Options:

- `-y, --yes` — Confirm the action without prompting.
- `--help` — Show this message and exit.

#### `aisland supervisor status`

```text
aisland supervisor status [OPTIONS]
```

Show the supervisor state and its process table.

Options:

- `--help` — Show this message and exit.

### `aisland swiftbar`

```text
aisland swiftbar [OPTIONS] COMMAND [ARGS]...
```

Manage SwiftBar menu bar plugins.

Options:

- `--help` — Show this message and exit.

Commands: `info`, `init`, `remove`, `reset`, `restart`, `start`, `stop`.

#### `aisland swiftbar info`

```text
aisland swiftbar info [OPTIONS]
```

Show detailed SwiftBar configuration (plugins dir, registered scripts).

Options:

- `--help` — Show this message and exit.

#### `aisland swiftbar init`

```text
aisland swiftbar init [OPTIONS]
```

Initialize SwiftBar scripts for monitoring workspace projects.

Options:

- `--help` — Show this message and exit.

#### `aisland swiftbar remove`

```text
aisland swiftbar remove [OPTIONS]
```

Unlink workspace scripts from SwiftBar.

Options:

- `--help` — Show this message and exit.

#### `aisland swiftbar reset`

```text
aisland swiftbar reset [OPTIONS]
```

Alias for remove.

Options:

- `--help` — Show this message and exit.

#### `aisland swiftbar restart`

```text
aisland swiftbar restart [OPTIONS]
```

Restart SwiftBar application.

Options:

- `--help` — Show this message and exit.

#### `aisland swiftbar start`

```text
aisland swiftbar start [OPTIONS]
```

Start SwiftBar application.

Options:

- `--help` — Show this message and exit.

#### `aisland swiftbar stop`

```text
aisland swiftbar stop [OPTIONS]
```

Stop SwiftBar application.

Options:

- `--help` — Show this message and exit.

### `aisland tests`

```text
aisland tests [OPTIONS] COMMAND [ARGS]...
```

Every pytest run in the workspace, recorded — read back per project.

Options:

- `--help` — Show this message and exit.

Commands: `nightly`, `report`.

#### `aisland tests nightly`

```text
aisland tests nightly [OPTIONS]
```

Report the local Jenkins' nightly runs since the previous report; exit 1 when one failed or none ran.

Reads every 01:00 run of the orchestrator (`<workspace>.local`) started since the previous report, writes `.aisland/data/nightly/<date>.md` (`aisland/data/` from a checkout) — each project's result, a failed one's stage, tests and error lines — and shows a desktop notification when a night failed, had no run, or is still building. The `<workspace>.nightly-check` Jenkins job runs it each weekday at 09:30.

```text
aisland tests nightly
aisland tests nightly --json --no-notify
```

Options:

- `--json` — Print the report as JSON (what an MCP tool would return).
- `--no-notify` — Write the report but show no desktop notification.
- `--help` — Show this message and exit.

#### `aisland tests report`

```text
aisland tests report [OPTIONS] PROJECT
```

PROJECT's fast run against its 5-minute budget, its slowest tests, the tests that failed, and what to move.

Reads the local runs (`.aisland/data/test-stats/PROJECT/`, `aisland/data/…` from a checkout) and the local Jenkins builds' JUnit results. A fast run over budget lists the never-failed tests to mark `slow`; a slow test that failed is listed for the fast run.

```text
aisland tests report models
aisland tests report models --days 90
aisland tests report models --json
```

Options:

- `--days INTEGER RANGE` — How far back to read the local runs and the Jenkins builds. [default: 30; x\>=1]
- `--json` — Print the report as JSON (what an MCP tool would return).
- `--help` — Show this message and exit.

### `aisland tunnel`

```text
aisland tunnel [OPTIONS] COMMAND [ARGS]...
```

Reach a remote machine from your local editor, over a VS Code Remote Tunnel.

A box published through an HTTPS ingress — a browser IDE under a path prefix — has no port for Remote-SSH to attach to. A tunnel needs none: the remote machine dials OUT to the relay and your editor attaches to that tunnel, so nothing has to be exposed through the ingress.

Two sides. On the machine you want to reach:

```text
    aisland tunnel start --provider microsoft
```

The first start signs in — it prints a code to enter in a browser — and the account is kept, so later starts need no interaction. The tunnel takes the machine's own workspace name ($WORKSPACE), else its hostname, unless --name says otherwise.

Everything the tunnel keeps lives in ~/.aisland/tunnel on that machine, wherever you run the command from. On a box whose $HOME does not survive a restart, put it on the mount that does — once, in the shell profile — and the sign-in lasts:

```text
    export AISLAND_TUNNEL_HOME=/mnt/workspace/.aisland/tunnel
```

On your own machine, paste the address the browser IDE shows — once:

```text
    aisland tunnel open 'https://HOST/WS-prod/?folder=/mnt/workspace'
    aisland tunnel open 'https://HOST/WS-prod/?workspace=/mnt/workspace/repo/x.code-workspace'
```

Whichever of the two query parameters the address carries says what to open: a folder, or a .code-workspace file. An address ending in -prod (or -dev, -staging, -test, -qa) names the INGRESS, though, not the tunnel — the box inside usually calls itself WS. When the two differ and neither is remembered yet, `open` asks which one it is; --name says it outright.

After that the workspace is remembered, so it is just:

```text
    aisland tunnel open WS<TAB>
```

A terminal instead of the editor — the same tunnel, nothing added on the remote machine:

```text
    aisland tunnel connect WS<TAB>
```

`list` shows what is remembered, `clear` forgets one or all of them, and `status` says whether a tunnel runs on this machine, under which name and which account. None of this is part of `aisland services start` / `stop`: a tunnel into your own laptop is nobody's intent, so it runs only where you ask for it.

Options:

- `--help` — Show this message and exit.

Commands: `clear`, `connect`, `list`, `login`, `logs`, `open`, `start`, `status`, `stop`.

#### `aisland tunnel clear`

```text
aisland tunnel clear [OPTIONS] [NAMES_OR_URLS]...
```

Forget a remembered workspace (by name or address), or all of them with --all.

NAMES_OR_URLS take either spelling — the name TAB offers, or the address you pasted. This only forgets what `open` completes on; it never touches a running tunnel (`stop` does that).

Options:

- `--all` — Forget every remembered workspace.
- `-y, --yes` — Do not ask before forgetting everything.
- `--help` — Show this message and exit.

#### `aisland tunnel connect`

```text
aisland tunnel connect [OPTIONS] [TARGET]
```

A terminal on a remote machine, through the VS Code tunnel `aisland tunnel start` runs there.

Run it on YOUR machine. With no name it lists the VS Code tunnels of your account; with one it opens your shell over there, in a real terminal (editors, Ctrl-C, resizing and your ~/.bashrc's colours work), and exits with the shell's status:

```text
    aisland tunnel connect
    aisland tunnel connect WS<TAB>
    aisland tunnel connect WS --cwd /mnt/workspace
```

Nothing is added on the remote machine: no sshd, no second tunnel. This machine needs Microsoft's `devtunnel`, signed in with the account the remote tunnel runs under (`devtunnel user login`, or `-g` for GitHub), and the [connect] extra (pip install 'aisland[connect]').

Options:

- `--cwd TEXT` — Folder on the remote machine to start the shell in. Default: your home folder there.
- `--json` — With no tunnel named: the list, machine-readable.
- `--help` — Show this message and exit.

#### `aisland tunnel list`

```text
aisland tunnel list [OPTIONS]
```

The workspaces you have opened, newest first — what `open` completes on TAB.

Each row is the tunnel name, what was opened there (a folder or a .code-workspace file), how long ago, and the address it was learned from. The twenty most recent are kept; `clear` forgets them.

Options:

- `--json` — Machine-readable output.
- `--help` — Show this message and exit.

#### `aisland tunnel login`

```text
aisland tunnel login [OPTIONS]
```

Sign the VS Code CLI in, so the next start needs no interaction.

`start` already does this when no account is stored; run it on its own to sign in ahead of time, or to switch account with --provider. The choice is remembered on this machine, which is why it is not in the workspace's committed configuration: the account is yours, not the workspace's.

Options:

- `--provider [github|microsoft]` — Account to sign in with; remembered on this machine.
- `--help` — Show this message and exit.

#### `aisland tunnel logs`

```text
aisland tunnel logs [OPTIONS]
```

The tunnel process's own log — where the sign-in prompt and the tunnel address appear.

This is what to read when `start` said nothing useful: the CLI writes its progress, its device-code prompt and any relay error here.

Options:

- `-n, --lines INTEGER` — How many trailing lines to show. [default: 80]
- `--help` — Show this message and exit.

#### `aisland tunnel open`

```text
aisland tunnel open [OPTIONS] [NAME_OR_URL]
```

Attach YOUR editor to a tunnel — by address the first time, by name (TAB) after that.

NAME_OR_URL is the address the browser IDE shows, a name you have opened before (TAB completes it), or nothing at all for the workspace you opened last.

From an address: its `workspace` or `folder` parameter says what to open — a .code-workspace file or a directory — and its last path segment suggests the tunnel name. Suggests, because an ingress serving WS-prod says nothing about what the box inside calls itself; when the stripped form is the one remembered it is used, when neither is remembered yet you are asked, and --name settles it without being asked.

Options:

- `--name TEXT` — The tunnel's name, when the address does not carry it (an ingress serving WS-prod fronts a box that names its tunnel WS). Wins over the name in the address.
- `--path, --folder TEXT` — What to open on the remote machine — a folder or a .code-workspace file. Default: whatever the address carries, else what this workspace was opened with last time.
- `--json` — Machine-readable output; never asks anything.
- `--help` — Show this message and exit.

#### `aisland tunnel start`

```text
aisland tunnel start [OPTIONS]
```

Bring up a tunnel to THIS machine, so a local editor can attach to it.

Fetches the VS Code CLI on first use (--cli installs it from an archive you uploaded instead, for a box that cannot reach the download host), then leaves the tunnel running in the background and reports what its log said: the tunnel's address once it is up, or the sign-in to complete when no account is stored yet. Running it again while a tunnel runs reports that one rather than starting a second. `aisland tunnel logs` and `aisland tunnel status` follow it from there.

Options:

- `--name TEXT` — Tunnel name. Default: $WORKSPACE, else this machine's hostname.
- `--provider [github|microsoft]` — Account to sign in with; remembered on this machine for the next start.
- `--cli FILE` — Install the VS Code CLI from this archive instead of downloading it (for a box that cannot reach it).
- `--json` — Machine-readable output.
- `--help` — Show this message and exit.

#### `aisland tunnel status`

```text
aisland tunnel status [OPTIONS]
```

Whether a tunnel to this machine is running, and under which account.

Run it on the machine you started the tunnel on: it reads that machine's own state (the process it started, the account it signed in with, the tunnel address once the log carries it). On your own machine it reports your laptop's tunnel, not the remote one.

Options:

- `--json` — Machine-readable output.
- `--help` — Show this message and exit.

#### `aisland tunnel stop`

```text
aisland tunnel stop [OPTIONS]
```

Stop the tunnel to this machine; saying nothing ran is not an error.

Run it on the machine the tunnel runs on. The remembered workspaces are untouched (`clear` forgets those), and the sign-in is kept, so a later `start` needs no interaction.

Options:

- `--json` — Machine-readable output.
- `--help` — Show this message and exit.

### `aisland wheels`

```text
aisland wheels [OPTIONS] [COMMAND] [ARGS]...
```

The wheels a project installed outside the workspace needs: build them, zip them, install a zip.

```text
aisland wheels build PROJECT [--include-published | --all] [--include-project] [--extra NAME] [--bundle] [--zip] [--commit] [--force] [--no-check] [--json]
aisland wheels zip PROJECT
aisland wheels install ZIP --python PYTHON [--into DIR] [-- install.py options]
aisland wheels prune PROJECT [--apply] [--all] [--json]
aisland wheels reset PROJECT [--json]
aisland wheels PROJECT …          (= aisland wheels build PROJECT …)
aisland wheels --install ZIP --python PYTHON [--into DIR]
```

Options:

- `--install FILE` — Short for `aisland wheels install ZIP`: unpack ZIP and install it into --python.
- `--python FILE` — With --install.
- `--into DIRECTORY` — With --install.
- `--help` — Show this message and exit.

Commands: `build`, `install`, `prune`, `reset`, `zip`.

#### `aisland wheels build`

```text
aisland wheels build [OPTIONS] PROJECT
```

Build the wheels PROJECT needs into PROJECT/wheels, with wheels.yaml and install.py; check the install.

By default only the projects PROJECT needs that are not on PyPI are built; --include-published builds every project it needs that has a checkout here, --all every workspace project that has one; --include-project builds PROJECT's own wheel too; --extra NAME writes the extra NAME into PROJECT's pyproject.toml, which installs PROJECT editable with every wheel built; --bundle merges PROJECT and the wheels it needs into one wheel that plain pip installs, in PROJECT/wheels/bundle. Each wheel is built from its project's last commit (a project with uncommitted changes is refused; --force builds it from its files as they are). wheels.yaml lists the wheels and which to install (yours to edit; a rerun keeps it); install.py installs them, naming each by its file, then PROJECT editable. Each dependency PROJECT still takes from PyPI is compared with its checkout, file by file; then install.py installs PROJECT in an empty venv and every module is imported. Exit 1 when that install or an import fails: the report names the project to release first; --no-check skips both checks. Git ignores the folder unless --commit, which commits it in PROJECT when the check passed.

```text
aisland wheels build models
aisland wheels build models --include-published --zip
aisland wheels build models --include-project --zip
aisland wheels build models --all --extra wheels
aisland wheels build models --include-published --bundle
aisland wheels build models --all --commit --json
aisland wheels build models --all --force --zip
aisland wheels build models --all --zip --no-check
```

Options:

- `--include-published` — Also build the projects PROJECT needs that are on PyPI, from their checkouts (the commit, not the release).
- `--all` — Build every workspace project that has a checkout here, needed or not.
- `--include-project` — Also build PROJECT's own wheel; install.py installs it in place of PROJECT's folder, and the zip then installs from any folder.
- `--extra NAME` — Also write the extra NAME into PROJECT's pyproject.toml, with a uv source per wheel built, so `uv pip install -e ./PROJECT[NAME]` installs PROJECT editable with every wheel; NAME must be in its [tool.aisland] manual-extras. Once written, every later build keeps it current without --extra, except one with --include-project. --commit commits pyproject.toml too.
- `--bundle` — Also merge PROJECT and the wheels it needs into ONE wheel, PROJECT/wheels/bundle/PROJECT_bundle-VERSION-py3-none-any.whl, that plain `pip install` installs; pip then knows only that one name (`pip show <dependency>` finds nothing). Checked alone in an empty venv of its own.
- `--zip` — Also pack the folder into PROJECT/wheels/PROJECT-wheels.zip.
- `--commit` — Commit PROJECT/wheels (the zip excepted) in PROJECT once the install check passed; without it git ignores the folder.
- `--force` — Build a project with uncommitted changes from its files as they are (those git ignores left out) instead of refusing it; its commit reads \<sha\>+uncommitted. Not with --commit.
- `--no-check` — Build and write the wheels only: compare nothing with PyPI and install nothing in an empty venv. Not with --commit.
- `--json` — Print the report as JSON (what an MCP tool would return).
- `--help` — Show this message and exit.

#### `aisland wheels install`

```text
aisland wheels install [OPTIONS] ZIP_PATH [REST]...
```

Unpack ZIP into the project's wheels/ and run its install.py with PYTHON; REST goes to install.py.

```text
aisland wheels install models-wheels.zip --python ~/envs/models/bin/python
aisland wheels install models-wheels.zip --python ~/envs/models/bin/python -- --dry-run
```

Options:

- `--python FILE` — The interpreter of the environment to install into (never the workspace's .venv). [required]
- `--into DIRECTORY` — The project's folder (default: the current one); the zip unpacks into its wheels/.
- `--help` — Show this message and exit.

#### `aisland wheels prune`

```text
aisland wheels prune [OPTIONS] PROJECT
```

Strip from PROJECT's history every committed wheel version no branch or tag holds.

A version stays when a branch or a tag of this clone or of origin holds it, so every branch tip and tag keeps exactly its files; only commit ids change. --apply refuses a clone with changes no commit holds or a branch that differs from origin's, backs every ref up into a bundle, rewrites, force-pushes origin's branches and tags in one atomic push (leased: a push someone made meanwhile refuses it all), shrinks this clone and re-records the workspace root's pointer to PROJECT. Every other clone then runs `aisland wheels reset PROJECT` before its next pull. Needs git-filter-repo: uv pip install 'aisland[prune]'.

--all removes every wheel from every branch and tag as well: no tip keeps a wheel, so the committed files of each branch and tag change, not only their commit ids.

```text
aisland wheels prune models
aisland wheels prune models --apply
aisland wheels prune models --all --apply
```

Options:

- `--apply` — Rewrite the history and force-push origin; without it the run only says what would go.
- `--all` — Strip EVERY wheel version from all branches and tags too, not only the ones none holds.
- `--json` — Print the report as JSON.
- `--help` — Show this message and exit.

#### `aisland wheels reset`

```text
aisland wheels reset [OPTIONS] PROJECT
```

Bring this clone of PROJECT onto the history `aisland wheels prune` rewrote on another machine.

Strips the wheel versions of each of origin's prunes this clone still reaches — the same rewrite, so every branch that was origin's lands on origin's new commit, and commits origin lacks are kept on top. Changes no commit holds are set aside and put back; every ref is backed up into a bundle first. When the workspace root still records an old commit of PROJECT, it records the commit's rewritten twin — also in a clone already reset. Run it before the next pull: a pull merges the old history back in. Nothing pruned on origin, nothing changes.

```text
aisland wheels reset models
```

Options:

- `--json` — Print the report as JSON.
- `--help` — Show this message and exit.

#### `aisland wheels zip`

```text
aisland wheels zip [OPTIONS] PROJECT
```

Pack PROJECT/wheels as it is into PROJECT/wheels/PROJECT-wheels.zip.

The zip installs with plain Python, from PROJECT's folder on any machine: `python PROJECT-wheels.zip` unpacks it into ./wheels and runs wheels/install.py (every other argument goes to install.py). A folder that installs PROJECT's own wheel (built with --include-project) makes a zip that installs from any folder.

Options:

- `--help` — Show this message and exit.

### `aisland workspace`

```text
aisland workspace [OPTIONS] COMMAND [ARGS]...
```

Create a workspace aisland runs in, or write this one down for another machine.

Options:

- `--help` — Show this message and exit.

Commands: `bashrc`, `code-workspace`, `color`, `configure`, `env-example`, `env-test`, `harness`, `init`, `mirror`, `projects`, `provision`, `show`, `test-stats`, `theme`, `track`.

#### `aisland workspace bashrc`

```text
aisland workspace bashrc [OPTIONS]
```

Link the workspace's project.bashrc to aisland's copy — the step `aisland setup` takes, alone.

A real file in its place is refused (it may hold your own lines: put them in user.bashrc); a link pointing elsewhere is replaced.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace code-workspace`

```text
aisland workspace code-workspace [OPTIONS]
```

Write the workspace's VS Code file, \<workspace\>.code-workspace — the step `aisland setup` takes, alone.

Its folders follow what is checked out here, its settings the template, its theme and colours this machine's choice (`aisland workspace theme|color`). An unchanged file is left as it is, so VS Code does not reload.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace color`

```text
aisland workspace color [OPTIONS] COMMAND [ARGS]...
```

This machine's background and foreground colour for the workspace window, over any theme.

Kept in .git/config (never committed). The colours go on the window frame — title bar, activity bar, status bar — and, with --area sidebar, on the side bar too, or, with --area both, on the code too: editor, side bar, panel and terminal.

Options:

- `--help` — Show this message and exit.

Commands: `list`, `set`, `unset`.

#### `aisland workspace color list`

```text
aisland workspace color list [OPTIONS] [PART]
```

The colour names to choose from (CSS's 148), each with its swatch and value; PART keeps the names holding it. The first line is this workspace's choice; ← marks the chosen names.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace color set`

```text
aisland workspace color set [OPTIONS] {background|foreground} COLOR
```

Colour the workspace window's ROLE (background or foreground) COLOR, whatever the theme.

COLOR is a CSS name (`aisland workspace color list`: navy, dark slate blue) or #RGB, #RGBA, #RRGGBB, #RRGGBBAA: `aisland workspace color set background navy --area both`.

Options:

- `--area [frame|sidebar|both]` — frame: the title bar, activity bar and status bar; sidebar: those and the side bar; both: those and the editor, side bar, panel and terminal. Kept for later colours. Default: the kept one, else frame.
- `--help` — Show this message and exit.

#### `aisland workspace color unset`

```text
aisland workspace color unset [OPTIONS] [background|foreground]
```

Give ROLE's colour back to the theme; without ROLE, both colours (and the area).

Options:

- `--help` — Show this message and exit.

#### `aisland workspace configure`

```text
aisland workspace configure [OPTIONS] [COMMAND] [ARGS]...
```

Each plugin's settings of the workspace (workspace.toml) and of this machine (.git/config).

```text
aisland workspace configure                                  every plugin, and how to set it
aisland workspace configure source                           where each project's package comes from
aisland workspace configure source set models pypi
aisland workspace configure source set models local --branch dev/main --machine-only
aisland workspace configure source unset models
aisland workspace configure extras set dev docs              the extras every project installs
aisland workspace configure extras set dev --machine-only
```

Options:

- `--help` — Show this message and exit.

Commands: `active`, `extras`, `mirror`, `source`.

#### `aisland workspace configure active`

```text
aisland workspace configure active [OPTIONS] [COMMAND] [ARGS]...
```

Which projects this machine has (a project with a committed value is optional, the value its default).

Options:

- `--json` — Print the rows and notes as one JSON object.
- `--help` — Show this message and exit.

Commands: `set`, `unset`.

#### `aisland workspace configure active set`

```text
aisland workspace configure active set [OPTIONS] PROJECT {true|false}
```

Set PROJECT's active: true: checked out and installed; false: left out, its folder empty.

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace configure active unset`

```text
aisland workspace configure active unset [OPTIONS] PROJECT
```

Remove PROJECT's active settings (with --machine-only, this machine's only).

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace configure extras`

```text
aisland workspace configure extras [OPTIONS] [COMMAND] [ARGS]...
```

Which extras should this machine install?

Options:

- `--json` — Print the rows as one JSON object.
- `--help` — Show this message and exit.

Commands: `set`, `unset`.

#### `aisland workspace configure extras set`

```text
aisland workspace configure extras set [OPTIONS] [NAMES]...
```

Choose the extras to install (the whole list) and reinstall the projects offering them.

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace configure extras unset`

```text
aisland workspace configure extras unset [OPTIONS] [NAMES]...
```

Forget the extras choice (with --machine-only, this machine's only); setup asks again. With NAME…, drop just those extras from the choice and reinstall the projects offering them.

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace configure mirror`

```text
aisland workspace configure mirror [OPTIONS] [COMMAND] [ARGS]...
```

A second host each project is copied to (`aisland workspace mirror sync` pushes it).

Options:

- `--json` — Print the rows and notes as one JSON object.
- `--help` — Show this message and exit.

Commands: `set`, `unset`.

#### `aisland workspace configure mirror set`

```text
aisland workspace configure mirror set [OPTIONS] PROJECT MIRROR
```

Set PROJECT's mirror: The mirror's git address (ssh or https); the primary stays the .gitmodules remote.

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace configure mirror unset`

```text
aisland workspace configure mirror unset [OPTIONS] PROJECT
```

Remove PROJECT's mirror settings (with --machine-only, this machine's only).

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace configure source`

```text
aisland workspace configure source [OPTIONS] [COMMAND] [ARGS]...
```

Where each project's package comes from.

Options:

- `--json` — Print the rows and notes as one JSON object.
- `--help` — Show this message and exit.

Commands: `set`, `unset`.

#### `aisland workspace configure source set`

```text
aisland workspace configure source set [OPTIONS] PROJECT {local|github|pypi}
```

Set PROJECT's source: local: checked out, installed editable; github: no checkout, installed from the remote; pypi: the published package.

Options:

- `--branch TEXT` — The branch a local or github source checks out or installs from (default: the project's).
- `--version TEXT` — A version specifier for a pypi source, such as '\>=0.1' (default: the latest).
- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace configure source unset`

```text
aisland workspace configure source unset [OPTIONS] PROJECT
```

Remove PROJECT's source settings (with --machine-only, this machine's only).

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace env-example`

```text
aisland workspace env-example [OPTIONS]
```

Write env.example and env.complete from each project's own: the variables a person normally sets, and all.

Each line is commented out, so a new machine copies env.example (`cp env.example .env`) and uncomments what it needs. A variable several projects read is listed once, under the project that defines it, naming the others. An unchanged file is left as it is; `aisland setup` writes them too. --missing prints, ready to paste, the variables of env.complete an existing .env does not mention.

Options:

- `--missing` — Also print the variables .env does not mention yet (.env is never edited).
- `--help` — Show this message and exit.

#### `aisland workspace env-test`

```text
aisland workspace env-test [OPTIONS] [PROJECTS]...
```

Write PROJECT's tests/test_env_example.py: it fails when the project reads a variable its env.complete lacks.

No PROJECT: every project with a checkout. A project without an env.complete gets no test; an unchanged test is left as it is.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace harness`

```text
aisland workspace harness [OPTIONS] [COMMAND] [ARGS]...
```

The agent harness sets the workspace combines — each one's commands, scripts, rules and other files, in the order workspace.toml lists them (aisland's own when it lists none). `install` installs them, as `aisland setup` does; `export` copies aisland's set to start one of your own.

```text
[harness]
sets = ["aisland", "models", { git = "https://github.com/acme/agent-rules", ref = "v2" }]
```

Options:

- `--json` — Print the sets as one JSON object.
- `--help` — Show this message and exit.

Commands: `export`, `install`.

#### `aisland workspace harness export`

```text
aisland workspace harness export [OPTIONS] FOLDER
```

Copy aisland's own harness set into FOLDER (new or empty), to start a set of your own from it.

```text
aisland workspace harness export agents
```

Options:

- `--help` — Show this message and exit.

#### `aisland workspace harness install`

```text
aisland workspace harness install [OPTIONS]
```

Install the harness sets into the workspace, as `aisland setup` does.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace init`

```text
aisland workspace init [OPTIONS] FOLDER
```

Make FOLDER a workspace: git init, the starting files, the submodules and the git aliases.

Writes .gitignore, .env, mypy.ini and vscode-init-bash.sh, never over a file that exists. A folder that already is a git repository is set up in place: no git init, the missing files written, the --add projects added (--from and --local are refused there). --from names a workspace.toml: each of its projects is cloned into its path on its branch and the file is copied in as the new workspace's own; a file that cannot be read is refused, naming its line, before anything is written.

--rewrite FILE writes FILE again over the one there, keeping it as FILE.bak (refused when FILE.bak exists); in an existing repository it is all init does: `aisland workspace init . --rewrite vscode-init-bash.sh`.

--local makes a new root repository that tracks only the projects (its .gitignore ignores everything; add a project later with `git submodule add -f <url>`, then `aisland setup`). --untracked makes the root record none of the projects' commits: `aisland workspace track unset`, for every project.

Options:

- `--add URL` — A repository to add as a submodule (a project of the workspace). Repeat for several.
- `--from FILE` — A workspace.toml (`aisland setup` keeps one in every workspace): each of its projects is added as a submodule in its path, on its branch, before the --add ones; the file becomes the new workspace's own.
- `--rewrite FILE` — A starting file to write again from aisland's template, the old copy kept as FILE.bak. Repeat for several. In an existing repository only these files are written.
- `--local` — A new root repository no one shares: it tracks only .gitmodules and the projects, and ignores everything else.
- `--untracked` — The root records none of its projects' commits: `git status` never shows them, `git pull-all` never records them (`aisland workspace track set` undoes it).
- `--help` — Show this message and exit.

#### `aisland workspace mirror`

```text
aisland workspace mirror [OPTIONS] COMMAND [ARGS]...
```

The mirror plugin's action: copy each project to its mirror (set it with `aisland workspace configure mirror`).

Options:

- `--help` — Show this message and exit.

Commands: `sync`.

#### `aisland workspace mirror sync`

```text
aisland workspace mirror sync [OPTIONS] [PROJECT]
```

Push the primary's dev/main, main, every other branch checked out here and tags to PROJECT's mirror.

Default: every mirror. Each branch is copied as the primary has it, so an unpushed commit never reaches the mirror, and a local branch the primary lacks is reported, not pushed. Fast-forward only: a branch the mirror moved is refused, a tag that differs is never overwritten, and nothing on the mirror is ever deleted. Exit 1 when anything was refused or failed.

Options:

- `--dry-run` — Fetch and show what a sync would push; push nothing.
- `--help` — Show this message and exit.

#### `aisland workspace projects`

```text
aisland workspace projects [OPTIONS]
```

The workspace's projects with a checkout, every dependency before the projects that need it: one folder a line, or with --json each project's folder and distribution name.

```text
for project in $(aisland workspace projects); do git -C "$project" status --short; done
```

Options:

- `--json` — Print them as one JSON object.
- `--help` — Show this message and exit.

#### `aisland workspace provision`

```text
aisland workspace provision [OPTIONS] [COMMAND] [ARGS]...
```

The system tools this machine needs (Homebrew on macOS, aisland's downloads on Linux): who needs each, and whether it is installed. `install` installs them, as `aisland setup` does.

```text
aisland workspace provision
aisland workspace provision install
```

Options:

- `--json` — Print the rows as one JSON object.
- `--help` — Show this message and exit.

Commands: `install`.

#### `aisland workspace provision install`

```text
aisland workspace provision install [OPTIONS]
```

Install every system tool this machine needs: aisland's own and the projects' sys-dependencies.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace show`

```text
aisland workspace show [OPTIONS]
```

What the workspace's projects declare: selections, services and code plugins, and from which project.

Read from each project's pyproject.toml ([tool.aisland.selection.\*], [tool.aisland.service.\*], [project.entry-points."aisland.plugins"]) — nothing needs to be installed; a code plugin shows whether its project is. A declaration aisland cannot use is refused, naming its file and line.

Options:

- `--json` — Print it as one JSON object.
- `--help` — Show this message and exit.

#### `aisland workspace test-stats`

```text
aisland workspace test-stats [OPTIONS] COMMAND [ARGS]...
```

Whether the pytest plugin records the projects' test runs (for `aisland tests report`).

```text
aisland workspace test-stats off                       every project, and every one added later
aisland workspace test-stats off models                one project
aisland workspace test-stats off --machine-only        this machine only (.git/config)
aisland workspace test-stats on models                 recorded again
```

Options:

- `--help` — Show this message and exit.

Commands: `off`, `on`.

#### `aisland workspace test-stats off`

```text
aisland workspace test-stats off [OPTIONS] [PROJECT]...
```

Stop recording PROJECT's test runs (no PROJECT: every project, and every one added later).

Written for everyone as `test-stats = false` in workspace.toml (commit it), or with --machine-only in .git/config alone; this machine's choice wins over the committed one.

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace test-stats on`

```text
aisland workspace test-stats on [OPTIONS] [PROJECT]...
```

Record PROJECT's test runs again (no PROJECT: every project). Without --machine-only it also drops this machine's own choice for them.

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace theme`

```text
aisland workspace theme [OPTIONS] COMMAND [ARGS]...
```

This machine's VS Code theme for the workspace window, kept in .git/config (never committed).

The workspace file's `workbench.colorTheme`: this window only — every other window keeps your user setting.

Options:

- `--help` — Show this message and exit.

Commands: `list`, `set`, `unset`.

#### `aisland workspace theme list`

```text
aisland workspace theme list [OPTIONS]
```

The themes VS Code offers on this machine: name, kind, the extension that brings it; \* marks the workspace's. Without VS Code here (a remote machine), a line saying the themes are your local VS Code's.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace theme set`

```text
aisland workspace theme set [OPTIONS] NAME
```

Show the workspace window in theme NAME (as `aisland workspace theme list` prints it, case ignored).

On a machine without VS Code (a remote one: its window's themes are your local VS Code's) NAME is kept as typed, unchecked.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace theme unset`

```text
aisland workspace theme unset [OPTIONS]
```

Forget the workspace's theme: the window shows your VS Code user setting again.

Options:

- `--help` — Show this message and exit.

#### `aisland workspace track`

```text
aisland workspace track [OPTIONS] COMMAND [ARGS]...
```

Whether the root records the projects' commits: set it, unset it, or see what is set.

```text
aisland workspace track unset                     every project: the root records none of their commits
aisland workspace track unset models              one project
aisland workspace track set models                record its commits again
aisland workspace track unset --machine-only      this machine only (.git/config); nothing to commit
aisland workspace track status                    which projects, whose choice, whether git agrees
```

Options:

- `--help` — Show this message and exit.

Commands: `set`, `status`, `unset`.

#### `aisland workspace track set`

```text
aisland workspace track set [OPTIONS] [PROJECT]...
```

Record PROJECT's commits in the root again (no PROJECT: every submodule).

A project whose commits moved meanwhile shows again in `git status` (` M <project>`), and the next `git pull-all` records it once origin has them. Without --machine-only it also drops this machine's own choice.

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.

#### `aisland workspace track status`

```text
aisland workspace track status [OPTIONS] [PROJECT]...
```

Whether the root records PROJECT's commits, whose choice that is and whether git agrees (no PROJECT: every submodule). Changes nothing.

With no PROJECT it says the workspace's choice and lists the projects that differ from it, or that git does not yet agree with (`aisland setup` writes git's setting); the others are counted. With PROJECTs it gives a line each.

Options:

- `--json` — Print the rows as one JSON object.
- `--help` — Show this message and exit.

#### `aisland workspace track unset`

```text
aisland workspace track unset [OPTIONS] [PROJECT]...
```

Stop recording PROJECT's commits in the root (no PROJECT: every submodule).

`git status` in the root no longer shows the project's new commits, `git add <project>` skips it and `git pull-all` never records them; the root keeps the commit it last recorded. Written for everyone as `track = false` in workspace.toml and `ignore = all` in .gitmodules (commit them), or with --machine-only in .git/config alone.

Options:

- `--machine-only` — Change this machine only (.git/config); commit nothing.
- `--help` — Show this message and exit.
