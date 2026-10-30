# A workspace and its setup

This page covers how a workspace is created and set up: the bootstrap, `aisland workspace init`, tracking of projects,
`workspace.toml`, what projects declare, system tools, workspaces without `.gitmodules`, and optional projects.

## Create a workspace (`aisland workspace init`)

[`examples/02_workspace_init.py`](../examples/02_workspace_init.py) runs `init` in a temporary folder and checks what
it writes: the starting files, aisland's agent rules and the git aliases in the local config.

Make a workspace, give it a venv holding aisland, and set it up:

```bash
aisland workspace init ~/work/acme --add git@github.com:acme/models.git
cd ~/work/acme
uv venv --seed .venv && .venv/bin/pip install aisland
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
    .agents/                  aisland's commands and agent rules (`.claude/commands` links to them)
    AGENTS.md                 written (imports the rules; CLAUDE.md and GEMINI.md link to it)
Next:
    cd /Users/you/work/acme
    uv venv --seed .venv && .venv/bin/pip install aisland
    .venv/bin/aisland setup
```

`init` never writes over a file that exists. In a folder that already is a git repository it sets
that one up instead: no `git init`, the `--add` projects added, the missing starting files written, the
aliases and the agent commands installed (`--from` and `--local` are refused there). Every file it writes is
a starting point you own. A workspace is a folder whose projects are subfolders holding a `pyproject.toml` —
submodules listed in `.gitmodules`, or, without one, every such subfolder (next section);
`aisland setup` needs the `.venv` and reads `workspace.toml`, `.env` and `mypy.ini` when they exist.

To take a newer template of a starting file, name it with `--rewrite` (repeat for several). In an
existing repository that is all `init` does; the old copy is kept as `<file>.bak`, and the run is
refused when that `.bak` already exists:

```
$ aisland workspace init . --rewrite vscode-init-bash.sh
==> Rewrote in /Users/you/work/acme
    vscode-init-bash.sh       rewritten (the old one kept as vscode-init-bash.sh.bak)
```

A wheel built from a checkout of aisland installs the same way: `python -m pip wheel --no-deps -w dist <checkout>`
writes `dist/aisland-<version>-py3-none-any.whl`, and `.venv/bin/pip install <that file>` takes the place of
`pip install aisland`.

### Developing aisland itself

In a workspace that holds aisland's own checkout, `bash aisland/setup.sh` (run from the workspace root) provisions the
`.venv`, installs aisland in editable mode, hands off to `aisland setup` and installs shell completion into the
workspace-local `.project.bashrc.completion` (sourced by `project.bashrc`, gitignored). From a checkout, aisland works
on the workspace it is checked out in, wherever you run it.

### A root no one shares (`--local`), projects it does not record (`--untracked`)

Two choices of the root's, each its own flag:

- `--local` makes a new root repository that only this machine has. It tracks `.gitmodules` and the projects and
  nothing else (its `.gitignore` is `/*`); `.gitmodules` and the projects are its first commit.
- `--untracked` makes the root record none of its projects' commits: `git status` in the root never shows a
  project's new commits and `git pull-all` never records them. It works in a new root, a `--local` one or an
  existing repository.

```text
$ aisland workspace init ~/work/acme --local --untracked --add git@github.com:acme/models.git
==> Created workspace acme in /Users/you/work/acme
    .git/                     git init (local: it tracks only the projects)
    models/                   submodule git@github.com:acme/models.git
    ...
    models                    untracked: the root records none of its commits
    first commit              .gitmodules, models
$ cd ~/work/acme && git status --short
$
```

In a `--local` root, add a project later with `git submodule add -f <url>` (git refuses the ignored path without
`-f`), then run `aisland setup`: it commits the project with `.gitmodules`.

### A repository cloned into the workspace by hand

`aisland setup` adds each clone in the workspace folder that `.gitmodules` does not list — a folder that is its own
repository, with an `origin` and a `pyproject.toml` — as a submodule on the branch it is on, without asking. git keeps
the clone as it is, uncommitted edits included:

```text
$ .venv/bin/aisland setup
  - added foo as a submodule (https://host/team/foo.git, dev/main)
  - foo: untracked (the root records none of its commits)
```

