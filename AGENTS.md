# Aisland Mandates

Rules only. The reasons are in [`docs/architecture.md`](docs/architecture.md), cited by the
short names below; usage is in the [README](README.md). Root `AGENTS.md` rules are not
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
file a machine without aisland reads · [harness] every workspace gets aisland's agent commands and rules.

## Current state

The workspace's dev-infrastructure CLI, `aisland` (click, `aisland/cli.py`):
- `locations.py`: where the workspace and the state are — from a checkout as always, from a wheel
  where aisland runs (standard library only; the pytest plugin imports it).
- `manager.py`: workspace root, `aisland setup` (provisioning, submodule activation, editable
  installs, generated workspace files), `BaseService`.
- `workspace.py`: `aisland workspace export [file]`, the workspace file (the how-to for a machine without aisland,
  then `[projects]`: each project's folder, `url`, `branch`), and `aisland workspace init <folder>`, a new
  workspace (its starting files, the `--from` file's projects and the `--add`
  submodules, the git aliases).
- `config.py`: the one committed `config.toml`, written through `sources.py`
  (`aisland source`), `mirrors.py` (`aisland mirror`) and `selection.py`
  (`aisland framework|tracker`; `frameworks.py` is a facade over it).
- `supervisor.py` (process-compose), `doctor.py` (`aisland doctor`), `completion.py`
  (`aisland completion install`), `clean.py` (`aisland clean`, `aisland backups`).
- `pytest_stats.py`: the `aisland_test_stats` pytest plugin (entry point `pytest11`) that
  records every run of a workspace project, and `aisland tests report` reading it beside the
  Jenkins builds' JUnit results.
- `wheels.py`: `aisland wheels build <project>`, the wheels of the workspace projects it needs that
  are not on PyPI (or, by flag, every needed or every workspace project), written into
  `<project>/wheels/` with `wheels.yaml` and `install.py` (`wheels_install.py`), and the check of what it
  takes from PyPI; the folder git-ignored unless `--commit`, packed by `aisland wheels zip` into a zip that
  installs with plain `python` (`wheels_unzip.py`), installed elsewhere by `aisland wheels install`.
- `nightly.py`: `aisland tests nightly`, the morning report of the orchestrator's nightly runs
  (`aisland/data/nightly/<date>.md`, a desktop notification, exit 1 on a bad night), run by the
  `<workspace>.nightly-check` Jenkins job.
- `tunnel.py`: `aisland tunnel` — a VS Code Remote Tunnel to the machine aisland runs on. One
  detached process with a pidfile, NOT a `BaseService` (the remote box has no process-compose) and
  not in `aisland start`; workspace-free in `cli.py`'s `_WORKSPACE_FREE`, because the box it is for
  has no workspace. State is PER MACHINE, `~/.aisland/tunnel` (`AISLAND_TUNNEL_HOME` overrides it,
  for a `$HOME` that does not persist) — never derived from the working directory, which made
  `start` in one folder and `status` in another disagree. `code tunnel` is passed NO account option
  (it has none; the sign-in runs first, separately). The remembered workspaces back `open`'s shell
  completion — aisland's first dynamic completion — so every reader of that file treats a bad file
  as empty rather than raising.
- `services/`: `jenkins` (incl. `aisland jenkins scaffold`), `mlflow`, `labelstudio`,
  `fiftyone`, `aim`, `swiftbar` run under the supervisor; `docker` (Colima), `clearml`,
  `iqengine` run under Docker.
- `setup.sh`: interpreter + venv, then `aisland setup -y` and `aisland completion install`.
- `harness.py` + `harness/`: the agent harness — the workspace commands, their helper script and the generic
  agent rules — installed into every workspace's `.agents/` by `aisland setup` and `aisland workspace init`.
- `aisland/git/setup_git.sh`: the workspace git aliases, local config of the root and every active
  submodule; `pull-all` runs `git_pull_all.sh`. `aisland git aliases` runs it with the root.

Invariants: only supervisor-started services exist (one started elsewhere is invisible; the
start-time port pre-flight naming the PID is the only signal). All service state lives under
`<workspace>/aisland/data/` (from a wheel, `<workspace>/.aisland/data/`).

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
    defines the root); the file finds `bin/` beside itself. (`tests/test_project_bashrc.py`)
  - `project.bashrc` ALWAYS sets `PROJECT_ROOT` from its own place, never keeps an inherited one: an editor
    started from a shell that sourced another workspace passes that root down, and its `.venv` was activated
    (`::test_a_project_root_inherited_from_another_workspace_is_replaced_by_this_ones`). A `.env` with no
    settings exports nothing — a bare `export` lists every variable (`::test_an_env_file_without_settings_prints_nothing`).
    `.env` is SOURCED under `set -a`, never word-split through `xargs` (which cut `"/a b"` at the space and left
    `$VAR` unexpanded) (`::test_a_quoted_value_with_a_space_is_exported_whole_and_a_variable_in_a_value_expanded`).
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
- **A workspace is written down in one TOML file a machine without aisland reads (user request
  2026-10-20).** [workspace-file] `export` writes the how-to comments (venv with pip and with uv, aisland-manager
  from PyPI or a wheel, `init --from`, `setup`) and `[projects]`; `init --from` reads it. A project takes `url` and
  `branch` only; the file is read BEFORE anything is written, and every refusal names `file:line`
  (`_line_of`). `export` never writes over a file. `.venv` stays the one venv aisland finds — a link counts, and
  `init`'s `.gitignore` says `.venv` (not `.venv/`) so the link is ignored; never add a venv-name setting without
  asking. (`tests/test_workspace_init.py`)

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
- Unix: socket in `aisland/data/`; Windows: TCP `AISLAND_SUPERVISOR_PORT` (default 9089).
  process-compose comes from Homebrew (macOS) or `USERSPACE_TOOLS` (Linux).
