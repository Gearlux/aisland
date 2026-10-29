# Aisland Mandates

Rules only. The reasons are in [`docs/architecture.md`](docs/architecture.md), cited by the
short names below; usage is in the [README](README.md) and its pages under `docs/`. Root `AGENTS.md` rules are not
repeated here.

Records: [sources] project sources · [folders] the generated workspace file · [frameworks] framework
selection · [completion] completion install · [config] one config file · [clean] reclaiming
disk · [containers] containerised services · [closure] the pre-install closure ·
[playwright] dev dependencies needing more than pip · [local-pipeline] the local pipeline ·
[stage-limits] a long stage's time limit · [doctor] the environment doctor · [test-stats] every
pytest run recorded · [pull-all]
`pull-all` settles a pointer only to a commit holding both sides · [tunnel] the remote tunnel runs where there is no
workspace and its remembered workspaces are a cache · [wheels] a project
installed outside the workspace gets its unpublished dependencies as wheels · [mirror] a mirror
is push-only, fast-forward-only, and copies the primary · [wheel-mode] from a checkout everything
is found from aisland's place, from a wheel from where it runs · [folders-as-submodules] a folder of
projects is a workspace, its subfolders read as submodules · [workspace-file] a workspace is written down in a
file a machine without aisland reads · [harness] every workspace gets aisland's agent commands and rules · [editor] the editor's theme and colours are a per-machine choice laid over the
generated workspace file · [declarations] a project declares what it brings, aisland finds it in two stages and the project never imports aisland · [mcp-server] aisland's tools are its own server, a chosen set · [aliases] a link to a folder of the workspace is a short name, never a project ·
[tunnel-terminal] a terminal on the remote machine rides the VS Code tunnel it already runs · [local-workspace] a root
repository no one shares tracks only the projects, and an untracked project's commits are never recorded. · [github-owner] a project is the
workspace's own by its GitHub remote, no organisation is written into aisland. · [test-stats-switch] the test-run record is
turned off per workspace, project or machine, never removed. [failures] a command that prints an `Error:` line exits 1. [on-demand-tools] aisland's own tools are installed when a feature first needs one, after asking. [env-example] the workspace's
`env.complete` and `env.example` are each project's own two lists, merged per machine under each variable's owner. [wheels-prune] old committed wheel
versions leave the history by one deterministic strip, which every other clone re-runs. [errors-root] every error aisland
expects is an `AislandError`, one `Error:` line; `--debug` adds the traceback. [lazy-plugins] listing the services
imports no project's code. [test-stats-workspace] the test-run record is written only in an aisland workspace.
[linux-docker] on Linux the container engine is the system's, checked and never managed. [mcp-services] the MCP
service tools take any service of the workspace. [manager-split] `aisland setup` runs steps that each live in a
module of their own.

## Current state

The workspace's dev-infrastructure CLI, `aisland` (click, `aisland/cli.py`):
- `errors.py`: `AislandError`, the root of every exception aisland defines (a `click.ClickException`, so the CLI
  prints it as one `Error:` line) [errors-root]. `downloads.py`: `download(url, target)`, every file aisland fetches,
  with a time limit.
- `locations.py`: where the workspace and the state are — from a checkout as always, from a wheel
  where aisland runs (standard library only; the pytest plugin imports it).
- `manager.py`: `aisland setup` — it runs the steps in order: provisioning, submodule activation, editable installs
  (`_run_installs`), the generated workspace files, the git aliases. Each step's code is a module of its own
  (user request 2026-10-29: `manager.py` was a 1722-line hub) [manager-split]:
  - `projects.py`: the workspace root and name, the project folders (`workspace_folders`, `submodule_active_map`), the
    dependency order (`get_install_order`, `install_groups`, `dependency_order`);
  - `installers.py`: Homebrew packages, the pinned userspace archives (`USERSPACE_TOOLS`), `ensure_uv_in_venv`;
  - `paths.py`: the state folders (`BIN_DIR`, `DATA_DIR`, `TOOLS_DIR`); `services/base.py`: `BaseService`;
  - `editor_files.py`: the VS Code workspace file, `pyrightconfig.json`, the root `mypy.ini`'s `mypy_path`;
  - `mcp_config.py`: `.mcp.json` and `.gemini/settings.json`; `uv_config.py`: the root `uv.toml`;
  - `quality.py`: `aisland check`.
  A name another module uses is public (no `_`); a new step goes into its own module, never back into `manager.py`.
  **A test patches the module whose code makes the call**, never the module that defines the name: `from x import f`
  binds a copy, so patching `manager.f` misses `quality`'s `f`. The workspace root has one source every module reaches —
  a test points it at its folder with `monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)`. Why: after
  the split, tests patching `manager.get_workspace_root` let `aisland check` run on the REAL workspace (its
  `isort .`/`black .` write files).
- `workspace.py`: `aisland workspace init <folder>`, a new workspace or an existing repository set up in place (its
  starting files, the `--from` file's projects and the `--add` submodules, its `workspace.toml`, the git aliases;
  `--local`, `--untracked`).
