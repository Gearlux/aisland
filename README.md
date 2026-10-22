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
`~/.bashrc` stays clean and re-running setup never dirties the tree).
`aisland setup` itself installs the git aliases (`git pull-all`, …) on every run.

### Another workspace: aisland from a wheel (`aisland workspace init`)

aisland also runs installed from a wheel, in a workspace of your own. Built here, the wheel
carries everything aisland reads (the editor-file template, the git alias scripts, the shell init):

```bash
python -m pip wheel --no-deps -w dist ./aisland   # → dist/aisland-0.1.0-py3-none-any.whl
```

Make a workspace, give it a venv holding aisland, and set it up:

```bash
aisland workspace init ~/work/acme --add git@github.com:acme/models.git
cd ~/work/acme
uv venv --seed .venv && .venv/bin/pip install aisland-0.1.0-py3-none-any.whl
.venv/bin/aisland setup
```

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
Next:
    cd /Users/you/work/acme
    uv venv --seed .venv && .venv/bin/pip install <the aisland wheel>
    .venv/bin/aisland setup
```

`init` never writes over a file that exists, and refuses a folder that already is a git
repository (set that one up with `aisland setup`). Every file it writes is a starting point you
own. A workspace is a folder whose projects are subfolders holding a `pyproject.toml` —
submodules listed in `.gitmodules`, or, without one, every such subfolder (next section);
`aisland setup` needs the `.venv` and reads `workspace.toml`, `config.toml`, `.env` and `mypy.ini` when they
exist.

To take a newer template of a starting file, name it with `--rewrite` (repeat for several). In an
existing repository that is all `init` does; the old copy is kept as `<file>.bak`, and the run is
refused when that `.bak` already exists:

```
$ aisland workspace init . --rewrite vscode-init-bash.sh
==> Rewrote in /Users/you/work/acme
    vscode-init-bash.sh       rewritten (the old one kept as vscode-init-bash.sh.bak)
```

#### The same workspace on another machine: `workspace.toml`

Every workspace keeps one committed file of its projects, `workspace.toml`, shaped like `.gitmodules`.
`aisland setup` keeps it current — a new submodule is added, a changed url or folder followed, a project
that is gone dropped — and its first lines say how to start on a machine that has no aisland yet:

```toml
# workspace.toml — the acme workspace: its projects and its choices, committed.
# ...
#   1. A virtual environment with aisland in it. ...
#        uv venv --seed --prompt acme .venv
#        uv pip install --python .venv/bin/python aisland
#   2. ...
#        .venv/bin/aisland workspace init . --from workspace.toml
#   3. ...
#        .venv/bin/aisland setup
[project."models"]
path = "models"
url = "git@github.com:acme/models.git"
branch = "dev/main"

[project."machine_learning"]
path = "ml"
url = "git@github.com:acme/machine_learning.git"
```

A project is named after its repository; `path` is its folder (its name when left out) and `branch` the
branch it is cloned on (without one, the repository's default branch). A branch already in the file is
kept: setup only writes one for a project that has none. A link in the workspace folder to a project
(`ml -> machine_learning`) is written as its `path`, so a workspace that uses links comes back with
direct checkouts.

Take the file to the new machine and follow its three steps in the folder that will hold the workspace.
`init --from` clones each project as a submodule into its `path` on its `branch`, before any `--add`, and
the file becomes the new workspace's own. A file aisland cannot read is refused before anything is
written, naming the line:

```text
$ .venv/bin/aisland workspace init . --from workspace.toml
Error: workspace.toml:7: project 'tools' has no url
```

aisland finds the venv at `.venv` in the workspace. To give it a name, use `--prompt` (the shell
then shows `(acme)`), or keep the venv elsewhere and link it in — `.gitignore` covers the link:

```bash
python3 -m venv ~/venvs/acme && ln -s ~/venvs/acme .venv
```

Run from that `.venv`, `init` leaves the venv line out of its `Next:` steps. A branch of `.` in
`.gitmodules` (follow the superproject's) is written as the root's branch.

How aisland finds its workspace depends on how it is installed:

| installed | the workspace | aisland's state (`data/`, `bin/`, `tools/`) |
|---|---|---|
| from this checkout (editable) | the workspace this checkout is in, wherever you run it | `<workspace>/aisland/` |
| from a wheel | `AISLAND_WORKSPACE` when set, else the nearest folder above where you run it that holds `.gitmodules`; the folder you run it in when it holds projects; from inside a project, the project's parent; a git repository with no project yet | `<workspace>/.aisland/` |

From a wheel, outside a workspace, a command is refused before it writes anything; only
`aisland workspace …`, `aisland wheels …` and `--help` run anywhere:

```text
$ aisland status
Error: No workspace here: /Users/you/Downloads is in no git repository and holds no project. Run aisland in your workspace or one of its projects, set AISLAND_WORKSPACE to it, or create one with `aisland workspace init <folder>`.
```

From a wheel, the git aliases `aisland setup` installs (`git pull-all`, …) name the scripts by
their place in the venv: after recreating the venv or installing a new aisland, run
`aisland setup` again. The editor file names the workspace's own folder
(`${workspaceFolder:acme}`, the `acme-bash` terminal profile) and takes the bash on `PATH` when
the template's is missing on the machine. aisland's own system tools (process-compose for the
services, Homebrew's bash) are not provisioned from a wheel; install them yourself before
`aisland start`.

#### What the projects declare (`aisland workspace show`)

A project tells aisland what it brings in its own `pyproject.toml` — nothing needs to be installed for aisland to
read it, and the project never imports aisland:

```toml
[tool.aisland.selection.framework]        # the project that asks the question defines the selection
question = "Which ML frameworks should this machine install?"
choices = ["torch", "keras"]              # each one an extra of this project; other projects add their own