- Every `USERSPACE_TOOLS` entry pins a per-arch `sha256`, checked before extraction; bump
  `version` and digests in one edit; a missing digest is never shipped.
  (`tests/test_provision.py::test_registry_pins_wellformed_sha256_for_every_tool`)
- A new service extends `BaseService` and implements `launch()` (command, env, probe); the
  base owns start/stop/status/reset. One `aisland/data/<service>/` and log each; no shared
  state.
- A Docker-run service (`docker`, `clearml`, `iqengine`) overrides `launch`/`stop`/`status`/
  `reset`; its `info()` skips `super().info()` (the supervisor line would mislead).
- A service never installs its backend: `mlflow`, `aim`, `clearml` fail naming
  `aisland tracker set <name>`; `fiftyone` (on no axis) fails naming `uv pip install fiftyone`
  (`tests/test_fiftyone_setup.py::test_setup_stops_with_the_uv_install_command_when_fiftyone_is_missing`).
- New services write `.env` through `supervisor.write_workspace_env` (aim/clearml copies:
  TASKS.md).
- macOS external volumes: Full Disk Access for the RESOLVED process-compose binary, then
  `aisland supervisor down && aisland start --background`. Never "fix" it by moving
  `DATA_ROOT`, editing Jenkinsfiles or loosening volume ownership; diagnose with a live
  `log stream`. (README "macOS: Full Disk Access for the supervisor")
- `aisland jenkins init` seeds a multibranch job per `Jenkinsfile`; `--local` seeds
  `<project>.local` with `Jenkinsfile.local` embedded.
- Jenkins (and its plugin manager) runs on the Java its WAR accepts, by FULL path — never the
  bare `java` on PATH, which a shell profile points at the newest JDK (26 broke Jenkins on
  2026-10-06). `services/jenkins_java.py` probes each installed JDK, newest first, with
  `java -jar jenkins.war --version`; no Java version is written into aisland, and the bare
  `openjdk` formula is never a sys-dependency. On macOS `provision_system` installs the
  `openjdk@N` the WAR names. (`tests/test_jenkins_java.py`,
  `::test_aisland_never_declares_the_floating_openjdk_formula`)