- `local_workspace.py`: a local workspace (`init --local`; user request 2026-10-23) — a root repository that tracks
  only the projects: `.gitignore` `/*`, `.gitmodules` and the projects committed, marked by `aisland.workspace.local`
  in `.git/config` (never a tracked file: the root is no one else's). `settle` commits `.gitmodules` and the
  projects' paths — nothing else staged — at init, after every `activation.reconcile` (setup, `configure active`)
  and after `track|untrack`; a commit git refuses is said, never raised; an unmarked workspace is never touched; the
  `active` plugin adds a project there with `-f`. (`tests/test_local_workspace.py`,
  `tests/test_workspace_init.py::test_a_local_workspace_tracks_only_its_projects_and_has_nothing_to_commit`)
- `clones.py`: `aisland setup` adds a clone the workspace does not list as a submodule BY ITSELF, never asking (user
  ruling 2026-10-23: "aisland setup MUST take care of this automatically" — do not add a prompt back); tracked as THE
  WORKSPACE says (`tracking.workspace_default`). A clone: a direct, non-hidden subfolder with a `.git` FOLDER, an
  `origin` and a `pyproject.toml`, not in `.gitmodules`, no alias link; only in a workspace with `.gitmodules` or a
  local root (a folder of clones has none). `git submodule add -b <its branch> <its origin> <folder>` keeps the clone
  as it is; `-f` only in a local root; a refused add is said and setup goes on. Its tracking is `tracking.apply`'s: the
  workspace's `[workspace] track`, this machine's `aisland.track.default` — never inferred from the other projects. Runs before
  `workspace_file.refresh` in setup. (`tests/test_setup_adds_clones.py::test_setup_never_asks`,
  `tests/test_setup_installs_git_aliases.py::test_setup_adds_the_clones_the_workspace_does_not_list`)
- `recording.py`: `aisland workspace test-stats on|off [PROJECT…] [--machine-only]` — whether the pytest plugin
  records a project's runs; see "Test-run statistics".
- `tracking.py`: `aisland workspace track|untrack [PROJECT…] [--machine-only]` (user choice 2026-10-23: two commands,
  no project = every submodule; not a `configure` plugin) — whether the root records a project's commits. Committed:
  the project's `track = false` in `workspace.toml`, written as `ignore = all` in `.gitmodules`; this machine:
  `aisland.track.<project>.track`, written as git's `submodule.<name>.ignore` (`all`/`none`) in `.git/config`, which
  git reads first — aisland's key because `submodule deinit` drops git's section, and `apply` (every
  `activation.reconcile`) writes it back. A committed `track|untrack` drops this machine's choice for those projects. **With no project named it is the
  WORKSPACE's choice (user correction 2026-10-23):** `[workspace] track = false` (per machine `aisland.track.default`),
  which every project without a `track` of its own follows, those added later included — so `init --untracked`, or
  one `untrack`, then only `aisland setup`; a project's own `track` is written only where it differs, and `untrack`
  with no submodules yet is allowed (it records the choice).
  An `ignore` value aisland does not write (`dirty`, `untracked`) is the user's and left alone. **An untracked
  project follows its branch (user request 2026-10-23):** one on no branch is fetched and switched to its branch —
  `.gitmodules`' `branch` (`.` = the root's), else origin's default; its local branch, fast-forwarded only when
  strictly behind, else a new one tracking origin's — by `follow_branches` (every `activation.reconcile`, `untrack`)
  and by `git pull-all` before it merges; never `-f`, a refused switch is said and the project left; a tracked project
  on no branch is never moved (`tests/test_tracking.py::test_a_tracked_project_on_no_branch_is_left_on_its_commit`,
  `tests/test_git_pull_push_all.py::test_an_untracked_submodule_on_no_branch_is_put_on_its_branch_and_pulled`). Only submodules: a
  workspace without them, and a name that is no submodule's, are refused before anything changes. `init
  --untracked` is `untrack` for every project. (`tests/test_tracking.py`,
  `tests/test_workspace_init.py::test_untracked_hides_a_projects_new_commits_and_writes_it_down`)
- `env_example.py`: `aisland workspace env-example [--missing]` (both files; also setup's step 5b and `init`'s `.env` seed) and
  `aisland workspace env-test [PROJECT…]` [env-example]; see "Generated and committed workspace files".
- `workspace_file.py`: `workspace.toml`, the workspace's committed file shaped like `.gitmodules` (the how-to for a
  machine without aisland, then a `[project."<name>"]` per project: `path`, `url`, `branch`), kept current by
  `aisland setup` (`refresh`) [workspace-file].
- `server.py`: `aisland-mcp`, aisland's MCP server (the `[mcp]` extra) — a chosen set of tools, listed in
  `.mcp.json` by setup's discovery.
- `discovery.py`: what the projects declare to aisland, found in two stages, and `aisland workspace show` [declarations].
- `editor.py`: `aisland workspace theme|color`, this machine's VS Code theme and colours (`css_colors.py`: the CSS
  names), kept in `.git/config` and laid over the generated workspace file.
- `configure.py`: `aisland workspace configure`, one set of commands built from each plugin's keys — the
  project-level built-ins `source` (`sources.py`), `mirror` (`mirrors.py`, its action `aisland workspace mirror
  sync`), `active` (`activation.py`), and a workspace-level plugin per selection (`selection.py`).
- `supervisor.py` (process-compose), `doctor.py` (`aisland doctor`), `completion.py`
  (`aisland completion install`), `clean.py` (`aisland clean`, `aisland backups`).
- `pytest_stats.py`: the `aisland_test_stats` pytest plugin (entry point `pytest11`) that
  records every run of a workspace project, and `aisland tests report` reading it beside the
  Jenkins builds' JUnit results.
- `wheels.py`: `aisland wheels build <project>` (`--bundle`: one merged wheel, `wheels_bundle.py`), the wheels of the workspace projects it needs that
  are not on PyPI (or, by flag, every needed or every workspace project), written into
  `<project>/wheels/` with `wheels.yaml` and `install.py` (`wheels_install.py`), and the check of what it
  takes from PyPI; the folder git-ignored unless `--commit`, packed by `aisland wheels zip` into a zip that
  installs with plain `python` (`wheels_unzip.py`), installed elsewhere by `aisland wheels install`.
  `wheels_prune.py`: `aisland wheels prune <project> [--apply]` strips the committed wheel versions no branch or tag
  holds from the history and force-pushes origin; `aisland wheels reset <project>` brings another clone onto it.
- `nightly.py`: `aisland tests nightly`, the morning report of the orchestrator's nightly runs
  (`aisland/data/nightly/<date>.md`, a desktop notification, exit 1 on a bad night), run by the
  `<workspace>.nightly-check` Jenkins job.
- `tunnel.py`: `aisland tunnel` — a VS Code Remote Tunnel to the machine aisland runs on. One
  detached process with a pidfile, NOT a `BaseService` (the remote box has no process-compose) and
  not in `aisland services start`; workspace-free in `cli.py`'s `_WORKSPACE_FREE`, because the box it is for
  has no workspace. State is PER MACHINE, `~/.aisland/tunnel` (`AISLAND_TUNNEL_HOME` overrides it,
  for a `$HOME` that does not persist) — never derived from the working directory, which made
  `start` in one folder and `status` in another disagree. `code tunnel` is passed NO account option
  (it has none; the sign-in runs first, separately). The remembered workspaces back `open`'s shell
  completion — aisland's first dynamic completion — so every reader of that file treats a bad file
  as empty rather than raising.
- `tunnel_connect.py` + `tunnel_shell.py`: `aisland tunnel connect`, a terminal through the SAME VS Code tunnel
  (user request 2026-10-21: no sshd on the box, no second tunnel). [tunnel-terminal] `devtunnel connect` on the
  connecting machine forwards the tunnel's control port 31545; aisland's MessagePack client (`msgpack`, the
  `[connect]` extra) `spawn`s `tunnel_shell.py` FROM ITS SOURCE (`python3 -c`), which runs the account's shell in a
  pseudo-terminal — interactive, NOT a login shell (user choice 2026-10-21), so `~/.bashrc` runs even where
  `~/.profile` does not load it (`::test_the_shell_reads_its_interactive_start_up_file_and_is_no_login_shell`). `tunnel_shell.py` imports the standard library only — the box has a Python, not aisland
  (`tests/test_tunnel_connect.py::test_the_helper_runs_on_a_python_without_site_packages`). A taken port 31545 is
  refused, never reused (`::test_a_taken_control_port_is_refused_naming_its_process`); the forward aisland starts is
  always stopped (`::test_the_forward_is_stopped_when_the_shell_ends`); raw mode is entered with `TCSADRAIN`, never
  `tty.setraw`'s default flush, which drops typeahead (`::test_the_local_terminal_is_put_back_as_it_was`). The
  control protocol is the VS Code CLI's: `tests/fake_devtunnel.py` speaks it and changes with it. The user page is
  `docs/tunnel.md` (the README keeps three commands and its absolute link), its twin `examples/01_tunnel_terminal.py`;
  an error the page quotes is the code's, word for word (`tests/test_tunnel_docs.py`).
- `services/`: `jenkins` (incl. `aisland jenkins scaffold`) and `swiftbar` run under the supervisor;
  `docker` (Colima) runs on its own; `registry.py` lists them, `declared.py` and `plugin.py` run the ones a project
  brings (FiftyOne is curaitor's; MLflow, Aim and ClearML are matrainer's, `matrainer/services/` — MLflow since
  2026-10-28, started when named like every service a project brings; Label Studio is annotaide's,
  `annotaide/labelstudio_service.py`; IQEngine is the signal project's that reads SigMF).
- `setup.sh`: interpreter + venv, then `aisland setup -y` and `aisland completion install`.
- `harness.py` + `harness/`: the agent harness — the workspace commands, their helper script and the generic
  agent rules — installed into every workspace's `.agents/` by `aisland setup` and `aisland workspace init`.
- `aisland/git/setup_git.sh`: the workspace git aliases, local config of the root and every active
  submodule; `pull-all` runs `git_pull_all.sh`. `aisland setup` runs it with the root on every run, whichever
  way aisland is installed, and `workspace init` once; there is no command of its own (user request 2026-10-21:
  fewer commands; `tests/test_setup_installs_git_aliases.py`).

Invariants: only supervisor-started services exist (one started elsewhere is invisible; the
start-time port pre-flight naming the PID is the only signal). All service state lives under
`<workspace>/aisland/data/` (from a wheel, `<workspace>/.aisland/data/`).

## Errors exit 1

- **A command that prints an `Error:` line exits 1 (user request 2026-10-27, release review).** A failure that ends
  the command is raised as a `click.ClickException` — one `Error:` line on stderr. A failure the command goes on past
  (a project that fails to install while setup installs the others, a service that does not start beside the others,
  a Jenkins step) is `failures.report(...)`-ed: its line goes to stderr, the command finishes, and
  `cli._WorkspaceGroup.invoke` turns the run into exit 1, also over a command's own `SystemExit(0)`; a non-zero code
  of the command's own is kept. Never `click.echo("Error…")` and return: a script, CI or the MCP server's `aisland`
  call reads the exit status (`tests/test_errors_exit_one.py`) [failures].
- **One exception root, and `--debug` (user request 2026-10-29, release review).** [errors-root] Library code raises
  `AislandError` (`errors.py`) — never `click.ClickException` itself, never `SystemExit` (the harness scripts and
  `aisland-mcp`'s `main` are their own entry points) — and an exception class aisland defines derives from it AND the
  builtin it stands for (`DeclarationError(AislandError, ValueError)`, `ReleaseProbeError(AislandError,
  ConnectionError)`). `cli._WorkspaceGroup.invoke` turns an `OSError` or `CalledProcessError` that reaches it into one
  `Error: <it> (run `aisland --debug …` for the traceback)` line; `aisland --debug` re-raises it and prints the
  traceback of aisland's own refusals too; any other exception is a bug and keeps its traceback. An `except` names
  what can fail there (`OSError`, `ET.ParseError`, `tomllib.TOMLDecodeError`), never `Exception` — except around a
  project's own code and a completion callback. Every download goes through `downloads.download` (a timeout, no
  part file left), and a `git` network call sets `GIT_TERMINAL_PROMPT=0` and a timeout
  (`tests/test_errors_exit_one.py::test_no_library_module_raises_clicks_own_exception_or_exits` and its neighbours,
  `tests/test_downloads.py`).

## Workspace root and paths

- **From a checkout aisland works as it always did (user ruling 2026-10-19).** [wheel-mode] `locations.FROM_CHECKOUT`
  (aisland's `pyproject.toml` beside the package, which no wheel carries) selects the rule: from a
  checkout the root and the state follow the package's location, and the folder you run in and
  `AISLAND_WORKSPACE` change nothing; from a wheel the root is `AISLAND_WORKSPACE`, else
  `locations.find_workspace` (first hit upwards: `.gitmodules`; the start folder when it holds
  projects; a project's parent; a `.git` folder — the subfolder rule only at the start, or a home
  folder holding one project becomes everyone's workspace), and the state is `<root>/.aisland/`. Never
  read the cwd or `AISLAND_WORKSPACE` in checkout mode, and never find a path by counting folders up
  from a module (`parents[N]`): ask `locations`/`get_workspace_root()`. (`tests/test_locations.py`,
  `tests/test_packaging.py::test_from_the_wheel_the_workspace_is_where_it_runs_and_the_state_goes_into_it`)
  - Importing never raises; outside a workspace the CLI's group (`cli._WorkspaceGroup.parse_args`)
    refuses every command but `workspace`, `wheels` and `--help`, before anything is written
    (`::test_a_workspace_command_is_refused_and_writes_nothing`). A new workspace-free command joins
    `_WORKSPACE_FREE`.
  - From a wheel, setup copies the packaged `project.bashrc` to `.aisland/` and links the root to it
    (`setup.py` puts it in the wheel; it stays at `aisland/project.bashrc` in the checkout, whose place
    defines the root); the file finds `bin/` beside itself. (`tests/test_project_bashrc.py`) `MANIFEST.in`
    keeps it in the source package, from which `python -m build` builds the wheel — a release is built that way
    (`tests/test_packaging.py::test_a_release_build_puts_the_shell_init_into_the_wheel`).
  - The release's metadata is what PyPI shows: the README as Markdown, `license = "MIT"` (SPDX, no `License ::`
    classifier, setuptools >= 77), the author and the GitHub URLs, a Development Status following the version and
    Python classifiers `requires-python` admits. The build prints no deprecation warning and `twine check --strict`
    passes (`tests/test_packaging.py::test_twine_accepts_the_release_strictly` and its neighbours).
  - GitHub CI checks aisland out ALONE and installs only `.[dev]`, so `dev` holds everything the gates and the
    suite need there: the tools, `liquifai`, and `setuptools`, `wheel` and `uv` (the build and wheels tests build
    without isolation and run the uv beside the interpreter). A test asserting the REAL workspace (its root, the
    servers it ships, its shell init) carries `needs_workspace` (`tests/workspace_layout.py`) and is skipped there
    with its reason, never failed (`tests/test_workspace_layout.py`). The runners have no git identity: every test
    commits as `conftest.git_identity`'s author (`tests/test_git_identity.py`), never as the machine's. Reproduce CI
    in a fresh venv holding only `aisland[dev]` and `pip`, in a `git clone` of aisland alone, with `HOME` an empty
    folder, before changing either.
  - A release ships `LICENSE` (`license-files`), `CHANGELOG.md` (`MANIFEST.in`) and `aisland/py.typed`, and the
    newest `## [<version>]` of the CHANGELOG is `pyproject.toml`'s version. It is published by the hand-written
    `.github/workflows/release.yml` on a `v<version>` tag: the quality gates and both test runs (`pytest tests -m ''`,
    a 45-minute limit; the build waits on them), the tag guard, `python -m build`, `twine check --strict`,
    a smoke test of the wheel in a clean venv from `/tmp` (`--help`, the version, `workspace init`), then Trusted
    Publishing from the `pypi` environment. Change a step and its test together (`tests/test_packaging.py`:
    `::test_the_tag_guard_refuses_a_tag_that_is_not_the_version` runs the guard's own script).
  - `project.bashrc` ALWAYS sets `PROJECT_ROOT` from its own place, never keeps an inherited one: an editor
    started from a shell that sourced another workspace passes that root down, and its `.venv` was activated
    (`::test_a_project_root_inherited_from_another_workspace_is_replaced_by_this_ones`). A `.env` with no
    settings exports nothing — a bare `export` lists every variable (`::test_an_env_file_without_settings_prints_nothing`).
    `.env` is SOURCED under `set -a`, never word-split through `xargs` (which cut `"/a b"` at the space and left
    `$VAR` unexpanded) (`::test_a_quoted_value_with_a_space_is_exported_whole_and_a_variable_in_a_value_expanded`).
    Each shell function it defines (`cdp`, `cdd`, `cdm`, `cdr`, `h`, `tm`) is `unalias`ed first: an alias of that
    name is expanded inside the definition and breaks the file (`::test_h_is_defined_even_where_an_alias_h_exists`).
    Tab after `cdp`/`cdd`/`cdm`/`cdr` completes a folder under ITS root (`_aisland_cd_complete`), never the current
    folder's, a spaced name escaped; it runs under `/bin/bash` 3.2 (no `compopt`: `nospace` at registration) and
    unescapes the typed word without `eval`, which would run a `$(…)` typed before Tab
    (`::test_tab_after_a_cd_helper_offers_the_folders_under_its_root_and_never_a_file`).
    `tm` hands `tmux new-session` the current folder and every exported variable but the terminal's and tmux's own,
    read NUL-separated (`env -0`): a running server otherwise gives a new session the environment it started with
    (`::test_tm_starts_a_session_in_the_current_folder_with_every_exported_variable`,
    `::test_tm_leaves_the_terminal_and_tmuxs_own_variables_to_tmux`).
- From a checkout, `get_workspace_root()` (`locations.checkout_workspace_root`) returns the directory
  holding `.gitmodules` (+ `TASKS.md`); without submodules the OUTERMOST `TASKS.md`, then the outermost
  `.git`; last resort `parents[2]`.
  Never "first `TASKS.md` wins": every project has one. (`tests/test_workspace_root.py`)
- Binaries, data and logs resolve to `<workspace>/aisland/` from the package's own location,
  never the cwd. Nothing is written into the workspace root as service state.
- A file the package reads or runs (the workspace-file template, the git alias scripts) lives
  INSIDE `aisland/aisland/` and is named in `[tool.setuptools.package-data]`, so a wheel carries
  it; never read one from the project folder beside the package.
  (`tests/test_packaging.py::test_the_wheel_carries_every_file_of_the_package`)
- **The workspace's name is `[workspace] name` in `workspace.toml`, else its origin's repository, else its folder
  (user request 2026-10-23).** `projects.get_workspace_name` is the one reader (`workspace_file.name_of`); it names
  `<name>.code-workspace`, the Jenkins jobs, the nightly report, the how-to and `init`'s first line. A name that is no
  plain name is refused naming its line; a file aisland cannot read leaves the name to the origin and the folder;
  discovery refuses a selection called `name`. (`tests/test_workspace_file.py::test_the_files_name_names_the_workspace_before_its_origin_and_its_folder`
  and its neighbours)
- **A workspace is written down in `workspace.toml`, shaped like `.gitmodules` (user rulings 2026-10-20/22).**
  [workspace-file] `workspace_file.py`: the how-to comments (venv with pip and with uv, aisland from PyPI or a wheel,
  `init --from`, `setup`), `[workspace]`, then a `[project."<name>"]` per project — `path` (its folder; the name when
  left out), `url`, `branch`; other keys of a project (later steps' settings) are kept as written. `aisland setup`
  keeps it current (`refresh`, from `.gitmodules` or the clones): a new project added, a changed url or path followed,
  a project the workspace lacks KEPT when it has a url — setup's activation step clones it, so writing a row is a way
  to add a project and removing one means deleting its row too (user choice 2026-10-28, release review 3a;
  `tests/test_activation.py::test_setup_clones_a_project_written_into_the_file_by_hand`) — dropped otherwise, a
  branch written once KEPT (only a project without one gets `.gitmodules`' branch); a project
  is matched by its path first, so a hand-written name survives; nothing changed, the file is not touched. `init
  --from` clones each project into its path on its branch and copies the file in; there is no `export`. A link in the
  workspace folder is written as the project's `path` (user ruling 2026-10-21: links come back as direct checkouts).
  Every refusal is a `FileError` naming `file:line` — the old `[projects]` layout, a section the file has not, no url,
  a value not text, a path that is no plain folder, two projects in one folder — and a `--from` file is read BEFORE
  anything is written (`tests/test_workspace_file.py`, `tests/test_workspace_init.py`). `.venv` stays the one venv
  aisland finds — a link counts, and
  `init`'s `.gitignore` says `.venv` (not `.venv/`) so the link is ignored; never add a venv-name setting without
  asking. (`tests/test_workspace_init.py`)
  - **A new workspace whose `init` fails half way is removed (user request 2026-10-28, release review).** A folder
    `init` made, or found empty, goes when a clone fails or the run is interrupted, and the error says so: the same
    command runs again. A folder that held files is never deleted (the error names `.git` and `.gitmodules` to remove);
    an existing repository set up in place is left as it stopped (`tests/test_workspace_init.py::test_a_failed_init_from_a_file_removes_the_folder_it_made_and_runs_again`
    and its neighbours).
  - `init` writes over a starting file ONLY when `--rewrite <file>` names it (a closed set, `workspace.StartingFile`);
    the old copy becomes `<file>.bak`, and a `.bak` already there refuses the whole run before anything is written.
    In an existing repository `--rewrite` writes the named files and nothing else (no git, submodules, aliases or
    harness), and beside any other option it is refused
    (`::test_rewrite_in_an_existing_repository_replaces_the_file_keeps_a_bak_and_writes_nothing_else`,
    `::test_rewrite_refuses_when_a_bak_is_in_the_way_and_changes_nothing`,
    `::test_an_existing_repository_refuses_rewrite_beside_anything_else`).
  - **An existing repository is set up in place (user choice 2026-10-23)**, no longer refused: no `git init`, the
    `--add` projects, the missing starting files, aliases, harness, `--untracked`. `--from` (the repository keeps its
    own `workspace.toml`) and `--local` (that root is already shared) are refused there before anything is written
    (`::test_an_existing_repository_is_set_up_in_place_and_its_files_kept`,
    `::test_an_existing_repository_refuses_a_workspace_file_and_changes_nothing`,
    `::test_local_in_an_existing_repository_is_refused`).

## User docs (`README.md`, `docs/`)

- **The README is the PyPI page: a quickstart of about 170 lines** — install, a first workspace with its output,
  configure, a common-tasks table and an index of every page — and each topic is a `docs/<topic>.md` (user choice
  2026-10-25: the old 2,280-line README split into fifteen pages). A new topic adds its page and its README index row
  in one change; a feature section never grows back into the README (`tests/test_docs.py::test_the_readme_is_a_short_quickstart`,
  `::test_the_readme_indexes_every_page`).
- The README links only absolutely (`https://github.com/Gearlux/aisland/blob/main/…`); a page links relatively, and
  every link lands on a file and, with an anchor, on a heading of it
  (`::test_every_link_in_the_readme_is_absolute_or_an_anchor_of_its_own`, `::test_every_link_of_a_page_lands_on_a_file_and_a_heading`).
- A user page names no project of the workspace aisland is developed in, nor a company: examples use `acme`, `models`,
  `tools`, `viewer`, `app`, `lib`; the tools aisland drives (liquifai's completion, loggair's log folder) keep their
  names (`::test_a_user_page_names_no_project_of_the_workspace`; the list is `WORKSPACE_NAMES`). No dated
  measurements or test references on a page; those belong in `docs/architecture.md` and here.
- What the installed package shows is held to the same list: every command's `--help` (its examples use `models`)
  and the usage text of the zip `aisland wheels zip` writes (`wheels_unzip.py`'s docstring)
  (`::test_a_commands_help_names_no_project_of_the_workspace`, `::test_the_wheel_zips_usage_names_no_project_of_the_workspace`).
  Every file of the package — code, comments, templates — names no private project, platform or company
  (`tests/test_no_private_names.py::test_no_file_of_the_package_names_a_private_project_or_a_company`); comments may
  still name the workspace's projects (root rule "Docs Never Name Dependent Projects" exempts them), code may not
  (the completion table, a fallback address, a template's placeholder name are gone, 2026-10-28).
- Every command, each verb of each group, is shown on a page or the README (`::test_every_command_is_shown_on_a_page`),
  and every variable `env.complete` lists for a person to set is in the README's table
  (`::test_every_variable_aisland_reads_is_in_the_readme`).
- An example in `examples/` is a page's runnable twin, named on its page (`::test_every_example_is_shown_on_a_page`),
  and touches only a temporary folder: from a checkout aisland works on the workspace it is checked out in whatever
  the current folder, so a command that changes a workspace (`setup`, `configure … set`, `clean`) has no example
  (`TASKS.md`). Each asserts what its page shows and runs in CI's Verify Examples.

## Supervisor and services

- Never `Popen` a service, track it by PID file/`pgrep`, or kill it with a signal: register
  it in the generated `data/process-compose.yaml` and `apply()`. (`tests/test_supervisor.py`,
  `tests/test_supervisor_expansion_integration.py`)
  1. Stop = `disabled: true` + apply, never a bare `process stop`.
  2. Startup success = `supervisor.wait_for_outcome`, never a momentary `is_running`.
  3. Every HTTP service passes a `readiness={"port", "path"}` probe to `start()`.
  4. `start()` pre-flights the port (`supervisor.port_in_use`).
  5. A re-start after a failed run needs an explicit `process start` (`BaseService.start`
     does it): `project update` ignores an unchanged document.
  6. No secret literal in the document: `${VAR}` refs to `.env` keys, and `apply()` layers
     `read_workspace_env()` over `os.environ`. No literal `$` in any value.
- **`.env` is read as the shell reads it, by ONE reader (user request 2026-10-28, release review).**
  `supervisor.read_workspace_env` is what `project.bashrc`'s `set -a; source .env` exports: `export`, quotes (single
  literal, double expanded, `\` escapes), `$VAR`/`${VAR}` from an earlier line then the environment (unset = empty),
  a trailing ` # note` dropped, a line bash reads as no assignment (`KEY = value`) skipped. Services, `apply()` and
  Jenkins read it; never a second parser. `write_workspace_env` single-quotes a value the shell would change, so both
  read it back verbatim (`tests/test_workspace_env.py`, every case checked against bash itself).
- Unix: socket in `aisland/data/`; Windows: TCP `AISLAND_SUPERVISOR_PORT` (default 9089).
  process-compose comes from Homebrew (macOS) or `USERSPACE_TOOLS` (Linux), installed by the first command that
  needs it once the user agrees (`supervisor.ensure_binary` → `provision.ask_to_install`) [on-demand-tools].
- A userspace tool's archive is unpacked into a scratch folder and moved into place whole, so an interrupted unpack
  never leaves a folder a later run takes for finished; an architecture aisland has no build for is refused, never
  read as amd64; a tool naming its builds otherwise (node: `x64`) says so in `arch_names`
  (`tests/test_provision.py::test_a_half_unpacked_folder_from_an_interrupted_run_is_replaced` and its neighbours).
- Every `USERSPACE_TOOLS` entry pins a per-arch `sha256`, checked before extraction; bump
  `version` and digests in one edit; a missing digest is never shipped.
  (`tests/test_provision.py::test_registry_pins_wellformed_sha256_for_every_tool`)
- **Every service is in ONE list, `services/registry.py` (`ENTRIES`, start order; user choice 2026-10-24).**
  `aisland services [start|stop|restart|status|info|reset] [NAME…]` reads it: named → those, in the order given;
  of several, one that fails is reported and the others still get their turn (`registry._each`; a service named
  alone fails as itself) — a missing container engine never keeps Jenkins down
  (`tests/test_services_command.py::test_a_service_that_fails_among_several_is_reported_and_the_others_still_start`);
  unnamed → `status`/`info` every service, the others the `by_default` ones, `stop`/`reset` last-started first (the
  container engine last). A name no service has is refused before anything is done. There are no top-level
  `aisland start|stop|restart|status|info|reset`; a new service is one `Entry` and never a hand-written list in
  `cli.py`. (`tests/test_services_command.py`)
- **`aisland services` asks process-compose as little as it can.** Each question is a client process (0.2-0.4 s on a
  loaded machine): `list_processes` and `get_ports` ask their own question alone (a failed one already means no
  server — no `project state` first), and `registry.status`/`info` run inside `supervisor.snapshot()`, where ONE
  `process list` answers every service. Nothing outlives the block, so `wait_for` still sees each new answer.
  (`tests/test_supervisor_snapshot.py`) The services the projects bring are worked out ONCE per CLI run
  (`cli._WorkspaceGroup._declared`, in `ctx.meta`: `--help` asks every group for its help), and `discovery.installed`
  scans the installed entry points once for every plugin — each scan reads every distribution's metadata
  (`tests/test_plugin_services.py::test_one_help_finds_the_projects_services_once`,
  `::test_loading_reads_the_installed_entry_points_once`). **Listing the services imports no project's code (user
  request 2026-10-29).** [lazy-plugins] A plugin service's `Entry` loads its class on first use (`registry._Plugin`:
  `make`, `details` → its `options` and `reset_question`), and its group builds its commands on first use
  (`cli._ServiceGroup`), so `aisland --help` reads declarations and metadata only (0.65 s → 0.24 s);
  `registry.entries` — what `aisland services …` acts on — still loads and checks every class first, so a broken one
  is refused before anything runs (`::test_help_lists_a_projects_service_without_importing_its_code`,
  `::test_a_plugin_that_cannot_be_imported_is_listed_and_fails_when_used`).
- **A project declares a service in its `pyproject.toml` and writes no aisland code (user choice 2026-10-24).**
  `[tool.aisland.service.<name>]` (`command`, `port`, `ready`, `needs`) becomes a `services/declared.py`
  `DeclaredService` — the command under the supervisor, probed only with both `port` and `ready` — appended to the
  list after aisland's own (`registry.declared`), started only when named, offered only while every `needs` choice is
  in that selection's effective choice. It gets `aisland <name> …` from `cli._WorkspaceGroup.get_command`, after
  aisland's own commands; its help, declared or implemented, names the project that brings it (`Entry.project`:
  `Manage labelstudio, a service annotaide brings.`), and `tests/test_docs.py`'s no-workspace-names rule reads
  aisland's own help without them. A name of an aisland service or command is a `DeclarationError` naming the line; the
  top-level listing (`--help`, completion) leaves the declared groups out then, every command reading them says why.
  (`tests/test_declared_services.py`)
- **A project implements a service in its own code, importing nothing of aisland (user choices 2026-10-25).** An
  `aisland.plugins` entry point of kind `service` (a class needing only `start(ctx)`; `stop`, `status`, `info`,
  `reset` are `BaseService`'s unless it has them, its `info(ctx)` printed after aisland's) becomes a
  `services/plugin.py` `PluginService`, offered once its project is installed, started only when named, with
  `--port` on `start`/`restart` (`ctx.port`, `None` when not given: the class picks its default). `ServiceContext` is
  all it gets: `name`, `workspace`, `data_dir`, `port`, `background`, `run`, `write_env`, `ports`, `echo`, `fail` —
  grow it only with a real service that needs more. A moved service disappears with its project; its data folder
  stays `data/<name>/`. Same name rules as a declared one. A class may declare `options = {"name": "help"}` (offered
  as `--name` on `start`/`restart`, `ctx.options`, `None` when not given; `port`/`background` refused, and a value of
  another shape, naming the line), and `supervised = False` when Docker runs it (its `info(ctx)` then stands alone);
  the context also gives `workspace_name`, `read_env()` and `choose_command(selection, name)` — the command that adds
  a choice keeping the others (`selection.add_command`; an `extras` never chosen keeps `dev`, which it installs). A
  class whose `reset` wipes no data names what `aisland <name> reset` asks first and its help says (`reset_question`). (`tests/test_plugin_services.py`; curaitor's FiftyOne:
  `curaitor/tests/test_fiftyone_service.py`; matrainer's Aim and ClearML: `matrainer/tests/services/`; annotaide's
  Label Studio: `annotaide/tests/test_labelstudio_service.py`)
- A new service extends `BaseService` and implements `launch()` (command, env, probe); the
  base owns start/stop/status/reset. One `aisland/data/<service>/` and log each; no shared
  state.
- A Docker-run service (`docker`) overrides `launch`/`stop`/`status`/
  `reset`; its `info()` skips `super().info()` (the supervisor line would mislead).
- A service never installs its backend: a project's tracking service (matrainer's MLflow, Aim and ClearML) fails
  naming `ctx.choose_command("tracker", <name>)` — `aisland workspace configure tracker set <the trackers already
  chosen> <name>` (`selection.add_command`) — since `set` takes the whole list, naming the missing one alone would
  drop the rest (user ruling 2026-10-23; `tests/test_selection.py::test_the_command_that_adds_a_tracker_keeps_the_ones_chosen`).
- New services write `.env` through `supervisor.write_workspace_env` (a project's class: `ctx.write_env`).
- macOS external volumes: Full Disk Access for the RESOLVED process-compose binary, then
  `aisland supervisor down && aisland services start --background`. Never "fix" it by moving
  `DATA_ROOT`, editing Jenkinsfiles or loosening volume ownership; diagnose with a live
  `log stream`. (`docs/services.md` "macOS: Full Disk Access for the supervisor")
- `aisland jenkins init` seeds a multibranch job per `Jenkinsfile`; `--local` seeds
  `<project>.local` with `Jenkinsfile.local` embedded.
- **Jenkins' port is written once, `JenkinsService.DEFAULT_PORT` (9080); everything that talks to it asks
  `JenkinsService.url()`** — the port it runs on, else the default: `info`, the safe restart, the scaffold's
  Jenkinsfile validation, `aisland tests nightly`'s links, the SwiftBar menu's links, the CLI's `--port` default
  (`tests/test_jenkins_port.py`). Never type the number elsewhere.
- Jenkins (and its plugin manager) runs on the Java its WAR accepts, by FULL path — never the
  bare `java` on PATH, which a shell profile points at the newest JDK (26 broke Jenkins on
  2026-10-06). `services/jenkins_java.py` probes each installed JDK, newest first, with
  `java -jar jenkins.war --version`; no Java version is written into aisland, and the bare
  `openjdk` formula is never one of aisland's tools. On macOS `provision.install` (the provision command, never
  setup) and `aisland jenkins start` after asking install the `openjdk@N` the WAR names. (`tests/test_jenkins_java.py`,
  `tests/test_workspace_provision.py::test_aisland_never_needs_the_floating_openjdk_formula`)

## Docker, containers, trackers

- Colima shares change ONLY through `aisland docker start|restart --mount`: `$HOME:w` always
  re-stated first, symlinks resolved, a missing directory refused, a running engine never
  restarted for the user. (`tests/test_docker_service.py`) [containers]
- `DockerService.is_running()` is `colima status`'s exit code, never its text; everything
  else reuses it.
- **On Linux the container engine is the system's (user request 2026-10-29).** [linux-docker]
  `docker.docker_service()` is `LinuxDockerService` there (`DockerService`, Colima, elsewhere): `docker info`'s exit
  code says it runs; `start` refuses naming the install page or `sudo systemctl start docker`, `stop` leaves it
  running, `reset` and `--mount` are refused. aisland never installs, starts, stops or wipes it — that takes root
  (`tests/test_docker_service.py::test_on_linux_start_says_how_to_get_the_engine_and_installs_nothing` and its
  neighbours).

## Test-run statistics (`pytest_stats.py`)

Root `AGENTS.md` "Two Test Runs" (user ruling 2026-10-05). [test-stats]
(`tests/test_pytest_stats.py`, `tests/test_workspace_fast_run_default.py`)
- **The plugin records only in an aisland workspace (user choice 2026-10-29, release review option 1).**
  [test-stats-workspace] `locations.is_workspace`: from a checkout always; from a wheel a folder holding
  `workspace.toml` or `.gitmodules` (`locations.WORKSPACE_MARKERS`), or the one `AISLAND_WORKSPACE` names. A lone
  repository (`~/code/app`, whose "workspace" `find_workspace` says is `~/code`) gets nothing written beside it, and
  `aisland workspace test-stats on|off` refuses there before writing anything; `--machine-only` refuses where the
  workspace is no git repository (it used to say "not recorded" and write nothing)
  (`tests/test_pytest_stats.py::test_a_repository_of_its_own_gets_nothing_written_beside_it`,
  `tests/test_recording.py::test_outside_an_aisland_workspace_the_switch_is_refused_and_writes_nothing`).
- The plugin records a run only when pytest's rootdir is `<workspace>/<project>` (a submodule, or a
  project folder of a workspace without `.gitmodules`:
  `::test_a_project_folder_of_a_workspace_without_gitmodules_is_recorded`); never a `pytester` tmp project
  (`::test_a_run_outside_the_workspace_records_nothing`), and never a Jenkins build (`JENKINS_URL`
  set — the local Jenkins builds in the project folder itself, and its JUnit record is the CI half
  the report reads; `::test_a_jenkins_build_is_left_to_jenkins_own_record`).