It is tracked as the workspace says: untracked when `workspace.toml` has `[workspace] track = false` — written by
`init --untracked` and by `aisland workspace untrack` with no project named — or this machine untracks every project,
tracked otherwise. A `--local` root commits it with `.gitmodules`; in a shared root they are left for you to commit.
So in a `--local --untracked` workspace, a project you clone into the folder needs only `aisland setup`: it becomes an
untracked submodule, listed in `workspace.toml` and in the editor's workspace file, and committed.

### Track or untrack the projects: `aisland workspace track|untrack`

```bash
aisland workspace untrack                      # every project: the root records none of their commits
aisland workspace untrack models               # one project
aisland workspace track models                 # record its commits again
aisland workspace untrack --machine-only       # this machine only; nothing to commit
```

```text
$ aisland workspace untrack models
==> untracked: models (in workspace.toml and .gitmodules — commit them)
  - models: untracked (the root records none of its commits)
```

The choice is written for everyone as `track = false` in the project's table of `workspace.toml` and `ignore = all`
in `.gitmodules` (a `--local` root commits it itself), or with `--machine-only` in `.git/config` alone, where it wins
over the committed one; `aisland setup` writes git's setting back from them. An untracked project is skipped by
`git add <project>` and by the `/commit-all` agent command ([harness](harness.md)); the root keeps the commit it last recorded, so a `git submodule update`
typed in the root would put the project back there, off its branch — `git pull-all` and `aisland setup` never do.
`track` shows a project that moved meanwhile again (` M models`), and the next `git pull-all` records it once origin
has those commits.

An untracked project follows its **branch**, not the commit the root recorded. A fresh clone's `git submodule update`
leaves every project on that old commit and on no branch; `aisland setup`, `aisland workspace untrack` and
`git pull-all` then fetch it and put it on its branch — `.gitmodules`' `branch` (`.` is the root's branch), else
origin's default — at origin's newest commit (a local branch with commits of its own is switched to, then merged by
`git pull-all`). So on a new machine `git clone --recurse-submodules <root>` and `aisland setup` put every untracked
project on its branch:

```text
$ git pull-all
fetching the root and 2 submodules…
models                 on dev/main (it was on no branch)
models                 merged origin/dev/main (3 commits)
```

A switch is never forced: uncommitted edits in its way leave the project as it is, listed as
`on no branch, and git switch dev/main refused: …`. A tracked project on no branch is still left alone.

### The machine's environment: `env.complete`, `env.example` and `.env`

Each project lists every environment variable a person may set for it in its `env.complete`, every line commented
out, and the few a person normally sets in its `env.example`, in the same format:

```bash
# models: where the models find their data

# -- paths
# DATA_ROOT=/path/to/datasets   the folder every dataset config reads (README → Data)
# MODELS_CACHE=   a cache folder; empty: none
# not for .env: COMP_LINE COMP_POINT — set by the shell while it completes
```

A variable line is `# NAME=VALUE`, two spaces, then what it does; a secret's value stays empty. `# -- <heading>`
groups the lines below it, and `# not for .env: NAME … — reason` names what the project reads that nobody sets in
`.env` (set by the shell, by CI, by the project for its own child processes).

`aisland workspace env-example` writes the workspace's `env.complete` and `env.example` from them — `aisland setup`
writes them too — a section per project, each library before the projects that use it. A variable several projects
read is listed once, under the project that defines it, the others named and their own wording kept:

```
$ aisland workspace env-example
==> Wrote env.complete (12 variables from 4 projects)
    no env.complete: tools
==> Wrote env.example (3 variables from 2 projects)
    no env.example: app, tools
$ cp env.example .env        # a new machine: then uncomment and set what it needs
```

```bash
# ===== models: where the models find their data

# -- paths
# DATA_ROOT=/path/to/datasets   the folder every dataset config reads (README → Data)
#     also read by: app
#     app: the datasets the app opens
# MODELS_CACHE=   a cache folder; empty: none
```