## Docker, containers, trackers

- Colima shares change ONLY through `aisland docker start|restart --mount`: `$HOME:w` always
  re-stated first, symlinks resolved, a missing directory refused, a running engine never
  restarted for the user. (`tests/test_docker_service.py`) [containers]
- `DockerService.is_running()` is `colima status`'s exit code, never its text; everything
  else reuses it.
- ClearML vendors nothing. (`tests/test_clearml_service.py`)
  - Upstream compose at a release TAG, checked against `COMPOSE_SHA256`; bump both together.
  - The override is GENERATED from upstream and comes second in `-f`.
  - The fixed user goes in `state/config/apiserver.conf` (0600) before `up`, never in
    `CLEARML__…fixed_users…` env vars.
  - An existing login is reused; minting credentials never raises.
- IQEngine: `<image>:<version>@<digest>` (bump `IQENGINE_VERSION` + `IQENGINE_DIGEST`
  together), mount `:ro`. `start` keeps diagnosing both empty-table cases (no `.sigmf-data`;
  an unshared directory). `reset` never touches the recordings.
  (`tests/test_iqengine_service.py`) [containers]
- Aim: `start` publishes `AIM_REPO` to `.env` (run and UI must share it), passes `--yes`, and
  finds `aim` venv-first. (`tests/test_aim_service.py`)

## Test-run statistics (`pytest_stats.py`)

Root `AGENTS.md` "Two Test Runs" (user ruling 2026-10-05). [test-stats]
(`tests/test_pytest_stats.py`, `tests/test_workspace_fast_run_default.py`)
- The plugin records a run only when pytest's rootdir is `<workspace>/<project>` (a submodule, or a
  project folder of a workspace without `.gitmodules`:
  `::test_a_project_folder_of_a_workspace_without_gitmodules_is_recorded`); never a `pytester` tmp project
  (`::test_a_run_outside_the_workspace_records_nothing`), and never a Jenkins build (`JENKINS_URL`
  set — the local Jenkins builds in the project folder itself, and its JUnit record is the CI half
  the report reads; `::test_a_jenkins_build_is_left_to_jenkins_own_record`).
- It never costs a run its result: standard library only (no `aisland.manager` import — it
  loads in every pytest start), one append per run, a failed write is one printed line
  (`::test_a_stats_folder_that_cannot_be_written_costs_one_line_never_the_runs_result`).
- The data folder is resolved like `manager.DATA_DIR`
  (`::test_the_records_live_in_aislands_data_folder`); after touching the entry point,
  `uv pip install -e ./aisland --no-deps` (`::test_the_plugin_is_declared_as_a_pytest11_entry_point`).
- A nodeid and CI's JUnit `classname::name` meet through `junit_address`, pytest's own
  mapping (`::test_the_junit_address_is_the_one_pytests_own_junit_report_writes`).
- The report proposes moves; nothing moves a test between legs but an edit to its
  `@pytest.mark.slow` (or a `FAST_SAMPLE`). Never deselect by history at collection time.
- A project that registers `slow` sets `addopts = "-m 'not slow'"`
  (`tests/test_workspace_fast_run_default.py::test_the_real_workspace_runs_the_fast_run_by_default`).

## Doctor

