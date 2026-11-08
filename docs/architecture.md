# aisland — architecture

For contributors: what aisland is built of and why. This page is the map; each decision behind it is a record of its
own under [`decisions/`](decisions/), with the measurements and dates that led there. Usage lives in
[`../README.md`](../README.md) and the pages it indexes under `docs/`; the rules an agent working on aisland follows
live in [`../AGENTS.md`](../AGENTS.md). These cross-link and never duplicate.

## What aisland is

One Python package, `aisland`, with two front doors: the `aisland` click CLI (`aisland/cli/`) and the `aisland-mcp`
MCP server (`aisland/server.py`, the `[mcp]` extra), which wraps a chosen set of the same commands as tools. Both work
on a *workspace*: a git repository whose projects are submodules (or a plain folder of project folders), with one
shared `.venv`, a committed `workspace.toml`, and the services, CI files and editor files aisland generates for it.
A pytest plugin (`aisland.pytest_stats`, the `pytest11` entry point) records every test run in the workspace.

## Where things live

A command is a click function in `aisland/cli/` that calls the module of its subject; `setup/manager.py` runs the
steps of `aisland setup` in order, and each step is a module of its own.

| area | modules |
|---|---|
| The workspace and its file | `workspace/` (`model`, the workspace as one record; `file`, `check`, `local`, `projects`, `gitmodules`, `clones`, `discovery`, `github`), `locations`, `paths`, `pyproject`, `toml_places` (a table's line in a TOML file) |
| What a project or a machine chooses | `workspace/` (the plugins `activation`, `selection`, `sources` — the model — with `source_apply`, `mirrors`; `tracking`, `recording`), `machine_config` (this machine's `git config --local`) |
| Setting a machine up | `setup/` (`manager`, the driver, and its steps `provision`, `installers`, `bashrc`, `uv_config`, `editor_files`, `mcp_config`, `env_example`, `completion`, `playwright_browser`, `git_settings`, `harness`; `init`, `workspace init`; `configure` and `source_commands`, the configure commands), `downloads`, `editor`, `venv` (the `.venv` and its uv), `userspace_tools` |
| Services | `services/` (`base`, `registry`, `plugin`, `declared`, `supervisor`, `jenkins`, `jenkins_jobs`, `jenkins_settings`, `jenkins_java`, `docker`, `swiftbar`, `nightly`), `envfile` (the workspace's `.env`) |
| CI and tests | `ci/` (`ci_scaffold`, `ci_templates` — the sets under `templates/` —, `jenkins_job_config`, `quality`, `stats_report`), `pytest_stats` (the plugin, standard library only) |
| Wheels | `wheels/` (`build`, the run, and `projects`, `published`, `export`, `report`, `files`, `check`, `bundle`, `extra`, `scrub`, `prune` with `filter_repo`, `install`, `unzip`) |
| Reaching a remote machine | `tunnel/` (`host`, `state`, `binary`, `store`, `connect`, `shell`) |
| Diagnosis and housekeeping | `doctor`, `clean`, `backups`, `probe`, `git_exclude`, `notebook_utils`, `css_colors` |
| Shared plumbing | `gitcmd`, `downloads`, `pypi` (PyPI's JSON document and the release question), `errors`, `failures`, `console`, `atomic`, `server` (the MCP server) |

A subject of several modules is a package. Three modules stay at the top level whatever their subject:
`pytest_stats` is the `pytest11` entry point every workspace's venv has installed (moved, every pytest run there
fails to load its plugin until aisland is installed again), and `locations` and `paths` are what that plugin and the
standalone scripts read.

## The layers

The subpackages are layers, and import-linter holds them (`[tool.importlinter]` in `pyproject.toml`, run by
`tests/test_imports.py`). Five contracts:

- **`layers`** — top to bottom, each importing only those below it, the modules of one line never one another:

  ```text
  cli | server                 the front doors
  doctor | clean               the commands over the whole workspace
  backups | services
  ci
  wheels
  setup
  workspace | tunnel           the workspace model; the remote machine
  ```

- **`leaves`** — the top-level modules every layer shares (`atomic`, `console`, `downloads`, `envfile`, `errors`,
  `failures`, `gitcmd`, `locations`, `machine_config`, `paths`, `plugins`, `probe`, `pypi`, `pyproject`,
  `pytest_stats`, `venv`, …) import no layer.
- **`acyclic-inside`** — inside each subpackage the modules import one another without a cycle.
- **`stdlib-only`** — `atomic`, `locations`, `plugins`, `pytest_stats`, `tunnel.shell`, `wheels.install` and
  `wheels.unzip` import no third-party package: they run in every pytest start, or on a machine that has only Python.
- **`runs-without-aisland`** — `tunnel.shell`, `wheels.install` and `wheels.unzip` import nothing of aisland: they
  are copied to and run where aisland is not installed (the remote end of a tunnel, a wheels zip).

When a lower layer needs something from a higher one, the piece moves down; when a command drives a lower layer, the
command moves up. That is how the layering was reached on 2026-11-06, when ten imports going up a layer were all
that kept `workspace`, `setup`, `ci` and `services` in cycles.

## The gateways

Each outside thing aisland touches has one module that touches it, so behaviour cannot drift between callers:

| what | the one way | what it guarantees |
|---|---|---|
| git | `gitcmd.git(...)` | no terminal read; `network=True` never prompts for credentials and gives up on a silent remote ([0073](decisions/0073-a-git-command-that-reaches-a-remote-gives-up-on.md)) |
| `.gitmodules` | `workspace.gitmodules.read` | one parse of git's own reading |
| a project's `pyproject.toml` | `pyproject.load` / `pyproject.read` | one answer for a missing or broken file ([0070](decisions/0070-a-projects-pyprojecttoml-and-gitmodules-each-have.md)) |
| this machine's choices | `machine_config` | `git config --local` keys, read, written and forgotten the same way |
| a state file | `atomic.write_text` | the old file or the new one, never part of either |
| what aisland says and asks | `console.echo` / `confirm` / `prompt` | the terminal in the CLI, collected (and never asked) in the MCP server |
| a failure that does not end the command | `failures.report` | its line on stderr, the command goes on, the run exits 1 ([0051](decisions/0051-a-command-that-prints-an-error-line-exits-1.md)) |
| an expected error | `errors.AislandError` | one `Error:` line, exit 1; `--debug` shows the traceback ([0062](decisions/0062-one-exception-root-and-debug.md)) |
| the workspace's `.env` | `envfile.read` / `write` | the shell's dialect, as `project.bashrc` sources it ([0057](decisions/0057-env-is-read-as-the-shell-reads-it-by-one-reader.md)) |
| a status probe | `probe` | never hangs its caller: past its limit it is "no answer" |
| a download | `downloads.download` | a time limit; a pinned file checked by its sha256 |

A new call of one of these things goes through its gateway; a module that needs more than the gateway offers extends
the gateway.

## The workspace model

`workspace/model.py` is the workspace as one record. A `Workspace(root)` is a snapshot of the disk, each part read
once when first asked: its frozen `Project`s, each joining its folder and whether this checkout carries it
(`workspace.projects.workspace_folders`), its submodule (`workspace.gitmodules`), its row of `workspace.toml`
(`workspace.file`) and its `pyproject.toml`'s metadata (`workspace.projects.ProjectMetadata`), plus the install order.
Build one where it is used and never keep one across a step that changes the workspace: reading again costs a few
milliseconds, a stale answer a wrong command.

What is committed lives in `workspace.toml` — a project's settings in its table, a selection's choice under
`[workspace]` — and what one machine deviates in `.git/config` (`machine_config`). A plugin of
`aisland workspace configure` (`source`, `mirror`, `active`, `extras`, the selections the projects declare) is one set
of keys in both layers ([0035](decisions/0035-aisland-workspace-configure-one-set-of-commands-built.md)). A project
tells aisland what it brings in its own `pyproject.toml` (`[tool.aisland]`) or an `aisland.plugins` entry point, and
never imports aisland beyond `aisland.plugins` ([0033](decisions/0033-a-project-declares-what-it-brings-aisland-finds.md)).

## Where state lives

`locations.py` decides it, and a checkout and a wheel each have their own rule
([0027](decisions/0027-from-a-checkout-aisland-finds-everything-from-its-own.md),
[0076](decisions/0076-a-checkout-of-aisland-is-one-only-where-a-workspace.md)):

- **From a checkout** that a workspace lists, every path follows the package's own place: the workspace is the one
  that lists the checkout, and the state (`data/`, `bin/`, `tools/`) lives in the checkout. The folder aisland runs in
  and `AISLAND_WORKSPACE` change nothing.
- **From a wheel** (or a clone no workspace lists), the workspace is `AISLAND_WORKSPACE` when set, else the one the
  current folder belongs to (`locations.find_workspace`: a superproject, a folder of projects, a project's parent), and
  the state lives in `<workspace>/.aisland/`.

Nothing is fixed at import: the state folders are functions (`paths.data_dir()`, …) asked through
`locations.state_dir()`, so a long-running MCP server or a test that moves the workspace sees the move. Outside a
workspace the CLI refuses every command that works on one before it writes anything.

## The decisions

Every record runs Context, Decision, Consequences, Example and "What you may change"; where its title carries a date,
that is when it was decided, and an amendment says its own date. A record is changed when its mechanism changes; a
mechanism that is replaced gets a new record, and the old one says which supersedes it.

**The workspace, its projects and its file**

- [0001 — Project sources are a two-layer registry, and only the committed layer reaches a committed file](decisions/0001-project-sources-are-a-two-layer-registry-and-only.md)
- [0011 — A dev dependency that needs more than pip is declared by the dependency itself](decisions/0011-a-dev-dependency-that-needs-more-than-pip-is-declared.md)
- [0027 — From a checkout aisland finds everything from its own place; from a wheel, from where it runs (2026-10-19)](decisions/0027-from-a-checkout-aisland-finds-everything-from-its-own.md)
- [0028 — A folder of projects is a workspace: its subfolders are read as submodules (2026-10-19)](decisions/0028-a-folder-of-projects-is-a-workspace-its-subfolders.md)
- [0029 — Every workspace gets aisland's agent commands and rules (2026-10-20)](decisions/0029-every-workspace-gets-aislands-agent-commands-and-rules.md)
- [0030 — A link to a folder of the workspace is a short name, never a project (2026-10-21)](decisions/0030-a-link-to-a-folder-of-the-workspace-is-a-short-name.md)
- [0033 — A project declares what it brings; aisland finds it in two stages and the project never imports aisland](decisions/0033-a-project-declares-what-it-brings-aisland-finds.md)
- [0034 — `workspace.toml`: the workspace written down like `.gitmodules`, kept current by setup](decisions/0034-workspacetoml-the-workspace-written-down-like-gitmodules.md)
- [0035 — `aisland workspace configure`: one set of commands built from each plugin's keys; `source` the first](decisions/0035-aisland-workspace-configure-one-set-of-commands-built.md)
- [0036 — A root's two choices: a local repository (`--local`), projects it does not record (`track unset`)](decisions/0036-a-roots-two-choices-a-local-repository-local-projects.md)
- [0037 — A clone the workspace does not list: setup adds it as a submodule](decisions/0037-a-clone-the-workspace-does-not-list-setup-adds.md)
- [0038 — Which projects a machine has: the `active` key marks a project optional, its value the default](decisions/0038-which-projects-a-machine-has-the-active-key-marks.md)
- [0039 — What each project installs: selections the projects declare, committed in `workspace.toml`](decisions/0039-what-each-project-installs-selections-the-projects.md)
- [0040 — An extra a project marks is installed only when named (2026-10-23)](decisions/0040-an-extra-a-project-marks-is-installed-only-when-named.md)
- [0041 — A project's wheels folder is a find-links path of the generated `uv.toml` (2026-10-23)](decisions/0041-a-projects-wheels-folder-is-a-find-links-path.md)
- [0042 — The workspace's name can be written in `workspace.toml` (2026-10-23)](decisions/0042-the-workspaces-name-can-be-written-in-workspacetoml.md)
- [0043 — System tools: declared by the projects, checked before installing, aisland's own in the package](decisions/0043-system-tools-declared-by-the-projects-checked-before.md)
- [0045 — A project is the workspace's own by its GitHub remote: no organisation is written into aisland](decisions/0045-a-project-is-the-workspaces-own-by-its-github-remote-no.md)
- [0046 — `--extra NAME`: one uv command installs the project with its wheels (2026-10-27)](decisions/0046-extra-name-one-uv-command-installs-the-project.md)
- [0050 — The example environment: each project lists its variables, aisland concatenates them (2026-10-27)](decisions/0050-the-example-environment-each-project-lists-its-variables.md)
- [0054 — A project names its click CLIs, and the package names nothing of the workspace it was written in (2026-10-28)](decisions/0054-a-project-names-its-click-clis-and-the-package-names.md)
- [0057 — `.env` is read as the shell reads it, by one reader (2026-10-28)](decisions/0057-env-is-read-as-the-shell-reads-it-by-one-reader.md)
- [0058 — A failed `init` removes the folder it made (2026-10-28)](decisions/0058-a-failed-init-removes-the-folder-it-made.md)
- [0067 — `workspace.toml` says its format, and a key nobody reads is named (2026-10-30)](decisions/0067-workspacetoml-says-its-format-and-a-key-nobody-reads.md)
- [0070 — A project's `pyproject.toml` and `.gitmodules` each have one reader (2026-10-30)](decisions/0070-a-projects-pyprojecttoml-and-gitmodules-each-have.md)
- [0075 — A workspace combines harness sets](decisions/0075-a-workspace-combines-harness-sets.md)
- [0076 — A checkout of aisland is one only where a workspace lists it](decisions/0076-a-checkout-of-aisland-is-one-only-where-a-workspace.md)

**Editor, shell and MCP**

- [0002 — The VS Code workspace file is generated per machine, never tracked](decisions/0002-the-vs-code-workspace-file-is-generated-per-machine.md)
- [0003 — aisland's tools are its own MCP server, a chosen set (2026-10-21)](decisions/0003-aislands-tools-are-its-own-mcp-server-a-chosen-set.md)
- [0004 — The editor's theme and colours are a per-machine choice laid over the generated workspace file (2026-10-21)](decisions/0004-the-editors-theme-and-colours-are-a-per-machine-choice.md)
- [0005 — Completion install is a command, not a section of the bootstrap script (2026-07-31)](decisions/0005-completion-install-is-a-command-not-a-section.md)
- [0006 — The completion block leaves a non-interactive shell before it runs anything (2026-10-20)](decisions/0006-the-completion-block-leaves-a-non-interactive-shell.md)
- [0007 — TAB on an empty word offers a command's options (2026-10-21)](decisions/0007-tab-on-an-empty-word-offers-a-commands-options.md)
- [0031 — Every MCP server is started with the workspace's environment (2026-10-21)](decisions/0031-every-mcp-server-is-started-with-the-workspaces.md)
- [0032 — A terminal on the remote machine rides the VS Code tunnel it already runs (2026-10-21)](decisions/0032-a-terminal-on-the-remote-machine-rides-the-vs-code.md)
- [0053 — `aisland --help` works out the projects' services once per run (2026-10-28)](decisions/0053-aisland-help-works-out-the-projects-services-once-per.md)
- [0055 — The commands print the real install line, and every command is on a page (2026-10-28)](decisions/0055-the-commands-print-the-real-install-line-and-every.md)
- [0063 — Listing the services imports no project's code (2026-10-29)](decisions/0063-listing-the-services-imports-no-projects-code.md)
- [0065 — The MCP service tools take any service of the workspace (2026-10-29)](decisions/0065-the-mcp-service-tools-take-any-service-of-the-workspace.md)

**Services and Jenkins**

- [0009 — A containerised service pins its image by digest and is told what the VM shares](decisions/0009-a-containerised-service-pins-its-image-by-digest.md)
- [0018 — Jenkins runs on the Java its WAR accepts, by full path](decisions/0018-jenkins-runs-on-the-java-its-war-accepts-by-full-path.md)
- [0044 — A service a project brings: declared in its `pyproject.toml`, or a class of its own that imports nothing of aisland](decisions/0044-a-service-a-project-brings-declared-in-its-pyprojecttoml.md)
- [0052 — aisland installs its own tools when a feature first needs one, after asking (2026-10-28)](decisions/0052-aisland-installs-its-own-tools-when-a-feature-first.md)
- [0056 — Jenkins' port is written once (2026-10-28)](decisions/0056-jenkins-port-is-written-once.md)
- [0064 — On Linux the container engine is the system's (2026-10-29)](decisions/0064-on-linux-the-container-engine-is-the-systems.md)

**CI, tests and the doctor**

- [0010 — The scaffolder pre-installs the closure of what CI installs — not every reachable extras group](decisions/0010-the-scaffolder-pre-installs-the-closure-of-what-ci.md)
- [0012 — The local pipeline tests the venv, it does not install](decisions/0012-the-local-pipeline-tests-the-venv-it-does-not-install.md)
- [0013 — A long stage of the local pipeline has a time limit, and past it the stage fails](decisions/0013-a-long-stage-of-the-local-pipeline-has-a-time-limit.md)
- [0014 — A recurring environment skew becomes a doctor check, not a note](decisions/0014-a-recurring-environment-skew-becomes-a-doctor-check.md)
- [0015 — Every pytest run of a workspace project is recorded, and the split is read from the record](decisions/0015-every-pytest-run-of-a-workspace-project-is-recorded.md)
- [0047 — The test-run record can be turned off per workspace, project or machine](decisions/0047-the-test-run-record-can-be-turned-off-per-workspace.md)
- [0059 — aisland's fast run is parallel (2026-10-28)](decisions/0059-aislands-fast-run-is-parallel.md)
- [0060 — A project off GitHub never names one in its CI files (2026-10-28)](decisions/0060-a-project-off-github-never-names-one-in-its-ci-files.md)
- [0061 — The test-run record is written only in an aisland workspace (2026-10-29)](decisions/0061-the-test-run-record-is-written-only-in-an-aisland.md)
- [0068 — The CI files come from a template set (2026-10-30)](decisions/0068-the-ci-files-come-from-a-template-set.md)
- [0069 — The Jenkins jobs' `config.xml` comes from templates that escape every value (2026-11-05)](decisions/0069-the-jenkins-jobs-configxml-comes-from-templates.md)
- [0072 — A `package.json` with a `test` script is a Node test run CI runs](decisions/0072-a-packagejson-with-a-test-script-is-a-node-test-run-ci.md)
- [0074 — Warnings go to stderr, and the doctor and the notebook filter run outside a workspace](decisions/0074-warnings-go-to-stderr-and-the-doctor-and-the-notebook.md)

**Git: pull, push and mirrors**

- [0016 — `pull-all` settles a submodule pointer only to a commit holding both sides](decisions/0016-pull-all-settles-a-submodule-pointer-only-to-a-commit.md)
- [0017 — A root without an origin remote is not pulled or pushed, and its submodules follow their own branch](decisions/0017-a-root-without-an-origin-remote-is-not-pulled-or-pushed.md)
- [0024 — A mirror is push-only, fast-forward-only, and copies the primary (2026-10-16)](decisions/0024-a-mirror-is-push-only-fast-forward-only-and-copies.md)
- [0073 — A git command that reaches a remote gives up on one that stops sending](decisions/0073-a-git-command-that-reaches-a-remote-gives-up-on.md)

**Wheels**

- [0019 — A project installed outside the workspace gets its unpublished dependencies as wheels](decisions/0019-a-project-installed-outside-the-workspace-gets.md)
- [0020 — What `aisland wheels` builds is a scope, and the install is one generic script naming each wheel by its file](decisions/0020-what-aisland-wheels-builds-is-a-scope-and-the-install.md)
- [0021 — The wheels folder is the machine's: ignored unless `--commit`, carried as one self-installing zip (2026-10-18)](decisions/0021-the-wheels-folder-is-the-machines-ignored-unless-commit.md)
- [0022 — `--include-project`: the project's own wheel, installed in place of its folder (2026-10-21)](decisions/0022-include-project-the-projects-own-wheel-installed.md)
- [0023 — `--bundle`: one wheel, one name (2026-10-23)](decisions/0023-bundle-one-wheel-one-name.md)
- [0025 — `--force` builds a dirty project from its working tree, and says so (2026-10-20)](decisions/0025-force-builds-a-dirty-project-from-its-working-tree.md)
- [0026 — `--no-check` builds the wheels and checks nothing, and says so (2026-10-20)](decisions/0026-no-check-builds-the-wheels-and-checks-nothing-and-says.md)
- [0048 — `aisland wheels prune`: old wheel versions leave the history; `reset` follows on every other clone (2026-10-27)](decisions/0048-aisland-wheels-prune-old-wheel-versions-leave.md)
- [0049 — A project not on GitHub gets wheels that say nothing of GitHub (2026-11-03)](decisions/0049-a-project-not-on-github-gets-wheels-that-say-nothing.md)

**Housekeeping and the shape of the code**

- [0008 — Reclaiming a checkout's disk is a MOVE into a dated store, and it is per-project](decisions/0008-reclaiming-a-checkouts-disk-is-a-move-into-a-dated-store.md)
- [0051 — A command that prints an `Error:` line exits 1 (2026-10-27)](decisions/0051-a-command-that-prints-an-error-line-exits-1.md)
- [0062 — One exception root, and `--debug` (2026-10-29)](decisions/0062-one-exception-root-and-debug.md)
- [0066 — `manager.py` is split by what each part does (2026-10-29)](decisions/0066-managerpy-is-split-by-what-each-part-does.md)
- [0071 — A diagnostic is a debug record `--debug` shows, never output (2026-10-30)](decisions/0071-a-diagnostic-is-a-debug-record-debug-shows-never-output.md)
- [0077 — The CLI, the CI scaffold and the wheels run are split by what they do](decisions/0077-the-cli-the-ci-scaffold-and-the-wheels-run-are-split.md)
- [0078 — Imports at the top, containers typed, one completion guard](decisions/0078-imports-at-the-top-containers-typed-one-completion-guard.md)