[tool.aisland.service.aim]                # a service the project needs
command = ["aim", "up", "--repo", "{data}", "--port", "{port}"]
port = 43800
ready = "/"

[project.entry-points."aisland.plugins"]  # code, for what a declaration cannot say
acme-db = "acme_plugin:Database"
```

`aisland workspace show` lists what every project of the workspace declares (`--json` for the same as one object):

```text
$ aisland workspace show
Declared by the projects:
  selection  framework  matrainer  choices: torch, keras (matrainer), torch (sonair)
  service    aim        matrainer  port 43800
  plugin     acme-db    acme       acme_plugin:Database — not installed
```

A declaration aisland cannot use stops it with one line naming the file and the line of the table:

```text
Error: /path/to/sonair/pyproject.toml:8: sonair offers 'jax' for selection 'framework', but defines no extra of that name
```

#### A workspace without `.gitmodules`: a folder of projects

A folder whose subfolders are projects — plain folders, or each its own git clone — is a
workspace as it is: aisland reads it as if every subfolder holding a `pyproject.toml` were an
active submodule (hidden folders and folders without one are not projects):

```text
acme/                  ← the workspace (no .gitmodules, need not be a git repository)
├── alpha/             ← a project: pyproject.toml (here its own git clone)
├── beta/              ← a project, needing alpha
└── docs/              ← not a project: no pyproject.toml
```

```text
$ cd acme/beta && aisland wheels build beta
beta needs 1 workspace project that is not on PyPI. Its wheel is in beta/wheels (1 kB):
  alpha  0.1.0  main 3a126e4
```

The workspace git aliases work over the clones: `aisland setup` installs them into every clone, and the root when it is a repository. Each clone
merges and pushes its OWN branch — independent clones share none — and the root goes last; there
are no pointers to record:

```text
$ git pull-all            # from any clone
fetching 2 repositories…
alpha                  merged origin/main (1 commit)

$ git push-all
alpha                  main up to date
beta                   pushed main (1 commit)
```

`git pull-all` lists what needs you (edits in the way, `--autostash`, a detached clone, a
conflict) as it does for submodules, and `git push-all` pushes the root only when every clone
pushed. A root repository with no `origin` remote is neither pulled nor pushed. The others loop over the clones too, the root first for `status-all` and last otherwise:

```text
$ git status-all
alpha                  (@main)
 M f.txt
beta                   (@dev, 1 ahead)

$ git switch-all feature          # git merge-all <ref> reports the same way
alpha                  switched to feature
beta                   not switched: error: pathspec 'feature' did not match any file(s) known to git