- A recurring environment skew becomes a `check_*` in `doctor.py`, listed in `CHECKS`, not
  just a note. Current checks: `opencv`, `huggingface_hub`, `mcp`, `docker-creds`,
  `colima-tls`, `jenkins-java`, `torch-ext-lock` (rules for project authors: root `AGENTS.md` "Known Environment
  Skews").
  [doctor]
  - Cheap, read-only, specific; a failure carries a copy-pasteable remedy
    (`tests/test_doctor.py::test_every_registered_check_returns_a_result`).
  - Imports run out-of-process (`_import_probe`); `run_doctor()` exits 0/1.
  - Each check names the invariant it mirrors (`opencv` ↔ `manager._ROOT_UV_BASE`).
  - A probe that cannot run passes with a note. Never boot a VM; VM state comes from
    `DockerService.is_running()`.
  - Not a venv linter: `pip check`'s nominal lines are not breakage.
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
  `aisland source set <p> local --ref <branch> --machine-only`; after a branch switch run
  `uv pip install -e ./<p> --no-deps` to refresh the metadata.
- Optional submodule = ships a `uv.toml` or is `active = false`. The answer is per machine in
  `.git/config`, never `.gitmodules`; an undecided one is prompted even under `-y`;
  `--activate`/`--deactivate` skip the prompt; deinit is never `-f`.
  (`tests/test_uv_activation.py`)
- Every name in a `[tool.aisland] frameworks`/`trackers` declaration must be a real extra:
  `aisland setup` and `aisland framework|tracker set` refuse up front, naming project and name
  (user ruling 2026-08-25). (`tests/test_workspace_axis_declarations.py`)
- An internal dependency spec must accept the sibling's declared version, extras included; a
  bare name makes no claim. Bumping a version means every sibling spec must still accept it.
  (`tests/test_workspace_dependency_specs.py::test_the_real_workspace_has_no_unsatisfiable_internal_specs`)

## Generated and committed workspace files

- Generated, gitignored, rewritten from discovery (never a hand list): root and per-project
  `pyrightconfig.json` (relative paths; each project's `.gitignore` gains the entry),
  `project.bashrc`, `.gemini/settings.json`, `.mcp.json`, root `uv.toml`,
  `<workspace>.code-workspace`, and `.project.bashrc.completion` (`aisland completion
  install`). Never commit editor
  `extraPaths`. (`tests/test_pyrightconfig.py`, `tests/test_mcp_config.py`)
- `.gemini/settings.json` and `.mcp.json` keep ABSOLUTE venv paths (clients launch from any
  cwd). MCP discovery matches a `<pkg>.server:main` whose module imports `mcp.server`, never a
  class name.
- Root `mypy.ini` is committed; setup only APPENDS missing projects to `mypy_path` and never
  removes one. Never make it generated. (`tests/test_mypy_path_sync.py`)
- Root `uv.toml` = `_ROOT_UV_BASE` (drops `lightning`, `opencv-python`; keeps
  `opencv-python-headless>=4.12,<5`) + each ACTIVATED project's `uv.toml`. A private index
  lives only in its project's `uv.toml`; never commit the root file or put an index in the
  base. (`tests/test_uv_activation.py`)
- `config.toml` is the ONE committed config (`frameworks`, `trackers`, `[sources.<project>]`).
  (`tests/test_config.py`) [config]
  - `config.py` owns the file, not the data; it imports nothing from aisland.
  - Writes are whole-file, keyed; scalars before tables (`::test_scalars_are_emitted_before_tables`).
  - An absent key = unset; an empty document deletes the file.
  - `uv.toml` never joins it; never re-split it.
- `<workspace>.code-workspace` is GENERATED and gitignored (user choice 2026-10-18), written by
  `manager.write_code_workspace` on every `aisland setup` and `aisland source set|unset|apply`
  (`--machine-only` included). Folders: the workspace, each project with a checkout in
  dependency order (sorted by folder name first), then loggair's log folder when it exists.
  Settings and extensions come from `aisland/aisland/code-workspace.json` (strict JSON, no `folders`, no
  `python.analysis.extraPaths`): a shared setting changes there, never in the file. The template is
  written for `acme`: its `${workspaceFolder:acme}` and `acme-bash` take the workspace's name, and
  this platform's profile keeps its shell when the machine has it, else the bash on PATH (in acme on
  this machine the output is unchanged). (`::test_another_workspace_s_settings_name_its_own_folder`)
  (`tests/test_code_workspace.py`) [folders]
  - The log folder is loggair's answer, asked out of process (`python -P -m loggair --json`,
    from the root, `.env` over the environment): never a path typed into aisland, never an
    import of loggair. (`::test_the_real_loggair_report_names_its_log_folder`)
  - The real workspace ignores it and does not track it
    (`::test_the_real_workspace_keeps_the_file_out_of_git`).