Both files are per machine (they follow the projects checked out here) and ignored by git. `aisland workspace init`
starts a new `.env` from `env.example` (from `env.complete` while no project has an `env.example`); an existing
`.env` is never touched. To see which variables of `env.complete` an existing one lacks:

```
$ aisland workspace env-example --missing
==> Up to date: env.complete (12 variables from 4 projects)
==> Up to date: env.example (3 variables from 2 projects)
==> Not in .env (1) — copy the lines you need:
# -- models
# MODELS_CACHE=   a cache folder; empty: none
```

A name `.env` mentions at all, set or commented out, counts as mentioned.

`aisland workspace env-test [PROJECT…]` writes each project's `tests/test_env_example.py` (no project: every one
with an `env.complete`). The test reads the project's code and its shipped configs (`config/`, `examples/`,
`graphs/`, their comments left out) and fails, naming the file and line, on a variable its `env.complete` does not
list, on an `env.example` variable `env.complete` lacks, and on a line of either file that would set something. It
imports nothing of aisland. A name the code builds at run time is not seen: list it by hand.

### The same workspace on another machine: `workspace.toml`

Every workspace keeps one committed file of its projects, `workspace.toml`, shaped like `.gitmodules`.
`aisland setup` keeps it current — a new submodule is added, a changed url or folder followed — and its first lines
say how to start on a machine that has no aisland yet:

```toml
# workspace.toml — the acme workspace: its projects and its choices, committed.
# ...
#   1. A virtual environment with pip and aisland in it. ...
#        uv venv --seed --prompt acme .venv
#        uv pip install --python .venv/bin/python aisland
#      --seed puts pip in it: `aisland setup` installs uv into the venv with that pip and stops
#      without it. A venv made without --seed gets it with:
#        uv pip install --python .venv/bin/python pip
#   2. ...
#        .venv/bin/aisland workspace init . --from workspace.toml
#   3. ...
#        .venv/bin/aisland setup
[workspace]
format = 1

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

`format` says which shape of the file this is. A file without it is read as format 1; one written by a newer aisland
is refused with the upgrade to make:

```text
Error: workspace.toml:2: workspace.toml is format 2, written by a newer aisland; this one reads format 1. Upgrade it: pip install --upgrade aisland
```

**A key aisland does not read is named.** A project's table holds `path`, `url`, `branch` and the settings of
`aisland workspace configure` (`source`, `version`, `distribution`, `mirror`, `active`) and of `track` and
`test-stats`; `[workspace]` holds `format`, `name`, `track`, `test-stats` and the selections. Any other key — a typo,
most often — is kept as written and does nothing, so `aisland setup`, `aisland workspace show` and
`aisland workspace configure` name it with its line and the key it is closest to, and go on:

```text
Warning: workspace.toml:12: project 'models': sorce is no setting aisland reads (did you mean source?)
```

**Add a project by writing it in.** A project the file lists with a `url` that the workspace does not have yet is
cloned by the next `aisland setup`, as a submodule on its `branch`:

```text
$ cat >> workspace.toml
[project."lib"]
url = "git@github.com:acme/lib.git"
branch = "main"
$ .venv/bin/aisland setup
  - workspace.toml: lib is not in the workspace yet; setup adds it
  ...
  - add lib (active)
```

So removing a project means removing its section too: a submodule removed with `git rm` while its section stays is
cloned back by the next setup.

Take the file to the new machine and follow its three steps in the folder that will hold the workspace.
`init --from` clones each project as a submodule into its `path` on its `branch`, before any `--add`, and
the file becomes the new workspace's own. A file aisland cannot read is refused before anything is
written, naming the line:

```text
$ .venv/bin/aisland workspace init . --from workspace.toml
Error: workspace.toml:7: project 'tools' has no url
```

A project that cannot be cloned (a wrong url, no access) stops `init` and removes the folder it made, so the same
command runs again once the cause is fixed. A folder that held files before is never removed: the error says what
init left in it.

```text
$ aisland workspace init ~/work/acme --from workspace.toml
Error: [2/2] tools: could not add https://github.com/acme/tools.git as a submodule: fatal: repository 'https://github.com/acme/tools.git/' not found
/home/you/work/acme is removed again: this init made it. Run the same command once the cause is fixed.
```

While it clones, `init` says each project as it starts and ends, on stderr — on a terminal git's own download
progress is drawn on one line below it; the report it ends with goes to stdout alone:

```text
$ .venv/bin/aisland workspace init . --from workspace.toml
==> Creating workspace in /home/you/acme
[–]   viewer  skipped (optional: aisland setup asks)
[1/2] models  cloning git@github.com:acme/models.git (dev) …
[1/2] models  done in 3.1 s
[2/2] tools   cloning git@github.com:acme/tools.git …
[2/2] tools   done in 2.8 s
      writing the starting files, workspace.toml, the git aliases and the agent rules …