- It never costs a run its result: standard library only (no `aisland.paths` import — it
  loads in every pytest start), one append per run, a failed write is one printed line
  (`::test_a_stats_folder_that_cannot_be_written_costs_one_line_never_the_runs_result`).
- **Recording can be turned off, never removed (user choice 2026-10-27: option B, shaped like `track|untrack`).**
  `aisland workspace test-stats on|off [PROJECT…] [--machine-only]` (`recording.py`): committed `[workspace]
  test-stats = false` (no project named: every project, those added later included) or a project's own
  `test-stats` where it differs; this machine `aisland.test-stats.default` / `aisland.test-stats.<project>.record`,
  which wins; a committed switch drops this machine's choice for what it names. The plugin reads it with the
  standard library (`pytest_stats.recording`, one `git config` call, a file it cannot read = recorded) and the report
  says a project is off with the command that turns it on (`tests/test_recording.py`) [test-stats-switch].
- The data folder is resolved like `paths.DATA_DIR`
  (`::test_the_records_live_in_aislands_data_folder`); after touching the entry point,
  `uv pip install -e ./aisland --no-deps` (`::test_the_plugin_is_declared_as_a_pytest11_entry_point`).
- A nodeid and CI's JUnit `classname::name` meet through `junit_address`, pytest's own
  mapping (`::test_the_junit_address_is_the_one_pytests_own_junit_report_writes`).