## Project Sources Are Two-Layered

- Kinds `local` / `github` / `pypi`; branch on `REF_KINDS` / `DETACHED_KINDS`, never on a
  kind name. No `git`/`github` renaming: one word apart, and one deletes a checkout. [sources]
- Committed `[sources.<project>]` + per-machine `git config --local aisland.source.<project>`.
  The effective one drives the checkout, the `.venv` and, through the checkout, the generated
  workspace file's folders. No CI file reads either. An override never reaches a tracked file.
- `local` installs `selection.editable_target`, the same target as setup.
  (`tests/test_sources.py::TestApplySource::test_local_installs_the_dev_extra_and_the_selected_frameworks`)
- Before a deinit: `pypi` resolves and persists `distribution`; `_pypi_provenance` requires
  `github.com/Gearlux/` metadata; `_remote_ref_exists` checks a `github` ref. Unreachable =
  warn, never reject.
- Never force `git switch` / `submodule deinit`. Report the remedy for the ACTUAL blocker
  (`_deinit_remedy`: dirty tree vs stale gitlink) and roll the config back.
  (`::TestSetProjectSourceRollback`)
- `reconcile_submodules` folds in `detached_projects()`. The scaffolder seeds a detached dep
  from the registry's `distribution`
  (`::TestScaffoldAsksPypiNotTheSource::test_github_dep_survives_its_own_empty_directory`).
- The remote comes from `.gitmodules`; `pip_vcs_url` rewrites `git@host:org/repo`.

## Project mirrors (`aisland mirror`)

User choice 2026-10-16: an aisland command, submodules only, run by `/push-all` and `/ship`.
[mirror] (`tests/test_mirrors.py`)
- The primary is the `.gitmodules` remote; the mirror (remote `mirror`) only receives. A sync
  copies the PRIMARY's `dev/main`, `main` and tags (`ls-remote origin`), never local branches
  (`TestSync::test_the_mirror_follows_the_primary_not_unpushed_local_commits`).
- Never force, never delete, never overwrite a tag: a moved branch and a differing tag are
  REFUSED with the remedy, a mirror-only ref is left (`TestSync::test_a_branch_the_mirror_moved_is_refused_and_left_alone`,
  `::test_a_tag_that_differs_on_the_mirror_is_refused_not_overwritten`,
  `::test_a_branch_or_tag_only_on_the_mirror_is_never_deleted`). Never add a `--force`.
- Committed `[mirrors.<project>] url` + per-machine `aisland.mirror.<project>`; setup re-applies
  the remote. Only a remote marked `remote.mirror.aislandManaged` is changed or removed
  (`TestSetAndUnset::test_set_refuses_to_take_over_a_remote_it_did_not_create`).
- Network calls run with `GIT_TERMINAL_PROMPT=0` (`TestSync::test_network_calls_never_wait_on_a_password_prompt`);
  a project without a mirror is never contacted.
- `/push-all` and `/ship` call `aisland mirror sync` last; change its exit status or wording and
  those workflows together.

## Selection axes (`aisland framework` / `aisland tracker`)

- `selection.py` owns the mechanism per `Axis`. `FRAMEWORKS` and `TRACKERS` are siblings,
  never merged. `frameworks.py` is a facade; leave its call sites alone.
  (`tests/test_selection.py`, `tests/test_frameworks.py`) [frameworks]
- A project declares (`[tool.aisland] frameworks`/`trackers`), the workspace selects; it
  installs declared ∩ selected, in declaration order. Declare only implemented extras.
- CI reads only the committed key (`read_committed`, all `AXES`); the per-machine
  `git config --local aisland.<plural>` drives only the venv.
  (`tests/test_frameworks.py::TestScaffolderUsesThisModule`)
- Unset ≠ empty: setup prompts on unset, `-y` with nothing recorded selects all, an explicit
  `--framework` is recorded. `all_extras()` spans both axes. Reinstalls follow
  `get_install_order`.