==> Created workspace acme in /home/you/acme
```

aisland finds the venv at `.venv` in the workspace. To give it a name, use `--prompt` (the shell
then shows `(acme)`), or keep the venv elsewhere and link it in — `.gitignore` covers the link:

```bash
python3 -m venv ~/venvs/acme && ln -s ~/venvs/acme .venv
```

The workspace's name — the editor file `<name>.code-workspace`, the Jenkins jobs `<name>.local` and
`<name>.nightly-check`, the how-to's `--prompt` — is its origin's repository name, else its folder's. A different
one is written in the file, before the projects:

```toml
[workspace]
name = "studio"
```

Run from that `.venv`, `init` leaves the venv line out of its `Next:` steps. A branch of `.` in
`.gitmodules` (follow the superproject's) is written as the root's branch.

How aisland finds its workspace depends on how it is installed:

| installed | the workspace | where aisland keeps its state (`data/`, `bin/`, `tools/`: service data, downloaded tools) |
|---|---|---|
| a checkout of aisland, installed editable | the workspace the checkout is in, wherever you run it | `<workspace>/aisland/` |
| from a wheel | `AISLAND_WORKSPACE` when set, else the nearest folder above where you run it that holds `.gitmodules`; the folder you run it in when it holds projects; from inside a project, the project's parent; a git repository with no project yet | `<workspace>/.aisland/` |

From a wheel, outside a workspace, a command is refused before it writes anything; only
`aisland workspace …`, `aisland wheels …`, `aisland tunnel …` and `--help` run anywhere:

```text
$ aisland services
Error: No workspace here: /Users/you/Downloads is in no git repository and holds no project. Run aisland in your workspace or one of its projects, set AISLAND_WORKSPACE to it, or create one with `aisland workspace init <folder>`.
```

From a wheel, the git aliases `aisland setup` installs (`git pull-all`, …) name the scripts by
their place in the venv: after recreating the venv or installing a new aisland, run
`aisland setup` again. The editor file names the workspace's own folder
(`${workspaceFolder:acme}`, the `acme-bash` terminal profile) and takes the bash on `PATH` when
the template's is missing on the machine. aisland's own system tools (process-compose for
the services, Docker) are installed when a command first needs one ([System tools](#system-tools-aisland-workspace-provision)).

### What the projects declare (`aisland workspace show`)

A project tells aisland what it brings in its own `pyproject.toml` — nothing needs to be installed for aisland to
read it, and the project never imports aisland:

```toml
[tool.aisland.selection.framework]        # the project that asks the question defines the selection
question = "Which ML frameworks should this machine install?"
choices = ["torch", "keras"]              # each one an extra of this project; other projects add their own

[tool.aisland.service.notes]              # a service the project needs, run exactly as written
command = ["notes-server", "--port", "8123"]
port = 8123
ready = "/health"

[tool.aisland]
click-commands = ["app"]                  # scripts of [project.scripts] that are click commands: shell completion

[project.entry-points."aisland.plugins"]  # code, for what a declaration cannot say
acme-db = "acme_plugin:Database"
```

`aisland workspace show` lists what every project of the workspace declares (`--json` for the same as one object):

```text
$ aisland workspace show
Declared by the projects:
  selection  framework  models      choices: torch, keras (models), torch (app)
  service    aim        models     port 43800
  plugin     acme-db    acme       acme_plugin:Database — not installed