$ git tag-all v1 "first release"  # every clone tagged first, then each tag pushed
```

`git safe "<message>"` runs the gates of the clone you are in with the workspace's `.venv` — mypy
from the workspace root against the clone's folder — then commits and pushes that clone.

What only a submodule can do is refused with the reason:

```text
$ aisland workspace configure source set alpha pypi
Error: 'alpha' is a folder of the workspace, not a submodule: a pypi source empties its folder, and nothing could check it out again. Keep it local.
```

A wheel is built from a project's last commit, so a project folder in no git repository is
refused (`alpha is in no git repository; …`); `aisland mirror` needs a submodule's `.gitmodules`
remote and is refused as before.

### Agent commands and rules in every workspace (the harness)

`aisland setup` and `aisland workspace init` give every workspace the same agent commands and the same generic agent
rules, so Claude Code (and Gemini CLI) behave alike in all of them:

| In the workspace | What it is |
| --- | --- |
| `.agents/workflows/*.md` | the commands `/commit-all`, `/commit-session`, `/list-actions`, `/pull-all`, `/push-all`, `/ship` |
| `.claude/commands` | a link to `.agents/workflows`, where Claude Code looks for them — at the root and in every project folder (Claude Code reads only the folder a session starts in); a project's link is kept out of git through its repository's own `.git/info/exclude` |
| `.agents/scripts/stage_hunks.py` | the helper `/commit-session` runs to stage only some hunks of a file |
| `.agents/aisland/AGENTS.md` | the generic rules; the workspace's `AGENTS.md` imports them with its first line |
| `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` | written once, when the workspace has no `AGENTS.md`: the import line, then room for the workspace's own rules; the other two link to it |

From an aisland **checkout** each file is a link into aisland's package, so an edit is made in aisland and committed
there; from a **wheel** each is a copy. Every installed file is listed in the root `.gitignore` by its own path, so a
command of the workspace's own in `.agents/workflows/` is never touched and can be committed.

A copy someone edited is kept, and setup says so:

```text
$ aisland setup
  - kept your edited .agents/workflows/ship.md — delete it to take aisland's
```

An `AGENTS.md` of the workspace's own is never edited; when it does not import the rules, setup names the line:

```text
  - AGENTS.md does not import the aisland rules — add "@.agents/aisland/AGENTS.md" as its first line
```

The rules and commands live in `aisland/harness/` (`AGENTS.md`, `commands/`, `scripts/`); why they are installed this
way: [docs/architecture.md](docs/architecture.md) → "Every workspace gets aisland's agent commands and rules".

### Shell completion

Enable it by starting a shell that sources `project.bashrc`:

```bash
source project.bashrc      # activates .venv AND exports .env AND loads completion
                           # (and defines cdp/cdd/cdm/cdr — `cdm rai<TAB>` completes a folder under $MODEL_ROOT —
                           #  and h [word]: the history, filtered)
                           # (and tm [tmux new-session options]: a tmux session here, with this shell's environment)
aisland <TAB>              # check docker fiftyone info jenkins labelstudio mcp mlflow ...
aisland wheels build vendorcli <TAB>   # --include-published --all --include-project --zip ... --help
streamstudio exp<TAB>      # export export-workflow
recordstream <TAB>         # run
```

Once an `aisland` command has every argument it needs, TAB on an empty word offers its options (those
not typed yet); a command group offers its commands.

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

`aisland setup` also writes the **VS Code workspace file**,
`acme.code-workspace` (named after the workspace), from scratch on every
run. Its folder list is what this machine has: the workspace, every project
with a checkout in dependency order (`loggair` before `confluid`), and the
folder loggair writes its log files into, when that folder exists. That folder
is asked from loggair itself (`python -m loggair --json`, from the workspace
root with `.env` loaded), so a `log_dir` in `~/.config/loggair/config.yaml` or
a `LOGGAIR_DIR` in `.env` decides it. A project set to `pypi` or `github`, or
an inactive optional submodule, has no checkout and is not listed.
A short name you give a project folder with a link in the workspace root
(`ln -s machine_learning ml`) is the name the folder shows under, its path still
the real folder; with two links to one folder the first by name wins. Such a
link is never a project of its own: setup, its extras prompt, `aisland workspace configure source`
list the folder once, under its real name, and `workspace.toml` writes the link as
the project's `path`. A link to a folder outside the workspace is a project like
any other.
`aisland workspace configure source set|unset` rewrite the file too, `--machine-only`
included. The settings and extension recommendations come from
`aisland/aisland/code-workspace.json` (inside the package, so a wheel carries it): change a shared setting there and re-run
`aisland setup`. The file is gitignored and rewritten, so a setting you change
in VS Code at *Workspace* scope is gone at the next setup — put a personal one
in your *User* settings.

#### This machine's theme and colours (`aisland workspace theme|color`)

The workspace window can have its own theme and its own background and
foreground colour, chosen on this machine only: the choice is kept in
`.git/config` (`aisland.editor.*`), never in a committed file, and every
rewrite of the workspace file carries it.

```bash
aisland workspace theme list                  # the themes VS Code offers here; * marks the workspace's
aisland workspace theme set "Solarized Dark"  # this window only — the others keep your user setting
aisland workspace theme unset
aisland workspace color list slate            # CSS's 148 colour names with a swatch; a part of the name filters
aisland workspace color set background navy   # title bar, activity bar and status bar
aisland workspace color set foreground '#e0e0e0' --area both   # the frame and the editor, side bar, panel, terminal
aisland workspace color set background navy --area sidebar     # the frame and the side bar; the code keeps the theme's
aisland workspace color unset background      # without a role: both colours go back to the theme
```

```text
$ aisland workspace color list slate
This workspace: background: navy (#000080), foreground: #e0e0e0, area: both
       darkslateblue         #483d8b
       darkslategray         #2f4f4f
       ...
```

A theme is named as `theme list` prints it (case ignored); the list is read
from VS Code's own folder and `~/.vscode/extensions`, and is empty where VS
Code is not installed — `theme set` is refused there. A colour is a CSS name
(`navy`, `dark slate blue`) or `#RGB`, `#RGBA`, `#RRGGBB`, `#RRGGBBAA`. The
colours hold whatever the theme: they are written unscoped into
`workbench.colorCustomizations`, which VS Code lays over the active theme.
`--area` (`frame`, the default; `sidebar`, the frame and the side bar; or `both`) is kept for later colours.

The root **`mypy.ini`** is the opposite: committed, hand-maintained policy
(the per-module `[mypy-*]` sections). Setup only keeps its `mypy_path`
line complete — any discovered project missing from it is **appended**
(never removed: inactive submodules stay listed and mypy ignores path
entries that don't exist on a given machine, so the committed file stays
identical across machines).

#### Optional submodules & the generated `uv.toml`

Some projects are **optional** — they ship their own `uv.toml` (a private
package index) or are marked `active = false` in `.gitmodules` (e.g. the
corporate-only `vendorcli`). `aisland setup` **prompts**
whether to activate each one and remembers the answer **per-machine** in
`.git/config` (`submodule.<name>.active`), never in the committed
`.gitmodules`. A decided module is honored silently on later runs; a *new*
optional module is prompted **even with `--non-interactive/-y`** (a machine
must consciously opt into a private project).

`--activate <name>` / `--deactivate <name>` (repeatable) decide ANY submodule
on this machine without a prompt, optional or not, named by its name or its
path. Activating runs `git submodule update --init`; deactivating deinits the
working tree (never with `-f`: a folder with uncommitted changes is left and
named) and keeps the choice, so later runs leave the folder empty. A name that
is no submodule stops setup before it changes anything.

```bash
aisland setup                          # interactive: prompts for optional submodules
aisland setup -y                       # reuse saved decisions; still ask about new modules
aisland setup --activate vendorcli        # opt this machine into vendorcli (no prompt)
aisland setup --deactivate navigaitor  # leave navigaitor out on this machine (empties its folder)
```

```text
$ aisland setup --deactivate navigatior
Error: --deactivate navigatior: no submodule of that name (the submodules: confluid, liquifai, …)
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
#   ✓ tunnel-relay     no tunnel configured here (nothing in /path/to/aisland/data/tunnel)
#   ✓ tunnel-connect   `aisland tunnel connect` not used here (nothing in /path/to/aisland/data/tunnel)
#
# All 9 checks passed.
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
| `tunnel-relay` | A machine that has used `aisland tunnel` reaches the relay the tunnel dials out to (probed only there) | The tunnel starts, logs nothing useful and never appears in the editor; the fix line is the `curl` that confirms it |
| `tunnel-connect` | A machine that has used `aisland tunnel connect` has `devtunnel` on PATH, `msgpack` (the `[connect]` extra) and a signed-in `devtunnel` (checked only there; offline, it passes with a note) | `connect` stops with `No 'devtunnel' command found`, `needs msgpack` or `devtunnel is not signed in`; the fix line installs or signs in |

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
aisland tests report navigaitor --days 90
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
| A project with no checkout (`github` / `pypi` source) is refused | Nothing to clean; see `aisland workspace configure source` |
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
aisland wheels build navigaitor
aisland wheels build navigaitor --include-published   # also the projects it needs that ARE on PyPI
aisland wheels build navigaitor --all                 # every workspace project with a checkout here
aisland wheels build navigaitor --include-project     # also navigaitor's own wheel: installs with no source
aisland wheels build navigaitor --zip                 # also pack the folder into one zip
aisland wheels build navigaitor --commit              # commit the folder in navigaitor
aisland wheels build navigaitor --force               # build projects with uncommitted changes as they are
aisland wheels build navigaitor --no-check            # build and write the wheels only, check nothing
aisland wheels build navigaitor --json                # the same report as data
aisland wheels navigaitor …                           # short for `aisland wheels build navigaitor …`
```

| Flag | Built | Installed by default |
|---|---|---|
| *(none)* | the projects it needs that are not on PyPI | all of them |
| `--include-published` | every project it needs that has a checkout here — the commit, not the release | all of them |
| `--all` | every workspace project that has a checkout here | the ones it needs |

A project the flag covers that has no checkout on this machine is not built: the report says so
(`Not built: loggair (no checkout here — taken from PyPI 0.2.0)`) and it comes from PyPI.

`--include-project` builds the named project's own wheel too, beside any of the flags above. It
follows the same rules as the others — built from the last commit, refused with uncommitted changes
unless `--force` (its own `wheels/` folder does not count) — and the report lists it after the rest:

```text
navigaitor's own wheel (--include-project), installed in place of its folder:
  navigaitor  0.5.0  dev/main <sha>
```

`install.py` then installs that wheel instead of the folder, editable, so the other machine needs no
checkout of the project, and the zip installs from any folder. The empty-venv check installs and
imports that same wheel.

TAB completes the project: `aisland wheels build <TAB>` offers every project checked out here,
`aisland wheels zip <TAB>` those that have a `wheels/wheels.yaml`, and `aisland wheels <TAB>` the
commands, then the projects.

What it printed for navigaitor on 2026-10-19 (24 s; exit 0) — every workspace project it needs is on PyPI, so it built
nothing and checked the PyPI half:

```text
navigaitor needs no workspace project that is not on PyPI; navigaitor/wheels holds no wheel.

It takes 4 workspace projects from PyPI, compared with this checkout:
  confluid      PyPI 0.4.1    behind: 3 files differ (confluid/__init__.py, confluid/resolver.py, confluid/scopes.py)
  liquifai      PyPI 0.3.1    behind: 9 files differ (liquifai/__init__.py, liquifai/completion/cache.py, liquifai/completion/engine.py, …)
  loggair       PyPI 0.2.1    up to date
  recordstream  PyPI 0.1.0a3  behind: 6 files differ (recordstream/ops/__init__.py, recordstream/ops/image.py, recordstream/ops/predict.py, …)

Installed navigaitor in an empty venv (its wheels + PyPI) and imported every module of its 1 package:
  17 modules imported

Not imported, missing what no workspace project ships (an optional extra's?):
  navigaitor.server         ModuleNotFoundError: No module named 'sonair'
  navigaitor.visualisation  ModuleNotFoundError: No module named 'matplotlib'

Behind, nothing breaks: confluid, liquifai, recordstream
```

- **Behind**: the wheel this checkout builds differs from the one on PyPI, file by file. The
  wheels were built against this checkout, so they may use what no release has yet.
- **Release first**: a module failed to import in an empty venv, and the module it missed
  belongs to that project. Release it, then run `aisland wheels` again. Every module is
  imported, not only the packages: a module no package's own import reaches is checked too.
  Exit status 1 means the install failed, a package failed to import, or a module
  missed a workspace project's module; 0 means none of that, even if some projects are behind.
- **Not imported, missing what no workspace project ships**: a module below a package that
  imported, missing something no workspace project ships — most often an optional extra's
  dependency, which the empty venv does not install. It is listed and does not change the exit
  status. Only a `ModuleNotFoundError` is listed here: any other error of such a module (say a
  `TypeError` because PyPI's release of a dependency lacks a keyword) is a break once a project
  is behind, printed with that project's name in parentheses and under "Release first".
- **`--no-check`**: both checks are skipped, and the report puts two lines in their place:
  ```text
  It takes 4 workspace projects from PyPI, not compared (--no-check): confluid 0.4.1, liquifai 0.3.1, loggair 0.2.1, recordstream 0.1.0a3

  Not checked (--no-check): navigaitor was not installed in an empty venv, so nothing says these wheels import.
  ```
  The JSON report says `"checked": false`.

What `--include-published` printed for navigaitor on 2026-10-19 (16 s; exit 0):

```text
navigaitor needs 4 workspace projects; 4 are built from this checkout into navigaitor/wheels (749 kB):
  confluid      0.4.1    dev/main b313e71  on PyPI 0.4.1
  liquifai      0.3.1    dev/main e2e0e07  on PyPI 0.3.1
  loggair       0.2.1    dev/main 5dcb106  on PyPI 0.2.1
  recordstream  0.1.0a3  dev/main 4ba5477  on PyPI 0.1.0a3

It takes no workspace project from PyPI.

Installed navigaitor in an empty venv (its wheels + PyPI) and imported every module of its 5 packages:
  146 modules imported

Not imported, missing what no workspace project ships (an optional extra's?):
  navigaitor.server         ModuleNotFoundError: No module named 'sonair'
  navigaitor.visualisation  ModuleNotFoundError: No module named 'matplotlib'
  recordstream.keras        ModuleNotFoundError: No module named 'keras'
  recordstream.loaders      ImportError: recordstream.loaders needs torch (it declares DataLoader slots) — install `recordstream[torch]`.
  recordstream.ops.torch    ModuleNotFoundError: No module named 'torch'

PyPI is up to date for navigaitor.
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

From the project's own folder (the folder's `README.md` says the same). With `--include-project`,
`wheels.yaml` lists the project's own wheel last, `install: true`: the script installs it in place of
`-e <project>`, and from any folder; set that `install:` to false to install the folder, editable,
again. `--no-project` leaves out both.

```bash
python wheels/install.py                 # the wheels marked install: true, then the project, editable
python wheels/install.py --all           # every wheel listed
python wheels/install.py --needed        # the wheels the project needs
python wheels/install.py --only confluid,recordstream
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
| `--force` builds such a project from its files as they are instead — the files git tracks and the new ones it does not ignore, so an ignored `build/lib` still stays out; its commit reads `<sha>+uncommitted` in the report, `wheels.yaml` and the README, and the report lists its changed files | You ship work in progress knowingly, and the wheels say which ones no commit reproduces |
| `--force` with `--commit` is refused before anything is built | A committed wheel must be one its commit reproduces |
| `--no-check` builds and writes the folder as a checked run does, then compares nothing with PyPI and installs nothing in an empty venv; the report says so, and the exit status is 0 | A rebuild you do not need checked — the comparison downloads PyPI's wheels and the install needs the network — runs in the time the builds take |
| `--no-check` with `--commit` is refused before anything is built | The folder is committed only once the install check passed |
| An unreadable `wheels.yaml` stops the run at its line, before anything is built | Rewriting it would drop the `install:` values it holds |
| A needed project that is not on PyPI and has no checkout here is refused | There is nothing to build it from; the message names `aisland workspace configure source set <project> local` |
| "On PyPI" counts pre-releases | pip installs `recordstream 0.1.0a2` when no final release exists — unlike `aisland jenkins scaffold`, which asks about final releases only |
| A PyPI name owned by someone else is reported | pip takes the higher version of the two; the report says when that is theirs |
| Every environment marker is followed | The wheels serve another machine, whose platform is not this one's |
| The old `*.whl` in `wheels/` go, the folder's other files stay | |
| A zip of an unchanged folder is the same file | Every member is written in name order with one fixed time |
| An unchanged project rebuilds byte for byte | Its wheel carries its commit's time, so git sees no change |
| The empty venv uses the project's own `uv.toml` when it has one | A private index it needs; the workspace's root `uv.toml` is never read |

A rebuild keeps each wheel's version (an unreleased project stays at `0.1.0`), so an
environment that already has the wheels takes new ones with `python wheels/install.py --reinstall`.

#### Git: the folder is ignored unless you ask to commit it

Wheels are heavy, so every run writes `wheels/.gitignore` holding `*`: git, `git status` and a
`/commit-all` never see the folder. `--commit` is the one way in: the run writes `*.zip` there
instead (the zip is never committed) and, once the empty-venv check passed, commits `wheels/` in the
project itself — that folder only, whatever else is changed or staged:

```text
Committed navigaitor/wheels: <short SHA>    (git log: chore: wheels built with aisland wheels build navigaitor --all)
Not committed: the install check failed     (the check failed: nothing is committed)
Nothing new to commit in navigaitor/wheels  (a rebuild of unchanged commits)
```

A run without `--commit` that finds files of `wheels/` still tracked says so, with the command
that stops tracking them (the files stay on disk):

```text
wheels/ is tracked in navigaitor (10 files); stop tracking it with: git -C navigaitor rm -r --cached wheels
```

#### One zip to carry: `aisland wheels zip` and `aisland wheels install`

```bash
aisland wheels zip navigaitor            # → navigaitor/wheels/navigaitor-wheels.zip
aisland wheels build navigaitor --zip    # the same, at the end of a build
```

The zip holds the folder (the wheels, `wheels.yaml`, `install.py`) and a `__main__.py`, so Python
runs it. On the other machine — which has no aisland — from the project's folder:

```bash
python ~/Downloads/navigaitor-wheels.zip              # unpack into ./wheels, then run wheels/install.py
python ~/Downloads/navigaitor-wheels.zip --dry-run    # every other argument goes to install.py
python navigaitor-wheels.zip --into ../navigaitor --unpack-only
```

A zip built with `--include-project` carries the project's own wheel, so it also runs in a folder
that holds no project — any folder without a `pyproject.toml` — and unpacks into its `./wheels`
(`--into` names a folder, made when missing). There it refuses a `wheels/` holding another
project's wheels. The report and `aisland wheels zip` say which kind they wrote:

```text
Zip: navigaitor/wheels/navigaitor-wheels.zip (…) — install it from any folder with: python navigaitor-wheels.zip
```

The `*.whl` already in `wheels/` are replaced; other files there stay. A folder whose
`pyproject.toml` names another project is refused before anything is unpacked:

```text
navigaitor-wheels.zip: run it from the navigaitor folder, or name it with --into (…/pyproject.toml names annotaide)
```

On a workspace machine the same zip installs into another environment with aisland
(`aisland wheels --install ZIP --python PYTHON [--into DIR]` is the short form). The
workspace's own `.venv` is refused: install.py installs with pip, and the workspace installs its
projects through `aisland setup`.

```bash
aisland wheels install navigaitor-wheels.zip --python ~/envs/navigaitor/bin/python --into ../navigaitor
aisland wheels install navigaitor-wheels.zip --python ~/envs/navigaitor/bin/python -- --dry-run
```

Why it is built
this way: [`docs/architecture.md`](docs/architecture.md) → "A project installed outside the
workspace gets its unpublished dependencies as wheels".

### Reaching a remote machine (`aisland tunnel`)

A VS Code Remote Tunnel to a box published only through an HTTPS ingress — your editor or a plain
terminal on it, with nothing exposed and no sshd:

```bash
aisland tunnel start                  # on the remote machine
aisland tunnel open <workspace>       # your editor, from your machine
aisland tunnel connect <workspace>    # a terminal, from your machine
```

Signing in, a box that cannot download the VS Code CLI, remembered workspaces, and what `connect`
needs on your machine: [docs/tunnel.md](https://github.com/Gearlux/aisland/blob/main/docs/tunnel.md).

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

At 09:45 a scheduled task of the Claude app, `acme-nightly-check`, reads that report and moves
tests between the fast and slow runs by the workspace rule; its edits stay uncommitted for
review. It runs while the app is open, or at its next launch.

**A long stage has a time limit.** Each stage of `Jenkinsfile.local` that runs the project's own
code is held to a limit. Past it the stage fails, the build fails, and the orchestrator moves on to
the next project, so one hung test cannot hold the nightly run (on 2026-10-15 one did, from 01:00
into the afternoon). Initialize and the quality gates run tools, not the project's code, and have
none. Waiting for an executor is not counted: the node is held before the first stage.

| stage | limit | longest healthy run (01:00 runs of 2026-10-10 to 2026-10-15) |
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

The limits are one table, `STAGE_LIMITS` in `aisland/services/jenkins.py`, the same for every
project. When a project's healthy run approaches its limit, raise the number there, regenerate
with `aisland jenkins scaffold --force`, and update each `<project>.local` job's configuration
(post its `config.xml`, or re-seed with `aisland jenkins init --local`). Abort clicked before the
limit stays an abort. The process the stage was waiting on may outlive it: Jenkins gives it 20 s
and goes on without it. A pytest killed this way writes no report, and each pytest step removes
the previous build's report and coverage files before it runs, so Jenkins never records
yesterday's tests for a build that ran out of time. The push pipelines (`Jenkinsfile`, `ci.yml`)
carry no limit; a GitHub job is bounded at six hours by GitHub.

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
| `LABELSTUDIO_TOKEN` | generated from workspace name | Legacy API token for programmatic clients (annotaide, a pipeline) |
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
`config.toml` — which frameworks to install and which tracking backends (where each project's package comes from
is in [`workspace.toml`](#project-sources-aisland-workspace-configure-source)):

```toml
frameworks = ["torch", "lightning", "keras"]
trackers   = ["mlflow", "tensorboard", "aim"]

[mirrors.navigaitor]
url = "git@github.com:Org/navigaitor.git"
```

You never edit it by hand: `aisland framework set` / `aisland tracker set` / `aisland mirror set` each rewrite
their own section and leave the others alone. Two commands exist for the file
itself:

```bash
aisland config show      # print it
aisland config migrate   # fold a pre-2026-08 sources/frameworks/trackers.toml into it
```

**Per-machine deviations never land here.** They go to `.git/config` (`aisland.frameworks`,
`aisland.trackers`, `aisland.mirror.<project>`), because this file is tracked and drives every
generated CI pipeline — one developer's local narrowing must not decide what a runner installs.

> The workspace-root **`uv.toml` is a different file and stays separate.** uv reads only a file
> of its own settings and rejects anything else in it, and that file is *generated* per machine
> from the activated project set (a private index reaches it only when its project is active),
> so it is gitignored rather than committed. See [Optional submodules & the generated
> `uv.toml`](#optional-submodules--the-generated-uvtoml).

## Project Sources (`aisland workspace configure source`)

Each workspace project can be consumed either as a **local checkout** (developed here, installed
editable) or as a **published wheel** (not checked out at all). `aisland workspace configure source` owns that choice
and drives every artifact that has to agree with it — the checkout, the shared `.venv` and the
`*.code-workspace` folder list. It never touches a CI file: a runner installs what is **released**,
whatever you checked out (see [What CI does with all this](#what-ci-does-with-all-this)).

There are **three kinds**. `local` and `github` both take a `--branch` (a branch or tag; without one, the project's
own branch in `workspace.toml`) and differ only in whether a working tree survives; `pypi` takes a `--version`
instead:

| source | directory | `.venv` | `*.code-workspace` |
|---|---|---|---|
| `local` | checked out, `git switch <branch>` | `uv pip install -e loggair[dev,…]` | listed |
| `github` | **deinit'd — empty** | `uv pip install git+…@<branch>` | **not listed** |
| `pypi` | **deinit'd — empty** | `uv pip install -U loggair` | **not listed** |

The `local` install target is the one `aisland setup` computes for that checkout — its `dev` extra
plus every framework/tracker extra this machine selected (`sonair[dev,lightning]`) — so switching a
project to `local` never drops a test dependency or a training backend from the venv.

**CI never sees your source or your `--branch`.** They describe this machine; the CI files are
tracked and shared — see [What CI does with all this](#what-ci-does-with-all-this).

Pick `local` when you edit the project here, `github` when you want a specific branch or tag but
no working tree, `pypi` when a released wheel will do. The names are deliberately far apart:
these commands are one word apart and two of them run `git submodule deinit`.

A project with **no entry** is *unmanaged*: nothing is switched, nothing is installed
differently, and CI keeps the historical `@main`. Setting the first source is the only thing that
changes behaviour.

```bash
aisland workspace configure source                                         # what is set, and what is on disk
aisland workspace configure source set loggair local  --branch dev/main    # develop it here, on dev/main
aisland workspace configure source set loggair github --branch dev/main    # pin to that branch, no checkout
aisland workspace configure source set loggair pypi                        # consume the published wheel
aisland workspace configure source set loggair pypi --machine-only         # this machine only; nothing committed
aisland workspace configure source unset loggair                           # back to unmanaged
```

`aisland setup` applies the sources on every run.

```text
$ aisland workspace configure source
  PROJECT       COMMITTED        OVERRIDE  EFFECTIVE        STATE
  confluid      pypi             -         pypi             empty
  loggair       github:dev/main  -         github:dev/main  empty
  recordstream  local:dev/main   -         local:dev/main   on dev/main
```

A setting this machine still has in an older spelling (`aisland.source.loggair = pypi`) is no longer read; the
listing names it, with the command that sets it again.

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

- **`aisland workspace configure source set` / `unset` regenerate nothing.** A source and a ref are development
  choices ("I'm working on liquifai here, on `dev/main`"); letting them decide what a runner builds
  would publish a personal choice to every consumer's CI *and* rewrite the committed files of every
  dependent on each switch. Releasing a dependency is what changes a CI file — on the next
  `aisland jenkins scaffold`.
- **`Jenkinsfile.local` installs nothing and reads no configuration.** Every source decision was
  already applied to your `.venv` by `aisland setup`, so the local pipeline just runs the quality
  gates and tests of the checkout against that venv — no `aisland workspace configure source` change of any kind
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

**Which layer drives what is load-bearing.** Every downstream artifact except the venv, the
checkout and the generated workspace file is itself a committed file, so a machine-local choice
must never reach one:

- **committed source only** → the published/not bit that `Jenkinsfile` and `ci.yml` read. A
  `--machine-only` change does not touch it.
- **effective source** (override, else committed) → submodule init/deinit + branch switch, the
  editable/git/PyPI install into the shared `.venv` (the editable one with the same `[dev,…]` extras
  `aisland setup` installs), and — through the checkout — the folder list of the generated
  `*.code-workspace` file.
- **neither** → `Jenkinsfile.local`, which installs nothing and tests the venv as setup left it.

### The source settings in `workspace.toml`

Written by `aisland workspace configure source set` into the project's own table; nothing is written that the table already says:

```toml
[project."loggair"]
url = "git@github.com:Gearlux/loggair.git"
branch = "dev/main"          # the project's branch: what local checks out and github installs from
source = "github"            # "local" | "github" | "pypi"
# version = ">=0.1.0"        # pypi only; omitted => latest
# distribution = "loggair-x" # only when the package name differs from the project's
```

This machine's own choice goes to `.git/config`, one key each: `aisland.source.loggair.source`, `.branch`,
`.version` — a missing `.branch` is the project's. A `config.toml` that still has `[sources.*]` tables stops
`aisland setup`, naming the line to move into `workspace.toml`.

### Notes

- Both detaching kinds are verified **before** anything is emptied, because otherwise the failure
  would land after the directory was already gone:
  - `pypi` — the name must exist on PyPI **and** its published metadata must point at
    `github.com/Gearlux/…`. PyPI names are global, so a bare existence check would let a workspace
    project whose name is taken by someone else install that stranger's package into the shared
    venv.
  - `github` — the `--branch` must resolve on the project's remote (`git ls-remote`).

  An unreachable index or remote warns and continues rather than blocking an offline machine.
- `git switch` and `git submodule deinit` are never forced: a submodule with uncommitted work
  refuses, is reported, and neither the venv nor the registry is changed — a failed apply rolls
  the configuration back so the registry never claims a state the tree does not have.
- `aisland workspace configure source set` / `unset` never regenerate a CI file — see
  [What CI does with all this](#what-ci-does-with-all-this).
- `aisland setup` applies the configuration on every run, so a fresh clone converges without any
  manual step.

## Project Mirrors (`aisland mirror`)

A submodule can be kept on a second host — a GitHub repository and a Bitbucket one, say. The
remote `.gitmodules` names stays the **primary**: you clone, pull and push it exactly as before.
The **mirror** only receives a copy.

```bash
aisland mirror set navigaitor git@github.com:Org/navigaitor.git
```
```
==> navigaitor: mirror → git@github.com:Org/navigaitor.git
    recorded in config.toml (commit it); remote `mirror` added to navigaitor
    Note: each sync copies navigaitor's dev/main, main and tags from bitbucket.example.com to github.com — make sure its code may live there.
    Push to it with: aisland mirror sync navigaitor
```

`aisland mirror sync` pushes the primary's `dev/main`, `main` and tags to every mirror (or to
the one project named). `/push-all` and `/ship` run it as their last step, so after the first
`set` you rarely type it.

```
$ aisland mirror sync
==> navigaitor → git@github.com:Org/navigaitor.git
    dev/main  pushed (3 commits)
    main      in sync
    tags      pushed v0.4.0, v0.4.1
```

| command | effect |
| --- | --- |
| `aisland mirror list` | each mirror, its committed URL, this machine's override, and what a sync would push (`dev/main 3 behind · main in sync · 2 tags missing`) — pushes nothing |
| `aisland mirror set <project> <url>` | record the mirror in `config.toml` and add the remote `mirror` to the checkout |
| `aisland mirror set <project> <url> --machine-only` | this machine only (`.git/config aisland.mirror.<project>`); nothing committed |
| `aisland mirror unset <project> [--machine-only]` | drop the mirror (or only the override) and the remote aisland added; the mirror host is not touched |
| `aisland mirror sync [<project>]` | push; exit 1 when anything was refused or a host could not be reached |

**What a sync never does.** It never forces and never deletes:

- a branch the mirror has commits on that the primary does not (someone pushed to the mirror) is
  **refused**, naming the commit and the merge that brings it to the primary:
  ```
  ==> vendorcli → git@github.com:Org/vendorcli.git
      dev/main  REFUSED: the mirror has 1 commit the primary does not (a1b2c3d "fix: …")
                merge it first: git -C vendorcli fetch mirror && git -C vendorcli merge mirror/dev/main
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
- `aisland mirror set` refuses a project whose checkout already has a remote named `mirror` that
  aisland did not create, and prints the `git remote rename` that frees the name.
- `aisland setup` adds the remote for every committed mirror and removes the one aisland added
  for a mirror unset on another machine. Why the mirror is push-only: [docs/architecture.md](docs/architecture.md),
  "A mirror is push-only, fast-forward-only, and copies the primary".

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
> plain hard dependency on purpose: `deltaid` (a PyTorch-Lightning segmenter), `raidar` and `raidar-ultralytics` (torch
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
    liquifai apps: annotaide, curaitor, raidar, recordstream, sonair, wf, ...
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
{"mcpServers": {"vendorcli": {"command": "/…/bin/bash",
                           "args": ["-c", "source \"$0\" >/dev/null; exec \"$@\"",
                                    "/…/project.bashrc", "/…/.venv/bin/vendorcli-mcp"]},
                "...": {}}}
```

A client starts a server with its own environment, not the workspace's. So each server is
started through bash, which first sources the workspace's `project.bashrc` — the venv,
`PROJECT_ROOT` and every `.env` setting, as in a terminal — with its output discarded (the
server's stdout is its protocol stream), then becomes the server. `.env`'s values are never
written into the client file. Without a `project.bashrc` (or without bash) a server is
started directly, `{"command": "/…/.venv/bin/vendorcli-mcp"}`. A change to `.env` reaches a
server when the client restarts it.

Claude Code reads `.mcp.json` when a session starts in the workspace and asks once whether
to use the project's servers (`enableAllProjectMcpServers` in its settings skips the prompt);
`claude mcp list` then shows them. A server whose annotaide, say, is not running still starts
— its tools answer in-band with the command that starts the service.

### aisland's own server (`aisland-mcp`)

aisland ships an MCP server too, so an AI client can check and drive this workspace. It needs
the `mcp` extra; once installed, `aisland setup` (or `aisland mcp claude`) lists it as
`aisland` in `.mcp.json`:

```bash
pip install 'aisland[mcp]'
```

| tool | what it does |
| --- | --- |
| `doctor` | `aisland doctor`'s checks: each one's result and the command that fixes it |
| `tests_report(project, days)` | `aisland tests report PROJECT --json` |
| `sources` | `aisland workspace configure source`'s rows |
| `services_status` | every supervised service: running, PID, ports |
| `tunnel_list` | `aisland tunnel list --json` |
| `editor_themes`, `editor_colors(part)` | the VS Code themes here, the CSS colour names, this window's choice |
| `editor_set_theme(name)`, `editor_set_color(role, color, area)`, `editor_unset(what)` | `aisland workspace theme\|color set\|unset` |
| `service_start(name)`, `service_stop(name)`, `service_restart(name)` | `aisland jenkins\|mlflow start\|stop\|restart`, in the background |

Every tool answers with one JSON object, and a refusal is the tool's error in the command's own
words (`no installed VS Code theme is named 'Solarzed Dark' (did you mean 'Solarized Dark'?)`).
The first seven only read; the others change this machine and are marked as such for the
client. What prompts, runs for minutes or deletes — `setup`, `clean`, `backups prune`, the
`reset` commands, `jenkins init`, `source set` — is not a tool: run it yourself. Without the
extra, `aisland-mcp` stops with `Error: aisland-mcp needs the mcp extra: pip install 'aisland[mcp]'`.

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

The aliases below (`pull-all`, `push-all`, `switch-all`, `merge-all`, `tag-all`, `status-all`, `safe`) are installed by `aisland/aisland/git/setup_git.sh` into the **local** config of the root repository *and* every active submodule. They assume the acme mono-repo layout and toolchain, so they are deliberately not written to the global config — that would leak workspace-specific behavior into unrelated repositories (the script also removes any global copies left by earlier versions of itself). The definitions live in `setup_git.sh` (`pull-all` calls [`git_pull_all.sh`](aisland/git/git_pull_all.sh) beside it); to (re)install them — including after activating a new submodule — run:

```bash
bash aisland/aisland/git/setup_git.sh
```

### Pulling Everything (`pull-all`)

`git pull-all` brings the whole workspace up to date with origin: every active submodule, then the root, on the root's branch. Type it in the root or inside any submodule — it always pulls the whole workspace.

```bash
git pull-all                # merge what came in; list what needs you
git pull-all --autostash    # also set aside uncommitted edits that are in the way, and put them back
```

A plain `git pull` (even with `submodule.recurse`) only moves each submodule to the commit the root *records*; a commit pushed to a submodule that nobody recorded in the root is never pulled. And when two machines both commit in a submodule and record it, the root's merge stops on the pointer even though the submodule merges cleanly:

```
CONFLICT (submodule): Merge conflict in navigaitor
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
navigaitor             merged origin/dev/main (1 commit)
root                   pointer conflict navigaitor → 6cb3d7f (holds both sides)
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

A root with no `origin` remote — the one `aisland workspace init` makes — has nothing to pull: it is neither fetched nor merged, and each submodule is pulled on the branch it is on, whatever the root's branch. What came in is still recorded in the root:

```text
$ git pull-all
fetching 2 submodules (the root has no origin remote)…
alpha                  merged origin/dev/feature (2 commits)
beta                   merged origin/dev/main (1 commit)
root                   recorded submodule pointers: alpha, beta
```

The merges are explicit (`git fetch` + `git merge`, never `git pull`), so the result does not depend on a machine's `pull.rebase` setting. The logic lives in [`git_pull_all.sh`](aisland/git/git_pull_all.sh) (why it is shaped this way: [docs/architecture.md](docs/architecture.md), "`pull-all` settles a submodule pointer only to a commit holding both sides"). In Claude Code, `/pull-all` runs it and finishes the listed conflicts for you; `/push-all` runs `/pull-all`, pushes, and ends with `aisland mirror sync` (see [Project Mirrors](#project-mirrors-aisland-mirror)).

### Pushing Everything (`push-all`)

`git push-all` pushes the current branch of every submodule, then the root's — the root only when every submodule push succeeded, so the root on origin never records a submodule commit origin does not have. A root with no `origin` remote is skipped, ending with `root: no origin remote — not pushed`.

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