- The report proposes moves; nothing moves a test between legs but an edit to its
  `@pytest.mark.slow` (or a `FAST_SAMPLE`). Never deselect by history at collection time.
- **aisland's own fast run is PARALLEL — `-n 4` in `addopts`, `pytest-xdist` in `dev`** (user pick 3, 2026-10-28,
  over moving tests to the slow run: by the report's rule even every never-failed test moved left it over budget).
  Measured under load: 790 s in one process, 490 s over six workers, every test passing; the time is git, uv and
  builds in subprocesses. Four is a GitHub runner's cores; more than the machine has is slower, never wrong. The
  test-stats plugin records an xdist run as ONE whole run. A test file still passes alone (`pytest tests/x.py` runs it
  over the workers; `-n 0` runs it in one process). (`tests/test_workspace_fast_run_default.py::test_aislands_own_fast_run_is_parallel`)
- A project that registers `slow` sets `addopts = "-m 'not slow'"`
  (`tests/test_workspace_fast_run_default.py::test_the_real_workspace_runs_the_fast_run_by_default`).
- aisland's own pytest settings live in `[tool.pytest.ini_options]` alone, never a `pytest.ini`: pytest reads a
  `pytest.ini` INSTEAD of the table, while the scaffold (`jenkins.registers_slow_marker`) reads the table, so with
  both the CI files split a run pytest never splits. A test moves to aisland's slow leg only when
  `aisland tests report aisland` lists it
  (`tests/test_workspace_fast_run_default.py::test_aislands_own_pytest_reads_the_table_that_registers_slow`).

## Doctor