```

A declaration aisland cannot use stops it with one line naming the file and the line of the table:

```text
Error: /path/to/app/pyproject.toml:8: app offers 'jax' for selection 'framework', but defines no extra of that name
```

### System tools (`aisland workspace provision`)

A project declares the tools it needs from outside Python in its `pyproject.toml`; aisland has its own (the service
supervisor, Docker, Colima, `act`, SwiftBar, and Java for Jenkins):

```toml
[tool.aisland.sys-dependencies]
brew = ["node"]          # macOS, installed with Homebrew
userspace = ["node"]     # Linux, downloaded by aisland into its bin/ folder
```

```text
$ aisland workspace provision
  TOOL                           HOW   PROJECTS      INSTALLED
  act                            brew  aisland       yes
  node                           brew  viewer        no
  …
$ aisland workspace provision install      # install every one of them, aisland's own included
```

`aisland setup` installs only what the projects declare. aisland's own tool is installed when a command first needs
it — `aisland services start` the supervisor, `aisland docker start` Colima and Docker, `aisland swiftbar start`
SwiftBar, `aisland jenkins start` its WAR and Java — after the command asks; with no terminal to ask in, or on a "no",
it stops with the line that installs the tool and installs nothing. `aisland check` without `act` fails that gate with
the same line. On macOS `aisland workspace provision install` also installs the Java the Jenkins server runs on. A wrong declaration stops every command before
anything is installed, naming the file and the line:

```text
Error: /w/editor/pyproject.toml:4: sys-dependencies takes brew and userspace, not 'apt'
```

### A workspace without `.gitmodules`: a folder of projects

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

The workspace git aliases ([Git](git.md)) work over the clones: `aisland setup` installs them into every clone, and the root when it is a repository. Each clone
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
refused (`alpha is in no git repository; …`); a mirror needs a submodule's `.gitmodules`
remote and is refused.

## Optional projects & the generated `uv.toml`

A project whose table in `workspace.toml` has an `active` key is **optional**, and the value is its default; a project
without the key is always part of the workspace:

```toml
[project."tools"]
url = "https://bitbucket.example.com/scm/team/tools.git"
active = false          # optional, off unless a machine switches it on
```

`aisland workspace init --from` clones only the projects without `active`. `aisland setup` then **asks** about each
optional project this machine has not answered — `Activate 'tools'? (clone + install) [y/N]`, the default the
committed value — even under `-y` (a machine consciously opts into a private project), and keeps the answer in
`.git/config` (`aisland.active.<project>.active`), never in a committed file. It then brings the folders in line: an
active project is checked out (added from its `url` and `branch` when the workspace does not have it yet), an
inactive one emptied — never with `-f`, so a folder with uncommitted work is left and named. Setup also writes what
git reads: `.gitmodules` `active = false` for a project that is off by default, and this machine's
`submodule.<name>.active`.

```bash
aisland workspace configure active                                   # the optional projects, and this machine's choices
aisland workspace configure active set tools true --machine-only     # switch it on here (checks it out now)
aisland workspace configure active set app false              # make it optional, off by default (committed)
aisland workspace configure active unset app                  # always part of the workspace again
```

```text
$ aisland workspace configure active
  PROJECT  COMMITTED  THIS MACHINE  FOLDER
  tools    false      true          checked out
```

From the activated set, setup **generates `uv.toml` in the workspace folder**: a base that drops the GUI
`opencv-python` build (uv's `override-dependencies` with a marker that is never true) and pins headless opencv to one
range every project shares, merged with each *activated* project's own `uv.toml` (union of `extra-index-url`, etc.).
The file is **gitignored and regenerated** on every setup, so a private index (a company package index named in
`tools/uv.toml`) lands in it **only on a machine that activates that project** — a machine that skips it never tries
to contact the unreachable index.

Each activated project's `wheels/` folder (one `aisland wheels build` wrote, with its `wheels.yaml`) joins it as a
`find-links` path, relative to the file:

```toml
find-links = ["bridge/wheels"]
```

Every `uv pip install` run in the workspace — setup's, `aisland workspace configure … set`'s, your own — then finds
the wheels of what PyPI lacks without a separate step, and takes the folder's wheel over PyPI's of the same version.
A project checked out in the workspace still installs from its checkout.