- A project that needs one framework outright (`raidar-ultralytics`) keeps it as
  a base dependency and declares no axis. Never fake an extra.

## Generated CI (`aisland jenkins scaffold`)

A bare `::` pin in this section is in `tests/test_jenkins_scaffold.py`.

- **CI installs what is RELEASED (user ruling 2026-09-26, do not re-litigate).** The runner
  files ask PyPI per internal dep (`released_on_pypi`: a final, non-yanked version whose
  metadata points at `github.com/Gearlux/`): released → no install line; else
  `git+https://github.com/Gearlux/<dep>.git@main`. They never read a source or `ref`;
  `aisland source set|unset` regenerates no CI file; an unreachable PyPI stops the scaffold
  (`ReleaseProbeError`). Tests stub PyPI with the autouse `released_packages` fixture.
  (`tests/test_sources.py`: `TestScaffoldAsksPypiNotTheSource`, `TestSourceChangesRewriteNoCI`,
  `TestReleasedOnPypi`) [sources]
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
  Initialize and the quality gates get none; the push pipelines get none (a runner's limits are
  measured on the runner: `TASKS.md`). Each pytest step first removes the files it writes
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
    guarded on `BASH_VERSINFO >= 4.4`. argcomplete: no member; never add a `streamstudio` row.
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
  naming `aisland source set <project> local` (`TestRefusals`). Nothing is built after a refusal.
  The one exception is `--force` (user choice 2026-10-20): a dirty project is then built from its
  working tree (`export_worktree`: `git ls-files --cached --others --exclude-standard`, never the bare
  folder, so an ignored `build/lib` stays out), its commit written `<sha>+uncommitted` and its
  `git status --porcelain` lines in `BuiltWheel.uncommitted` and the report; a clean project is still
  built from `git archive HEAD`; `--force --commit` is refused before any build
  (`TestTheRunWithFakedBuilds::test_force_builds_a_dirty_project_from_its_files_as_they_are_and_says_so`,
  `TestRefusals::test_force_with_commit_is_refused_before_anything_is_built`,
  `TestEndToEnd::test_force_ships_the_working_tree_of_a_dirty_project_and_checks_the_dirty_target`).
- "On PyPI" = `pypi_state`: Gearlux metadata and a file not yanked, pre-releases INCLUDED. Never
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
  request 2026-10-19).** [folders-as-submodules] `manager.workspace_folders` is the project list: `.gitmodules`' submodules
  when it exists, else every direct, non-hidden subfolder holding a `pyproject.toml`, active. A new
  reader of "the workspace's projects" uses it, never `submodule_active_map` (which stays the
  submodules only: activation, deinit, mirrors). What needs a submodule is refused naming the
  reason: a `github`/`pypi` source (it would empty the folder for good), a mirror, a wheel of a
  project in no git repository. (`tests/test_folder_workspace.py`)
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
  - Uncommitted edits are touched only under `--autostash`, only the files the merge needs.
  - bash 3.2 (`/bin/bash` on macOS): no `wait -n`, associative arrays, `mapfile`; the tests run
    it under `/bin/bash`.
- `git push-all` pushes every submodule before the root, and the root only when they all pushed
  (`::test_the_push_all_alias_pushes_the_submodules_before_the_root`).

- **`aisland setup` installs a dependency CYCLE in one `uv pip install` and skips the dependents of a failure (`manager.install_groups` / `_run_installs`, 2026-10-07):** two projects can require each other (one through its `dev` extra), so installing either alone makes uv look for the other on the index and fail — and every project depending on either then failed too, burying the first error. `install_groups` returns dependency-first strongly-connected components (a cycle is ONE group); `_run_installs` runs one `uv pip install -e A -e B` per group, SKIPS (and names) a project whose dependency failed rather than attempting it against the stale index copy, and setup prints one failed/skipped summary. Don't revert to a per-project loop or edit the extras to hide the cycle. `apply_selection` still installs singletons (`TASKS.md`). Pins: `tests/test_install_groups.py`.