- A recurring environment skew becomes a `check_*` in `doctor.py`, listed in `CHECKS`, not
  just a note. Current checks: `opencv`, `huggingface_hub`, `mcp`, `docker-creds`,
  `colima-tls`, `jenkins-java`, `torch-ext-lock`, `tunnel-relay`, `tunnel-connect` (rules for project authors: root `AGENTS.md` "Known Environment
  Skews").
  [doctor]
  - Cheap, read-only, specific; a failure carries a copy-pasteable remedy
    (`tests/test_doctor.py::test_every_registered_check_returns_a_result`).
  - Imports run out-of-process (`_import_probe`); `run_doctor()` exits 0/1.
  - Each check names the invariant it mirrors (`opencv` ↔ `uv_config._ROOT_UV_BASE`).
  - A probe that cannot run passes with a note. Never boot a VM; VM state comes from
    `DockerService.is_running()`.
  - Not a venv linter: `pip check`'s nominal lines are not breakage.
- `torch-ext-lock` judges THIS venv's torch, read from its metadata (`_torch_version`, never an import): torch
  >= `_TORCH_OS_LOCK_SINCE` (2.14.1, the first measured) builds under an OS lock a killed process releases and leaves
  its `lock` file after every build, so the check passes saying so; an older torch fails on a lock older than two
  minutes, and only in this Python's own folder (`py<ver>_*`) — another Python's folder is another venv's
  (`tests/test_doctor_torch_extension_lock.py::TestATorchWhoseLockCannotBlock`,
  `::test_another_pythons_folder_is_not_this_venvs`).
- The `colima-tls` fix stays manual and doctor-guided (user ruling 2026-08-31, do not
  re-litigate). Never auto-install the network's CA or prompt for it.

## Bootstrap and `aisland setup`

- `setup.sh` (`tests/test_setup_sh.py`):
  - `WORKSPACE_PYTHON` (3.13) is matched EXACTLY. Raise it only when every installed framework
    ships wheels for the next version (TensorFlow first).
  - A missing interpreter comes from `uv python install`, never Homebrew.
  - Only POSIX syntax above the re-exec-under-bash guard.
  - pip is seeded with `ensurepip`; setup runs as `aisland setup -y`.
- `aisland setup` re-applies each managed project's effective source, `ref` included, so a
  checkout switched by hand goes back. Keep a branch with
  `aisland workspace configure source set <p> local --branch <branch> --machine-only`; after a branch switch run
  `uv pip install -e ./<p> --no-deps` to refresh the metadata.
- **Which projects a machine has is the `active` plugin (`activation.py`; user rulings 2026-10-23).** A project
  whose `workspace.toml` table has `active` is OPTIONAL, the value its default; without the key it is always part of
  the workspace (a `uv.toml` makes nothing optional). `workspace init --from` clones only projects WITHOUT `active`;
  `aisland setup` asks about each optional one (`resolve`: under `-y` only a project this machine never answered)
  and keeps the answer in `aisland.active.<project>.active` — aisland's key, so git's `submodule deinit` never erases
  it — then brings the folders in line (`reconcile`): active → init, or `git submodule add` from the file's url and
  branch when `.gitmodules` lacks it; inactive → deinit, never `-f`; a `pypi`/`github` source stays empty and records
  no choice. It writes what git reads from the effective value: `.gitmodules` `active = false` exactly for a
  default-off project, and `submodule.<name>.active`. Setup's upkeep keeps an optional project `.gitmodules` lacks.
  There are no `--activate`/`--deactivate` flags and no `aisland reconcile`: `aisland workspace configure active
  set|unset [--machine-only]` acts at once. `setup_git.sh` writes no activation key. (`tests/test_activation.py`)
- Every choice a project offers for a selection must be an extra of its own: discovery refuses anything else
  (`DeclarationError`, the file and the table's line), so `aisland setup` stops before it installs anything and every
  `aisland workspace configure` command stops with one `Error:` line (user ruling 2026-08-25).
  (`tests/test_selection.py::test_a_choice_that_is_no_extra_is_refused_naming_its_file_and_line`)
- An internal dependency spec must accept the sibling's declared version, extras included; a
  bare name makes no claim. Bumping a version means every sibling spec must still accept it.
  (`tests/test_workspace_dependency_specs.py::test_the_real_workspace_has_no_unsatisfiable_internal_specs`)

## Generated and committed workspace files

- Generated, gitignored, rewritten from discovery (never a hand list): root and per-project
  `pyrightconfig.json` (relative paths; kept out of git with the editable install's `**/*.egg-info/` through the
  project's own `.git/info/exclude`, never a `.gitignore` — setup leaves every project's `git status` as it was,
  user choice 2026-10-28, `git_exclude.py`, `tests/test_setup_leaves_projects_clean.py`),
  `project.bashrc`, `.gemini/settings.json`, `.mcp.json`, root `uv.toml`,
  `<workspace>.code-workspace`, and `.project.bashrc.completion` (`aisland completion
  install`). Never commit editor
  `extraPaths`. (`tests/test_pyrightconfig.py`, `tests/test_mcp_config.py`)
- **The workspace's `env.complete` and `env.example` are generated per machine from each project's own two files
  (user choices 2026-10-27: `env.complete` every variable, written by the agent; `env.example` the common ones,
  written by the user; never committed; a generated test per project; `--missing`).** [env-example]
  `env_example.read_project` refuses a line that is no comment (the files are copied to `.env`: they set nothing),
  naming its line; every project file is read before either workspace file is written. The render lists a variable
  once, under its OWNER — the first project in `env_example.project_order`, the editor file's dependency order, so the
  library that defines it (user choice 2026-10-27) — `also read by:` the others, each other wording kept, and leaves
  the `not for .env` names out. Setup writes them (a malformed project file is said, setup goes on); `init` seeds a
  missing `.env` from `env.example`, else `env.complete`; nothing ever edits an existing `.env` — `--missing` prints.
  The per-project test comes from `templates/test_env_example.py.template` (standard library + pytest only: a project
  never imports aisland; its output black-, isort- and flake8-clean at 120); a fix to it is made in the template and
  `aisland workspace env-test` re-run. Never write a project's `env.example` for the user.
  (`tests/test_workspace_env_example.py`, which runs the generated test on a project that passes and on ones that
  fail)
- `.gemini/settings.json` and `.mcp.json` keep ABSOLUTE venv paths (clients launch from any
  cwd). MCP discovery matches a `<pkg>.server:main` whose module imports `mcp.server`, never a
  class name.
- **aisland's MCP server is aisland's own, never navigaitor's (user ruling 2026-10-21).** [mcp-server] `aisland-mcp`
  (`server.py`, the `[mcp]` extra — never a base dependency) serves a CHOSEN set of tools (user choice 2026-10-21):
  reading `doctor`, `tests_report`, `editor_themes`, `editor_colors`, `sources`, `services_status`, `tunnel_list`;
  changing (`destructive_hint=False`) `editor_set_theme`, `editor_set_color`, `editor_unset` and
  `service_start|stop|restart` for ANY service of the workspace (user choice 2026-10-29, replacing jenkins and mlflow
  only) [mcp-services]: `name` is a string in the schema, checked when the tool runs against `registry.entries`
  (`no service 'x' (the services: …)`), and `--background` is passed only where the entry takes it
  (`tests/test_server.py::test_every_service_of_the_workspace_is_one_a_tool_controls_and_no_other`). Never a tool for what prompts, runs for minutes or
  deletes (`setup`, `clean`, `backups prune`, `* reset`, `jenkins init`, `source set`), and never every click
  command generated as tools. A tool returns ONE JSON object — the data its command prints with `--json`, from the
  same function; a service tool runs the `aisland` command itself (`_run_aisland`: this interpreter, `-P`, the
  workspace root). An anticipated refusal (a `ToolRefusal`, aisland's own `click.ClickException`) is raised as the
  SDK's `ToolError` (`_refusals_reach_the_client`): from mcp 2.1 any other exception reaches the client as `Error
  executing tool <name>` alone (`::test_an_anticipated_refusal_reaches_the_client_as_a_tool_error_in_its_own_words`).
  Not a copy of `_LLMSafeMCPServer` (no confluid here): a plain `MCPServer` with the
  resources/prompts handlers dropped. (`tests/test_server.py::test_the_tools_are_the_agreed_set`,
  `::test_a_reading_tool_says_it_only_reads_and_a_changing_one_that_it_destroys_nothing`,
  `::test_over_stdio_a_print_goes_to_stderr_and_the_tools_still_answer`)
- **Every MCP server is started with the workspace's environment (user request 2026-10-21).**
  With a `project.bashrc` at the root and bash on PATH, each entry of both client files is
  `bash -c 'source "$0" >/dev/null; exec "$@"' <root>/project.bashrc <server>`
  (`mcp_config._with_workspace_env`): a client otherwise starts the server with its own environment
  (vendorcli-mcp ran with `DATA_ROOT` unset). The init's stdout is discarded — the server's stdout is
  its JSON-RPC stream; never copy `.env`'s values into a client file (`"env"`), which would put its
  secrets there. Without either, the plain command. (`tests/test_mcp_config.py::TestTheServersGetTheWorkspaceEnvironment`)
- Root `mypy.ini` is committed; setup only APPENDS missing projects to `mypy_path` and never
  removes one. Never make it generated. (`tests/test_mypy_path_sync.py`)
- Root `uv.toml` = `_ROOT_UV_BASE` (drops `lightning`, `opencv-python`; keeps
  `opencv-python-headless>=4.12,<5`) + each ACTIVATED project's `uv.toml`. A private index
  lives only in its project's `uv.toml`; never commit the root file or put an index in the
  base. (`tests/test_uv_activation.py`)
  - Each activated project's `wheels/` folder holding a `wheels.yaml` is a `find-links` path in it, relative to the
    file (uv resolves it against the file's folder from anywhere below), so setup and every install in the workspace
    find the wheels without `install.py` (user choice 2026-10-23, every such folder, tracked or not). uv takes a
    find-links wheel over the index's of the same version (pip does not); an editable checkout still wins.
    (`tests/test_uv_activation.py::TestWheelsFolders`, end to end with the real uv offline)
- There is no `config.toml` and no `aisland config` (user ruling 2026-10-23): every committed setting is in
  `workspace.toml` — a project's in its table, a selection's choice under `[workspace]`. `uv.toml` never joins it:
  uv reads only a file of its own settings. [config]
- `<workspace>.code-workspace` is GENERATED and gitignored (user choice 2026-10-18), written by
  `editor_files.write_code_workspace` on every `aisland setup` and `aisland workspace configure source set|unset`
  (`--machine-only` included). **Each of setup's two editor-and-shell steps also runs alone (user request
  2026-10-26):** `aisland workspace code-workspace` (that function; an unchanged file → `is up to date`) and
  `aisland workspace bashrc` (`manager.link_project_bashrc`; a correct link → `already links to`, a regular file in
  its place → one `Error:` line, untouched) — the commands call setup's own functions, never a second writer
  (`tests/test_workspace_files_commands.py`). Folders: the workspace, each project with a checkout in
  dependency order (sorted by folder name first), then loggair's log folder when it exists.
  Settings and extensions come from `aisland/aisland/code-workspace.json` (strict JSON, no `folders`, no
  `python.analysis.extraPaths`): a shared setting changes there, never in the file. The template names the
  workspace `WORKSPACE`: its `${workspaceFolder:WORKSPACE}` and `WORKSPACE-bash` take the workspace's name, and
  this platform's profile keeps its shell when the machine has it, else the bash on PATH (in acme on
  this machine the output is unchanged). (`::test_another_workspace_s_settings_name_its_own_folder`)
  (`tests/test_code_workspace.py`) [folders]
  - The log folder is loggair's answer, asked out of process (`python -P -m loggair --json`,
    from the root, `.env` over the environment): never a path typed into aisland, never an
    import of loggair. (`::test_the_real_loggair_report_names_its_log_folder`)
  - The real workspace ignores it and does not track it
    (`::test_the_real_workspace_keeps_the_file_out_of_git`).
  - This machine's editor theme and colours (`aisland workspace theme|color`, `editor.py`; user request
    2026-10-21) live in `git config --local aisland.editor.*` ONLY — never `workspace.toml`, the template or any
    tracked file — and `write_code_workspace` lays them over the template's settings on every write. The colours
    are written unscoped in `workbench.colorCustomizations` (they hold whatever the theme); a theme is VS Code's
    `id || label`; an unreadable hand-edited value is skipped with a warning, never a failed setup. [editor]
    (`tests/test_editor.py::test_the_choice_lives_in_git_config_local_and_in_no_committed_file`,
    `::test_every_later_rewrite_keeps_the_choice`, `::test_the_colours_hold_whatever_the_theme`)
  - **A machine with no VS Code theme is a remote one: `theme set` keeps the name as typed, unchecked (user ruling
    2026-10-26).** Every desktop VS Code ships built-in themes; the VS Code server of a remote window ships none, and
    the window's themes are the local VS Code's. The command and the MCP tool's `note` say it is unchecked, `theme
    list` says where the themes are; a machine with any theme still refuses an unknown name. Never detect "remote"
    by an environment variable (an `aisland tunnel connect` shell has no `code`), and never ask the terminal's
    `$VSCODE_IPC_HOOK_CLI` socket for themes: it lists the remote's extensions only.
    (`tests/test_editor.py::test_without_vs_code_a_theme_is_kept_as_typed_and_unchecked`,
    `::test_where_vs_code_is_a_theme_is_still_checked`,
    `tests/test_server.py::test_without_vs_code_editor_set_theme_keeps_the_name_and_says_it_is_unchecked`)

## Project Sources Are Two-Layered

- Kinds `local` / `github` / `pypi`; branch on `REF_KINDS` / `DETACHED_KINDS`, never on a
  kind name. No `git`/`github` renaming: one word apart, and one deletes a checkout. [sources]
- **Committed: the project's table in `workspace.toml`; per machine: `aisland.source.<project>.<key>` (user rulings
  2026-10-22).** [sources] `source`, `version`, and `distribution` ONLY where it differs from the project's name — a
  git source's ref is the project's own `branch` (`set` without `--branch` keeps it; `--branch` writes it), and nothing
  the table already says is written again (a `path` equal to the name, a distribution equal to it). Per machine:
  `.source`, `.branch`, `.version`, a missing `.branch` being the committed one. No backward compatibility (user
  ruling 2026-10-22): an old `[sources.*]` table or one-key `aisland.source.<project>` is simply not read — never
  refused, named or converted by code. A failed `set` puts `workspace.toml` back byte for byte. The commands are the
  generic ones of `aisland workspace configure` (`configure.py`: a `Plugin`'s keys build `set`/`unset`/the listing;
  there is no `aisland source` group and no `source apply` — setup applies). (`tests/test_sources.py`, `tests/test_configure.py`)
  The effective one drives the checkout, the `.venv` and, through the checkout, the generated
  workspace file's folders. No CI file reads either. An override never reaches a tracked file.
- `local` installs `selection.editable_target`, the same target as setup.
  (`tests/test_sources.py::TestApplySource::test_local_installs_the_dev_extra_and_the_selected_frameworks`)
- Before a deinit: `pypi` resolves `distribution` (persisted only when it differs from the name); `_pypi_provenance` requires
  `github.com/<owner>/` metadata, the owner of the project's own remote [github-owner]; `_remote_ref_exists` checks a `github` ref. Unreachable =
  warn, never reject.
- Never force `git switch` / `submodule deinit`. Report the remedy for the ACTUAL blocker
  (`_deinit_remedy`: dirty tree vs stale gitlink) and roll the config back.
  (`::TestSetProjectSourceRollback`)
- `activation.reconcile` folds in `detached_projects()`. The scaffolder seeds a detached dep
  from the registry's `distribution`
  (`::TestScaffoldAsksPypiNotTheSource::test_github_dep_survives_its_own_empty_directory`).
- The remote comes from `.gitmodules`; `pip_vcs_url` rewrites `git@host:org/repo`.

## Project mirrors (`aisland workspace configure mirror`)

User choice 2026-10-16: an aisland command, submodules only, run by `/push-all` and `/ship`.
[mirror] (`tests/test_mirrors.py`)
- The primary is the `.gitmodules` remote; the mirror (remote `mirror`) only receives. A sync
  copies `dev/main`, `main` (always) and every other branch of the local checkout, each at the PRIMARY's commit
  (`ls-remote origin`), and the tags — never a local commit; a local branch the primary lacks is reported `absent`,
  a primary-only branch is not copied (user request 2026-10-28;
  `TestSync::test_the_mirror_follows_the_primary_not_unpushed_local_commits`,
  `::test_a_local_dev_branch_is_copied_at_the_primarys_commit_after_the_two_long_lived_ones`,
  `::test_a_local_branch_never_pushed_is_reported_and_not_pushed`, `::test_a_branch_only_on_the_primary_is_not_copied`).
- Never force, never delete, never overwrite a tag: a moved branch and a differing tag are
  REFUSED with the remedy, a mirror-only ref is left (`TestSync::test_a_branch_the_mirror_moved_is_refused_and_left_alone`,
  `::test_a_tag_that_differs_on_the_mirror_is_refused_not_overwritten`,
  `::test_a_branch_or_tag_only_on_the_mirror_is_never_deleted`). Never add a `--force`.
- Committed: the project's `mirror` in `workspace.toml`; per machine `aisland.mirror.<project>.mirror` (user rulings
  2026-10-22: the plugin's key pattern, no backward compatibility — an old `[mirrors.*]` table or one-key setting is
  simply not read); the commands are `configure.py`'s generic ones and the action `aisland workspace mirror sync`. Setup re-applies
  the remote. Only a remote marked `remote.mirror.aislandManaged` is changed or removed
  (`TestSetAndUnset::test_set_refuses_to_take_over_a_remote_it_did_not_create`).
- Network calls run with `GIT_TERMINAL_PROMPT=0` (`TestSync::test_network_calls_never_wait_on_a_password_prompt`);
  a project without a mirror is never contacted.
- `/push-all` and `/ship` call `aisland workspace mirror sync` last; change its exit status or wording and
  those workflows together.

## System tools (`aisland workspace provision`, `provision.py`)

User choices 2026-10-23: no configure plugin (which projects a machine has decides its tools), aisland's own tools in
the package. (`tests/test_workspace_provision.py`)
- A project declares `[tool.aisland.sys-dependencies]` `brew`/`userspace` lists; discovery refuses an unknown key, a
  value that is no list of names and a userspace tool `installers.USERSPACE_TOOLS` lacks, naming the file and the table's
  line — before anything is installed.
- aisland's own tools are `provision.AISLAND_TOOLS`, never a table in its `pyproject.toml` (a wheel workspace has none
  to read; `::test_aislands_pyproject_declares_no_tools_of_its_own`).
- `aisland workspace provision` lists this platform's tools, one row each (who needs it, installed or not — Homebrew's
  list, a tap-qualified name by its last part; a userspace tool by its binaries in `BIN_DIR`), `--json` the same
  rows; `install` installs every one, aisland's own and Jenkins' Java included. There is no `aisland provision`.
- **aisland installs its own tool only when a feature first needs it, after asking (user choice 2026-10-28, option 1
  of the release review).** `aisland setup` runs `provision.install(root, aislands_own=False)`: the projects'
  declarations only, no Java. A feature that finds its tool missing calls `provision.ask_to_install(name)` (the
  supervisor's process-compose, `docker start`'s colima and docker, `swiftbar start`'s SwiftBar) or asks itself
  (`jenkins` before downloading its WAR, `JenkinsService.ensure_war_asking`, and before installing Java,
  `java_asking`, whose refusal is the WAR's own `refusal()`); `aisland check` without `act` fails that gate naming
  `install_command`. A question is asked only when stdin and stdout are a terminal (`provision.agree`); without one, or
  on a "no", the command refuses with the line that installs the tool and installs nothing — a script, CI or an MCP
  call never installs software as a side effect. Never install one of aisland's tools without that question
  (`tests/test_installs_on_demand.py`) [on-demand-tools].

## What the projects declare (`discovery.py`)

The first step of the workspace redesign (`TASKS.md`, user rulings 2026-10-21/22). [declarations]
(`tests/test_discovery.py`)
- **Two stages, and a project never imports aisland.** STAGE 1 (`discover`) reads every project's
  `pyproject.toml` through `projects.workspace_folders` — nothing installed: `[tool.aisland.selection.<name>]` (the
  table with `question` DEFINES it; every project's `choices` are its menu and each must be an extra of its own),
  `[tool.aisland.service.<name>]` (`command`, `port`, `ready`, `needs`) and the entry points a project declares in
  `[project.entry-points."aisland.plugins"]`. STAGE 2 (`load`) loads only those, from the declaring project's own
  distribution, once installed; a plugin is any object with a `kind` (`PLUGIN_KINDS`) and that kind's methods
  (`_KIND_METHODS`) — never a subclass of an aisland type, so a project gains no dependency on aisland
  (`::test_an_installed_plugin_is_loaded_and_has_its_kind`,
  `::test_an_entry_point_of_a_package_outside_the_workspace_is_not_loaded`).
- Every refusal is a `DeclarationError` naming the file and the line of the TABLE (`Where`): a selection or service
  defined twice, a choice that is no extra, a missing command, an unknown setting, a value of the wrong shape, a
  plugin missing a method. NOT refused: choices for a selection no project defines (`Discovery.undefined`, shown —
  a workspace without the defining project still sets up), an empty project folder, a plugin not installed yet
  (`::test_choices_for_a_selection_no_project_defines_are_reported_not_refused`).
- A new plugin kind joins `PluginKind` and `_KIND_METHODS` together, with a contract test.

## Selections (`aisland workspace configure <selection>`, `selection.py`)

User rulings 2026-10-23. [frameworks] (`tests/test_selection.py`)
- **aisland names no selection but its own `extras`.** The others are DISCOVERED (`selection.axes`, over
  `discovery.discover`): the project whose `[tool.aisland.selection.<name>]` has `question` defines it, every project's
  `choices` are its menu. Never hard-code a selection's name or question in aisland; a workspace without the defining
  project has no such selection, and the others' choices install nothing
  (`::test_choices_for_a_selection_no_project_defines_offer_nothing`). `extras`' menu is every extra a project
  defines but a self-referential one (structural, never a name match).
- Committed: `[workspace] <name> = [...]` in `workspace.toml`; this machine: `aisland.<name>.selected`
  (space-separated, `""` = none), which wins. No backward compatibility: an old `[tool.aisland] frameworks`, a
  `config.toml` key or an `aisland.frameworks` setting is simply not read.
- A project installs its menu ∩ the effective choice, in its MENU's order, each extra once across selections; once
  `extras` is chosen it owns `dev` (`::test_a_resolved_extras_selection_owns_dev`). CI reads the committed layer
  only (`::test_ci_never_installs_this_machines_choice`); the generated CI files are byte-identical to the
  `config.toml` era's for acme (measured 2026-10-23, all 78 files, released and unreleased).
- Unset ≠ empty: setup asks on unset (the selection's own question, no hint line), `-y` with nothing chosen takes
  everything, `--all` takes every extra; an answer is committed. `set` takes the WHOLE list and reinstalls the
  offering projects at once (no `--no-apply`); an unknown name is refused before anything is written; the exit code
  is the number of failed installs. `setup` has no `--framework`/`--extra` (choose with `set` first).
- **An extra a project marks in `[tool.aisland] manual-extras` is installed only when named (user choice
  2026-10-23).** A blank answer, `-y` and `--all` leave it out; the menu marks it `(only when named)`; one project
  marking a name is enough, since a selection is one name for every project. Discovery refuses a marked name that is
  no extra of the project, and a value that is no list, naming the file and the line. A declaration, never a
  structural guess. (`tests/test_selection.py::test_a_blank_answer_leaves_out_an_extra_installed_only_when_named`
  and its neighbours)
- A project that needs one framework outright (`raidar-ultralytics`) keeps it as a base dependency and offers no
  choice. Never fake an extra.

## Generated CI (`aisland jenkins scaffold`)

A bare `::` pin in this section is in `tests/test_jenkins_scaffold.py`.

- **CI installs what is RELEASED (user ruling 2026-09-26, do not re-litigate).** The runner
  files ask PyPI per internal dep (`released_on_pypi`: a final, non-yanked version whose
  metadata points at `github.com/<owner>/`, the owner of the dep's remote): released → no install line; else
  `git+https://github.com/<owner>/<dep>.git@main` [github-owner]. They never read a source or `ref`;
  `aisland workspace configure source set|unset` regenerates no CI file; an unreachable PyPI stops the scaffold
  (`ReleaseProbeError`). Tests stub PyPI with the autouse `released_packages` fixture.
  (`tests/test_sources.py`: `TestScaffoldAsksPypiNotTheSource`, `TestSourceChangesRewriteNoCI`,
  `TestReleasedOnPypi`) [sources]
- **A project that does not live on GitHub never names one in its CI files (user request 2026-10-28; a company's
  repository must not reveal the workspace it is developed in).** For a project whose folder is not in
  `sources.project_repositories` (its remote is off github.com), the unreleased dependencies are never git-URL lines:
  they come from the wheels it commits, through the extra `aisland wheels build --extra` writes
  (`wheels_extra.remembered_extra`), added to the install line; a dependency no wheel of that extra holds — or no
  such extra — refuses the scaffold, naming the dependencies and `aisland wheels build <project> --extra <name>
  --commit`, before any file is written (`jenkins._unreleased_from_wheels`). A released dependency needs nothing.
  (`tests/test_github_owner.py::test_a_project_off_github_installs_its_unreleased_dependencies_from_its_wheels_extra`
  and its neighbours)
- **`Jenkinsfile.local` installs nothing (user instruction 2026-09-13).** `Initialize` only
  checks, with `python3 -P`, that the project's DISTRIBUTION records this checkout, else
  stops naming `aisland setup`. A new dependency fails until `aisland setup`; never add an
  install back. The orchestrator is `agent none`.
  (`::TestTheLocalPipelineInstallsNothing`, `tests/test_orchestrator_executor.py`) [local-pipeline]
- **The local orchestrator runs every night at 01:00 (`NIGHTLY_CRON`, user ruling 2026-10-06)**, so
  every project's slow run lands in Jenkins' record for `aisland tests report`. The timer is in
  BOTH the job's properties (armed at seeding) and the pipeline's `triggers` (a pipeline without
  one clears the job's timer on its first run); the GitHub-backed orchestrator gets none.
  (`tests/test_orchestrator_executor.py::test_the_local_orchestrator_runs_every_night_at_one`,
  `::test_the_ci_orchestrator_keeps_no_timer`) [test-stats]
- **The nightly runs are reported by Jenkins, the test moves made by Claude (user choice 2026-10-06).**
  `<workspace>.nightly-check` (seeded by a local init only; `NIGHTLY_CHECK_CRON`, weekdays 09:30,
  timer in properties and `triggers`) runs `aisland tests nightly`: every TIMER run of the
  orchestrator since the previous report (the newest `aisland/data/nightly/<date>.md` dated before
  today) — a manual run is never a night — written down with each failed project's stage, tests
  and gate lines; a notification and exit 1 when a night failed, had no run, is still building or
  Jenkins did not answer. The report states what it found, never a guessed cause. Moving tests
  needs judgment and stays with the Claude app's scheduled task `acme-nightly-check` (09:45),
  which reads the report. (`tests/test_nightly.py`)
- **A manual build is the fast run; the nightly one adds the slow tests (user ruling 2026-10-06).**
  A project with a `slow` marker gets a `SLOW_TESTS` parameter (default false) in
  `Jenkinsfile.local`, and its Slow Tests stage runs only `when` it is true; `Jenkinsfile` and
  `ci.yml` keep the slow leg on every push. The orchestrator passes
  `SLOW_TESTS: slowTests()` — true for its timer's run (`TimerTrigger$TimerTriggerCause`) or a
  tick on its own `SLOW_TESTS` — to the projects with the marker only. Jenkins drops a parameter
  a job does not define, so a seeded job (`local_job_config`) and the orchestrator define it
  before their first run. (`tests/test_jenkins_scaffold.py::TestTheSlowMarkerSplitsTheTestInvocations`,
  `tests/test_orchestrator_executor.py::test_the_nightly_run_asks_for_the_slow_tests_and_a_manual_one_does_not`,
  `::test_a_seeded_local_job_accepts_the_slow_switch_before_its_first_run`)
- **A long stage of `Jenkinsfile.local` has a time limit (user choice 2026-10-15).** `STAGE_LIMITS`
  (Unit Tests 15, Slow Tests 60, Verify Examples 15, Verify Notebooks 15 min — about twice the
  longest healthy run of 2026-10-10..15) is the ONE table; each such stage's steps run inside the
  `limited` helper the file defines (`_LIMITED_HELPER`), which past the limit FAILS the stage with
  `TIMED_OUT_MESSAGE` — the stage and the limit named — and rethrows an abort clicked before it.
  Initialize and the quality gates get none. **`ci.yml`'s jobs get `RUNNER_LIMIT_FACTOR` (3) times their stage's
  limit (`runner_limit`, user pick 2026-10-28 over waiting to measure a runner: a hung job held one for GitHub's six
  hours)**; the push `Jenkinsfile` still gets none (`TASKS.md`). `release.yml`'s jobs: tests 45, build 15, publish 10
  (`tests/test_packaging.py::test_every_release_job_has_a_time_limit`). Each pytest step first removes the files it writes
  (`rm -f test-report.xml coverage.xml`, `rm -f slow-test-report.xml`, both Jenkins templates): a
  pytest killed before its report leaves the previous build's for the `junit` step
  (annotaide.local #37 recorded #36's 2 859 cases). `aisland tests nightly` quotes the timed-out
  line (`nightly._TIMED_OUT`). Raise a limit in the table, never in a generated file.
  (`::TestALongStageHasATimeLimit`,
  `tests/test_nightly.py::test_a_timed_out_stage_is_reported_with_its_limit`) [stage-limits]
- Deploying a template change to a running Jenkins: `aisland jenkins scaffold --force`, then
  POST each job's `/job/<name>/config.xml` with a crumb and
  `Content-Type: application/xml; charset=utf-8` (no charset → HTTP 500). A running build
  keeps its script. `aisland jenkins init --local` also works but stops Jenkins and prompts.
- The pre-install closure follows what CI installs: the root's base deps + its CI extras; a
  transitive dep's base deps + only the extras its requirement names (never every extras
  group); specs via `packaging.Requirement`. (`::TestTheInternalDepClosureFollowsWhatCIInstalls`)
  [closure]
- Every `uses:` in `ci.yml` comes from `GH_ACTIONS` (moving major tags on node24; `setup-uv`
  stays `@v7`). The hand-written `*/.github/workflows/release.yml` are bumped in the same
  change. (`::TestTheWorkflowActionsRunOnNode24`)
- A registered `slow` marker splits the test run in all three templates (exit 5 tolerated);
  an unmarked project's output is byte-identical. (`::TestTheSlowMarkerSplitsTheTestInvocations`)
- `playwright` in a `dev` extra: setup and the runner files fetch chromium (`--with-deps` in
  `ci.yml`); `Jenkinsfile.local` fetches nothing. (`tests/test_playwright_browser.py`,
  `::TestAPlaywrightDevDependencyInstallsItsBrowser`) [playwright]
- Verify Examples runs `examples/*.py` and `examples/*/run.py`.
  (`::test_scaffold_emits_directory_example_runner`)
- A declaration a project already makes (a marker, a dependency) is the opt-in; never add an
  aisland knob for it. After a header change, re-scaffold every project (root Generator
  Provenance rule).

## The agent harness (`harness.py`, `harness/`)

[harness] (`tests/test_harness.py`, `tests/test_packaging.py`)
- `harness/` holds ONLY what every aisland workspace shares: no workspace's project names, private names or domain
  rules (`::test_the_harness_names_no_project_of_the_workspace_it_was_written_in`). A workspace's own rule goes in its
  `AGENTS.md` below the import line, never here.
- A command cites a rule as `aisland rules → "<bold heading>"`, and `harness/AGENTS.md` must hold that heading
  (`::test_every_section_a_command_cites_is_in_the_rules`): rename a rule and its citations together.
- From a checkout, links; from a wheel, copies (`locations.FROM_CHECKOUT`). A copy is replaced only while its
  fingerprint is the one `installed.json` recorded, or it equals aisland's; anything else is kept and named — never
  overwritten, never deleted. A file in `.agents/` aisland does not ship is never touched, and an existing workspace
  `AGENTS.md` is never edited (`::test_an_edited_copy_is_kept_and_named`,
  `::test_a_command_of_the_users_own_is_never_touched`, `::test_an_agents_md_without_the_import_is_left_and_named`).
- Every installed file is ignored by its own path, never by its folder, so a workspace's own command stays
  committable (`::test_the_installed_files_are_ignored_and_the_users_own_are_not`).
- Every project folder (a direct subfolder with a `pyproject.toml`) gets `.claude/commands` too — Claude Code reads
  only the folder a session starts in — excluded through ITS repository's `.git/info/exclude`, never a tracked
  `.gitignore` (`::test_a_session_started_in_a_project_finds_the_commands_too`,
  `::test_a_projects_link_is_excluded_in_its_own_repository_and_changes_no_tracked_file`).
- Setup installs it before provisioning; the commands that commit carry `model: sonnet`
  (`::test_a_command_that_commits_runs_on_sonnet`). `harness/` is no package: a new file kind there joins
  `[tool.setuptools.package-data]` (`tests/test_packaging.py::test_the_package_holds_the_templates_and_the_git_scripts`).

## Shell completion

- `aisland completion install` writes all three families into `.project.bashrc.completion`;
  `setup.sh` calls it in one line. Never move it back into shell. (`tests/test_completion.py`)
  [completion]
  - liquifai apps: `install_for_apps`, in-process. click apps: `_<PROG>_COMPLETE`, bash
    guarded on `BASH_VERSINFO >= 4.4` — `aisland` (`NON_LIQUIFAI_CLIS`, its only row) and every script a project
    lists in `[tool.aisland] click-commands` (`completion.workspace_click_clis`, read by discovery: never import a
    script to tell — measured 2 min importing every project's CLI, and a `main` wrapping a click group is a plain
    function; user request 2026-10-28). Never name a workspace CLI in aisland. argcomplete: no member; never add a
    `streamstudio` row. (`tests/test_completion.py::test_a_projects_declared_click_command_is_found_without_importing_it`)
  - liquifai is never a dependency of aisland, not even behind an extra: a venv without it has no liquifai app, so
    the install hands nothing over and writes the other families; the default shell is `completion.login_shell`
    (`$SHELL`'s name, else bash), never liquifai's (`tests/test_completion.py::test_without_liquifai_the_other_clis_are_still_installed`
    and its neighbours).
- **An empty word after an aisland command with nothing left to fill offers its options** (user request
  2026-10-21; click by itself waits for a `-`). `cli._Command.shell_complete` completes `""` as `"-"`; every
  group is a `cli._Group` (`command_class = _Command`, `group_class = _Group`), so a command added with
  `@<group>.command` gets it without a `cls=`. A group still offers its commands only; an argument still to
  fill comes first. A new group class derives from `_Group`, never `click.Group`
  (`tests/test_cli_option_completion.py`).
  - `--target-rc` appends marked blocks only.
  - Pruning stands down when discovery finds no liquifai app; program lookup is venv-first.
  - **The generated block LEAVES a non-interactive shell at once** (`_INTERACTIVE_ONLY`, the
    block's first line after its provenance header; 2026-10-20): every line below it RUNS its
    CLI to get the script (`eval "$(_X_COMPLETE=..._source X)"`), so a non-interactive shell
    that reads the file executes them — and with `BASH_ENV` pointing at the file that sources
    this one, click's own `bash --norc -c 'echo "${BASH_VERSION}"'` probe reads it and runs the
    CLI again, which probes again. Measured: 51 processes spawned from one `aisland` in 8 s
    against 0 with the guard; unguarded it filled this machine's process table (2931 processes,
    nothing could fork) and is SELF-SUSTAINING — killing a slice lets the survivors refill, so a
    clear is `while` over `pkill -9 -f BASH_VERSION; pkill -9 -f 'venv/bin/aisland$'` (anchored:
    the bare, argument-less invocations are the bomb's; `aisland tunnel …` is a person's).
    Never add a line to the block that runs a program before the guard.
    (`tests/test_completion.py::test_the_block_does_nothing_in_a_NON_interactive_shell`)

## `aisland clean` / `aisland backups`

- Clean MOVES into `aisland/data/backups/<project>/<stamp>/`, never deletes (never
  `git clean -Xdf`). (`tests/test_clean.py`) [clean]
  - ONE project per run (never `--all`); the workspace root is refused by name and path; the
    project dir must be its repo's root.
  - An entry containing the store is split (`_split_around`); the sweep deletes only
    `STAMP_FORMAT`-named directories; symlinks are measured, never followed.
- Restore moves and consumes the backup: a different copy is skipped (exit 1; `--force`
  asks), an identical one is reported `already` and dropped. `_same_content` compares bytes,
  never size+mtime (`::test_a_hand_copied_tree_is_recognised_despite_new_mtimes`). Manifest
  paths are validated (`_safe_rel`). `backups prune` confirms first.

## `aisland wheels` (`wheels.py`)

User choice 2026-10-08: an aisland command, only the named project's closure, both checks.
[wheels] (`tests/test_wheels.py`)
- A wheel is built from `git archive HEAD` with `SOURCE_DATE_EPOCH` = the commit's time, never
  from the checkout: a stale `build/lib` leaks deleted files into it, and the build's clock makes
  every rebuild a new file (`TestEndToEnd::test_a_stale_pypi_release_breaks_the_import_and_is_named`,
  `::test_once_pypi_has_the_release_everything_imports`).
- A project to ship with uncommitted changes is refused, naming its files; one with no checkout,
  naming `aisland workspace configure source set <project> local` (`TestRefusals`). Nothing is built after a refusal.
  The one exception is `--force` (user choice 2026-10-20): a dirty project is then built from its
  working tree (`export_worktree`: `git ls-files --cached --others --exclude-standard`, never the bare
  folder, so an ignored `build/lib` stays out), its commit written `<sha>+uncommitted` and its
  `git status --porcelain` lines in `BuiltWheel.uncommitted` and the report; a clean project is still
  built from `git archive HEAD`; `--force --commit` is refused before any build
  (`TestTheRunWithFakedBuilds::test_force_builds_a_dirty_project_from_its_files_as_they_are_and_says_so`,
  `TestRefusals::test_force_with_commit_is_refused_before_anything_is_built`,
  `TestEndToEnd::test_force_ships_the_working_tree_of_a_dirty_project_and_checks_the_dirty_target`).
- "On PyPI" = `pypi_state`: metadata naming the project's GitHub owner and a file not yanked, pre-releases INCLUDED. Never
  replace it with `released_on_pypi` (final only, CI's question). An unreachable PyPI stops the
  run (`TestPypiState`).
- The empty venv runs from a scratch folder, so the root `uv.toml` is never read; the target's
  own `uv.toml` is passed with `--config-file` (`TestInstallAndImport`).
- The exit status follows the install and the imports only; *behind* (a file differs from PyPI's
  wheel) is reported, never an exit 1 (`TestCli`).
- `--no-check` (user choice 2026-10-20) skips BOTH checks — no PyPI wheel downloaded or compared, no target
  wheel, no empty venv — and nothing else: the PyPI lookup, the refusals, the wheels and the generated files
  are those of a checked run, byte for byte. The report says `checked: false` and both sections read "not
  compared/Not checked (--no-check)"; the exit is 0. `--no-check --commit` is refused before any build: the
  folder is committed only after a passing check
  (`TestTheRunWithFakedBuilds::test_no_check_builds_and_writes_the_wheels_and_neither_compares_nor_installs`,
  `TestRefusals::test_no_check_with_commit_is_refused_before_anything_is_built`).
- Every module is imported, not only the packages (`__main__` skipped). A package that fails, or a
  module missing a workspace project's module, is a break; a deeper module missing what no
  workspace project ships (an optional extra's) is a warning, never an exit 1
  (`TestTheRunWithFakedBuilds::test_a_package_or_a_module_missing_a_workspace_projects_module_breaks_any_other_module_warns`,
  `TestEndToEnd`). Only a `ModuleNotFoundError` of such a module is an optional extra's: any other
  error of a deeper module (a `TypeError` from an outdated release's missing keyword) is a break when a
  published project is behind, blamed by `attribute_failure` on the innermost traceback frame in a behind
  project's code, else the behind projects whose checkout-only code uses a name the message quotes, else
  every behind project; nothing behind, nobody blamed (user choice 2026-10-19;
  `::test_an_error_naming_no_module_breaks_and_blames_the_behind_project_whose_code_has_the_name`,
  `::test_a_missing_module_no_workspace_project_ships_stays_a_warning_even_with_a_project_behind`).
- What is built is a scope (`WheelsScope`): the default ships what PyPI lacks, `--include-published`
  every needed project with a checkout, `--all` every workspace project with one; a covered project
  without a checkout is reported as not built, a dirty one refused in every scope
  (`TestTheRunWithFakedBuilds`, `TestRefusals::test_all_refuses_a_project_the_target_does_not_need_with_uncommitted_changes`).
- `install.py` (a copy of `wheels_install.py`, one template for every project) names each chosen
  wheel by its FILE — pip takes PyPI's same-version wheel over one it only finds through
  `--find-links` (`tests/test_wheels_install.py::test_pip_takes_the_indexs_wheel_over_one_it_finds_only_through_find_links`,
  `::test_the_script_installs_the_folders_wheel_over_the_indexs_of_the_same_version`). The empty-venv
  check runs that script (`TestInstallAndImport`). Never go back to `--find-links` alone.
- `wheels.yaml`'s `install:` values are the user's: a rerun keeps them, an unreadable file stops the
  run at its line before any build (`TestWheelsYaml`, `TestRefusals`). `wheels_install.read_wheels` is
  the one reader of that file, for both sides.
- The folder is ignored by git (`wheels/.gitignore`: `*`) unless `--commit`, which writes `*.zip` and
  commits `wheels/` ITSELF — that path only, only after a passing check, never the zip; a tracked folder
  without `--commit` is reported with its `git rm -r --cached`, never untracked by the command
  (`tests/test_wheels_zip.py::TestGit`).
- `aisland wheels` is a group (`build`, `zip`, `install`); an unknown first word is `build`'s project,
  `--install ZIP` the group's short form. The zip runs with plain `python` (`wheels_unzip.py`, standard
  library only — the other machine has no aisland), refuses another project's folder before unpacking,
  and is byte-identical for an unchanged folder; `install` refuses the workspace's `.venv`
  (`tests/test_wheels_zip.py`).
- `--include-project` (user request 2026-10-21) builds the target's OWN wheel under the same rules (its commit;
  dirty refused unless `--force`, its own `wheels/` never counting as dirty), kept OUT of `built`
  (`WheelsReport.project_wheel`) so every count and table of what it needs is unchanged, and listed LAST in
  `wheels.yaml`. `install.py` treats the entry whose `project:` is the file's own as the project, never as a
  dependency: while its `install:` is true it is installed in place of `-e ..` whatever `--all/--needed/--only`
  chose; `--no-project` drops both. The check installs and imports that wheel, never a second build. A folder
  that installs its own wheel (`installs_own_wheel`) zips with `STANDALONE = True`: the zip also runs in a
  folder without `pyproject.toml`, but still refuses another project's `pyproject.toml` and a `wheels/` holding
  another project's wheels. Without the flag every file is byte-identical
  (`TestTheProjectsOwnWheelInTheFiles::test_without_it_every_file_is_what_it_was`,
  `TestEndToEnd::test_include_project_ships_the_project_as_a_wheel_that_installs_from_a_folder_of_no_project`,
  `tests/test_wheels_zip.py::TestRunningTheZip`).
- `--bundle` (user choice 2026-10-23) merges the target's own wheel and the needed separate wheels into ONE wheel,
  `<project>-bundle`, in `wheels/bundle/` (`wheels_bundle.py`): one `.dist-info` (pip refuses more), so pip knows
  only that name — the user accepted that `pip show <dependency>` finds nothing and `__version__` falls back.
  Its requirements come from each bundled project's `pyproject.toml` (a bundled project's requirement inlined
  under its marker, the target's extras kept), never from METADATA; an unsatisfied requirement, a non-pure wheel,
  a path with two contents, an entry point with two targets and `dynamic` dependencies are REFUSED, never resolved.
  The target is held to its own wheel's rules; the bundle is checked alone (`install_bundle_and_import`) and its
  failures exit 1 and block `--commit`. Without the flag every file is byte-identical and a stale bundle is removed;
  the zip never holds it (`tests/test_wheels_bundle.py::TestTheRunWithABundle::test_without_bundle_every_file_is_what_it_was_and_an_old_bundle_goes`,
  `::test_the_zip_leaves_the_bundle_out`, `TestEndToEnd::test_the_bundle_installs_alone_with_plain_pip_and_pip_knows_only_its_name`).
- `--extra NAME` (user request 2026-10-27) writes the extra NAME — every built wheel's distribution — and a
  `[tool.uv.sources]` entry per wheel, applied only under that extra (`{ path = "wheels/<file>", extra = NAME }`),
  into the target's `pyproject.toml`, so `uv pip install -e ./<project>[NAME]` installs it editable with every wheel
  and takes a wheel rebuilt under its version (`wheels_extra.py`). Both pieces sit between marker lines a rerun
  replaces, and nothing outside them changes; the run REFUSES before any build an extra NAME missing from
  `[tool.aisland] manual-extras` (setup would install the wheels over the checkouts), a hand-written extra NAME or
  source for a built wheel, and a broken marker, each naming its line; `--commit` commits `pyproject.toml` with the
  folder and refuses one with changes of its own. It is a flag, never tied to `--include-project`: aisland's own
  `--include-project` run would put the extra into its PyPI metadata. **The block is remembered (user request
  2026-10-27):** a build without `--extra` and without `--include-project` finds the block (`remembered_extra`, its
  first line names the extra) and keeps it current, so the regenerate command names `--extra` only with
  `--include-project`; with `--include-project` and no `--extra` the block is left and the report says so; another
  name beside the block is refused naming its line. A project without the block: the file and every generated
  file are byte-identical (`tests/test_wheels_extra.py`; the uv behaviour, offline with the real uv:
  `::test_uv_installs_the_project_with_the_wheels_of_its_extra_and_takes_a_rebuilt_one`;
  `TestTheBuildRemembersTheExtra`). [wheels]
- **`aisland wheels prune` / `reset` (user request 2026-10-27: keep the wheels a branch or tag holds, aisland
  force-pushes, other machines can be fixed)** (`wheels_prune.py`, `tests/test_wheels_prune.py`) [wheels-prune]:
  - What goes is exactly the `wheels/**.whl` blobs no tip holds — this clone's branches and tags, origin's branches
    (`::test_a_wheel_only_this_clones_branch_holds_is_kept`); every kept tip's tree is checked unchanged after the
    rewrite. Without `--apply` nothing changes (`::test_without_apply_it_names_what_goes_and_changes_nothing`).
  - The strip is git-filter-repo with `--prune-empty never --prune-degenerate never --preserve-commit-hashes`, so it is
    a function of each commit and `reset` re-runs it to land on origin's ids
    (`::test_reset_brings_another_clone_onto_the_pruned_history_keeping_its_work`). Never drop emptied commits or
    rewrite messages, and never replace `reset` by a rebase.
  - `--apply` refuses tracked changes, a branch ahead of or behind origin's and a differing tag, naming each, before
    anything changes; it bundles every ref first and prints the restore command. The push is ONE atomic push leased
    per ref on the fetched sha, never a `+` refspec, which overrides the lease
    (`::test_a_push_made_meanwhile_refuses_the_whole_push_and_the_backup_restores`).
  - origin's `refs/wheels-prune/map` is a chain, one commit per prune (`stripped-blobs`, `commit-map`), text only;
    `reset` takes each prune the clone still reaches with that prune's own list (`::test_reset_takes_every_prune_it_missed`).
    Its name carries no workspace or tool name (a Bitbucket project must not reveal them).
  - The root's gitlink moves only to the rewritten id of the commit it already records, committed alone from a
    scratch index — never to a newer commit, so an untracked project stays unrecorded
    (`::test_the_root_records_the_rewritten_twin_of_its_commit_alone`).
  - Reflogs expire for every ref but `refs/stash`, symbolic refs included (`origin/HEAD`'s reflog kept the old
    history alive); a detached HEAD is carried through `refs/wheels-prune/head`.
- **A project whose setuptools discovery would take `wheels/` for a package is refused before anything is built
  (user choice 2026-10-27: refuse with the fix, never hide the folder or edit the project).** `wheels_folder_problem`:
  a setuptools project (or no `build-backend`) without `setup.py`, with no `packages` list or `py-modules`, and either
  automatic discovery in a flat layout (no `src/`) — two top-level packages stop its build — or a `packages.find`
  reaching `wheels` (`where` holding `.`, no `include` or one matching it, namespaces on, no `exclude` matching it) —
  the folder ships in its wheel. The message names the project's own package folders in the `include` setuptools
  recommends (`your_package` when it has none); that advice, pasted in, makes the build pass
  (`tests/test_wheels_folder_discovery.py`).
- TAB after `wheels build` / `wheels zip` / the short form `wheels` offers the projects (`cli._complete_projects`:
  checked out here; `zip` only those with a `wheels.yaml`; the group's commands first). It reads folders and
  `pyproject.toml` only and an unreadable workspace offers nothing (`tests/test_wheels.py::TestShellCompletion`).
- Tests reach no network: the end-to-end tests run the real uv with `UV_OFFLINE=1`, a fresh
  cache, and `_PYPI_INDEX_ARGS` pointed at a local folder holding this interpreter's setuptools and
  PyYAML, repackaged (`offline_uv`); the pip tests run with `PIP_CONFIG_FILE=/dev/null` against a
  local PEP 503 index.
- A wheel a test writes by hand (`tests/test_wheels.py::_wheel`) gives every member one fixed time
  (`wheels._ZIP_TIME`), as a real build stamps its commit's: a member stamped by the clock makes two
  builds two seconds apart differ, and a test comparing reruns then fails only some of the time
  (`tests/test_wheels_zip.py::TestGit::test_a_rerun_after_the_clock_moved_commits_nothing`).

## Workspace git aliases (`setup_git.sh`, `git_pull_all.sh`)

- Aliases live in LOCAL config (root + every active submodule), never global.
  (`tests/test_setup_git_aliases.py`)
- **A workspace without `.gitmodules` is read as if each project folder were a submodule (user
  request 2026-10-19).** [folders-as-submodules] `projects.workspace_folders` is the project list: `.gitmodules`' submodules
  when it exists, else every direct, non-hidden subfolder holding a `pyproject.toml`, active. A new
  reader of "the workspace's projects" uses it, never `submodule_active_map` (which stays the
  submodules only: activation, deinit, mirrors; read with ONE `git config --get-regexp` process — every command
  reads it, and a process per submodule made `aisland --help` take 79 s on a loaded machine:
  `tests/test_submodule_active_map.py`). What needs a submodule is refused naming the
  reason: a `github`/`pypi` source (it would empty the folder for good), a mirror, a wheel of a
  project in no git repository. (`tests/test_folder_workspace.py`)
- **A link to a folder inside the workspace is a short name, never a project (user request 2026-10-21).** [aliases]
  Every reader of the workspace's projects skips a folder for which `locations.is_alias(folder, workspace)` holds
  (`discover_projects`, `workspace_folders`, `harness._project_folders`, `sources.known_projects`,
  `workspace_file.workspace_projects`); a new reader does the same, never an `is_symlink` test of its own. A link to a
  folder OUTSIDE the workspace stays a project. The generated `.code-workspace` names such a folder by its link
  (`locations.alias_names`: root links, non-hidden, the first by name), its `path` the real folder; `workspace.toml`
  writes it as the project's `path` ([workspace-file]). (`tests/test_symlinked_folders.py`,
  `tests/test_workspace_file.py::test_without_gitmodules_each_clone_is_a_project_and_a_link_to_it_its_path`)
- Without `.gitmodules`, `setup_git.sh` gives every subfolder that is its own clone (and the root
  when it is a repository) every workspace alias with the root baked in (`GIT_PULL_ALL_ROOT`):
  `git_pull_all.sh`'s `folder_main` merges each clone on ITS OWN branch, the root last, and records
  no pointer; `git_push_all.sh` pushes each clone's branch and the root only when all pushed;
  `git_folder_all.sh <verb>` loops `status-all` (root first), `switch-all`, `merge-all`, `tag-all`
  (tag everywhere, then push) and runs `safe` in the clone typed in, with the workspace's venv and
  mypy from the root. No clone and no root repository: no aliases. The scripts run under bash 3.2
  (`/bin/bash`): brace a variable before a non-ASCII character (`${noun}…`), as 3.2 reads the
  byte into the name. (`tests/test_folder_git_aliases.py`, run under `/bin/bash`)
- `setup_git.sh [root]`: from the checkout (the script in `<root>/aisland/aisland/git/`) the
  `pull-all` alias keeps its root-relative form; installed elsewhere it names the script by its path
  and passes the root in `GIT_PULL_ALL_ROOT`
  (`::test_aliases_installed_from_outside_the_workspace_pull_it_from_inside_a_submodule`,
  `::test_aliases_installed_from_the_checkout_keep_their_form`).
- `git pull-all` is `git_pull_all.sh`, run from the workspace root wherever it is typed; the
  `/pull-all` and `/push-all` commands (`aisland/harness/commands/`) call it — change its `needs you:` wording and
  those commands together. (`tests/test_git_pull_push_all.py`) [pull-all]
  - A pointer conflict is settled only to the submodule's checkout when it contains BOTH sides
    (`::test_a_pointer_conflict_is_settled_to_the_submodule_merge_holding_both_sides`,
    `::test_a_pointer_whose_submodule_does_not_hold_both_sides_is_left_and_listed`).
  - A pointer is recorded only for commits that came from origin, never backwards
    (`::test_a_pointer_is_never_moved_backwards`,
    `::test_unrecorded_local_commits_with_nothing_incoming_are_left_alone`); the record commit
    takes only its pointers (`::test_recording_pointers_leaves_other_staged_changes_staged_and_out`).
  - `git fetch` + `git merge`, never `git pull` (`pull.*` settings, `submodule.recurse`).
  - **A clone still on a pruned history is never merged (user request 2026-10-27).** Each fetch takes origin's
    configured refspecs plus `+refs/wheels-prune/*:refs/wheels-prune/*`; a repository whose HEAD reaches an `<old>`
    of any record's `commit-map` is listed `not merged: … run aisland wheels reset <project>` and left — the merge
    would bring the stripped history back [wheels-prune]
    (`::test_a_clone_still_on_a_pruned_history_is_not_merged_and_names_the_reset`,
    `::test_a_clone_already_on_the_pruned_history_pulls_as_usual`). `/pull-all` leaves the reset to the user.
  - Uncommitted edits are touched only under `--autostash`, only the files the merge needs.
  - **A root without an `origin` remote (what `workspace init` makes; user request 2026-10-21)** is neither
    fetched nor merged, each submodule is pulled on ITS OWN branch, and what came in is still recorded;
    `push-all` (both forms) skips that root and says so. With an origin, a submodule off the root's
    branch stays listed, never pulled — unless it is on a `dev/<name>` branch of its own, which is merged with
    `origin/dev/<name>` and never recorded in the root (user request 2026-10-28; unrecorded is a user ruling of the
    same day, do not re-litigate — recording it would point the root's branch at work only that `dev/<name>` holds;
    `::test_a_submodule_on_its_own_dev_branch_is_pulled_on_it_and_its_pointer_left`,
    `::test_a_root_without_origin_pulls_each_submodule_on_its_own_branch`,
    `::test_the_push_all_alias_pushes_the_submodules_of_a_root_without_origin`,
    `tests/test_folder_git_aliases.py::test_pull_all_skips_a_root_repository_without_origin`).
  - **An untracked submodule is never recorded (user request 2026-10-23).** `ignore = all` for it (`tracking.py`)
    — this machine's `.git/config` first, else `.gitmodules` — and step 4 skips it; it is still pulled
    (`::test_a_submodule_whose_updates_the_root_ignores_is_pulled_and_never_recorded`,
    `::test_this_machines_ignore_setting_counts_too`). Never record it "just this once": the root keeps the commit it last recorded. `/commit-all` and
    `/commit-session` leave it unrecorded, never `git add -f` (`aisland/harness/commands/commit-*.md`).
  - bash 3.2 (`/bin/bash` on macOS): no `wait -n`, associative arrays, `mapfile`; the tests run
    it under `/bin/bash`.
- `git push-all` pushes every submodule before the root, and the root only when they all pushed
  (`::test_the_push_all_alias_pushes_the_submodules_before_the_root`).

- **`aisland setup` installs a dependency CYCLE in one `uv pip install` and skips the dependents of a failure (`projects.install_groups` / `_run_installs`, 2026-10-07):** two projects can require each other (one through its `dev` extra), so installing either alone makes uv look for the other on the index and fail — and every project depending on either then failed too, burying the first error. `install_groups` returns dependency-first strongly-connected components (a cycle is ONE group); `_run_installs` runs one `uv pip install -e A -e B` per group, SKIPS (and names) a project whose dependency failed rather than attempting it against the stale index copy, and setup prints one failed/skipped summary. Don't revert to a per-project loop or edit the extras to hide the cycle. `apply_selection` still installs singletons (`TASKS.md`). Pins: `tests/test_install_groups.py`.
