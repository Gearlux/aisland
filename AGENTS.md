# Aisland Mandates

Rules only. The reasons are in [`docs/architecture.md`](docs/architecture.md), whose Contents lists every decision: a
`[tag]` below names a decision record there (search the tag). Usage is in the [README](README.md) and its pages under
`docs/`. Root `AGENTS.md` rules are not repeated here. A `tests/…` path is the pin that holds the rule.

## Current state

The workspace's dev-infrastructure CLI, `aisland` (click, the `aisland/cli/` package); `docs/architecture.md` →
"Where things live" maps the modules. The rules that bind them:

- **`cli/`** [module-split]: `__init__.py` adds every group to `main`; `_base.py` holds `main`, the classes every group
  derives from (`_Command`, `_Group`, `_WorkspaceGroup`), the groups of the services a project brings, `--debug`'s log
  and the shared completers; each group is a module of its own (`workspace.py` with `configure.py` and `editor.py`
  added to it, `toplevel.py`, `wheels.py`, `services.py`, `tunnel.py`). A new command goes into its group's module, a
  new group into a module of its own. A command module builds its group with `click.group(cls=_Group)` /
  `click.command(cls=_Command)` — a `cls` left out loses the empty-word completion. `python -m aisland.cli` is how
  `aisland-mcp` runs a command.
- **`errors.py`** [errors-root]: `AislandError`, the root of every exception aisland defines. **`downloads.py`**:
  `download(url, target)` (a time limit, no part file left) is how every file aisland fetches arrives; `sha256(path)`
  checks one pinned by a digest.
- **`pypi.py`**: PyPI's JSON document (`pypi_json`) and the CI scaffolder's question of it (`released_on_pypi`);
  **`workspace/github.py`**: a project's GitHub repository from its remote (`github_repository`,
  `project_repositories`). Both were split out of `sources.py`, which keeps the source choice itself [module-split].
- **`venv.py`**: the workspace's `.venv` — `no_venv(root)`, the refusal naming how to make one, and
  `ensure_uv_in_venv(venv_bin)`; **`userspace_tools.py`**: `USERSPACE_TOOLS`, the pinned Linux downloads (the table
  alone; `setup/installers.py` installs them). Leaves, so every layer asks them, never another layer's module.
- **`gitcmd.py`**: `git(repo, *args, network=, timeout=, check=, input=, env=)` is the one helper that runs `git` and
  captures its text — `git -C <repo>` (a missing folder is git's exit code, never an exception), stdin empty unless
  `input`, `env` laid over the environment; `network=True`: no credential prompt, 60 s default limit, `NO_LIMIT` for a
  long transfer (a mirror's push, a prune's); `check=True`: an `AislandError` naming the command. `read(repo, *args)`
  is what git prints, or `None` when it cannot answer, and `remote_url(repo)` an origin's address. A new git call uses
  these, never a bare `subprocess.run(["git", …])`; a module's own wrapper only adds to them (`mirrors._git`,
  `wheels.prune._run`: `core.quotePath=false`, a `PruneError`). Only these run git themselves, each for a reason:
  `wheels.export.export_head` (`git archive`'s bytes), `sources._run` (git's output reaches the terminal),
  `workspace.init._add_submodule` (clone progress) — both with `gitcmd.network_options()` and `network_env()`, as below —, `wheels/filter_repo.py`, `pytest_stats.py` and the harness's
  `stage_hunks.py` (they run where aisland is not) (`tests/test_gitcmd.py`). **Every git command that reaches a remote gives up on one that stops sending**:
  `gitcmd.network_options()` (`http.lowSpeedLimit=1`, `http.lowSpeedTime=STALL_SECONDS`) and `network_env()`, through
  `git(network=True)`, the helpers above or `gitcmd.fetch` (best-effort, no overall limit). A transfer may be long, so
  the guard is never an overall limit (ssh's `ServerAliveInterval` bounds SSH). Never a bare
  `subprocess.run(["git", "fetch", …])` (`tests/test_gitcmd.py`). **A url, ref or path read from a file reaches git
  after `--`** (`clone`, `submodule add`, `ls-remote`, `remote add|set-url`): `workspace.toml` and a harness set are
  another person's files once a workspace is cloned, and `--upload-pack=…` must name a repository, never an option
  (`tests/test_harness_sets.py::test_a_url_that_looks_like_an_option_is_read_as_a_repository`).
- **`envfile.py`**: the workspace's `.env` — `read(workspace_root=None)`, what bash's `set -a; source .env` exports, and
  `write(values)`, through `atomic` (a new file `atomic.PRIVATE`); a leaf, so every reader imports it, never
  `supervisor` (rule below: "`.env` is read as the shell reads it").
- **`probe.py`**: a state probe — a short, read-only command a status line, the doctor or an MCP tool waits for
  (`run`, `succeeds`; `port_answers`, `listener` for who holds a port). Past `PROBE_SECONDS` (30) it is given up like a
  missing program, so a stuck `colima status` reads as not running and never hangs a tool. A new `state()`,
  `is_running()` or status probe goes through it (`tests/test_docker_service.py`).
- **`atomic.py`**: `write_text(path, text, new_mode=)` writes a state file whole or not at all (a scratch file beside
  it, then `os.replace`), keeps an existing file's mode and a link's target; `workspace.toml`, `.env`, the
  supervisor's document, the completion file and the harness's record go through it, and a new `.env` is
  `atomic.PRIVATE` (0600). Standard library only (`tests/test_atomic.py`).
- **`machine_config.py`**: this machine's settings — a repository's own `git config --local`, never committed.
  `get`, `get_bool`, `get_all(pattern)` and `put(key, value)` (`None` forgets the key; a bool is written
  `true`/`false`; `check=True` raises when git refuses) are the one way a module reads or writes
  `aisland.<plugin>.<project>.<key>`, `submodule.<name>.active` or `pull.rebase`; never a bare `git config --local`.
  A read answers "nothing" for an unset key or a folder that is no repository, and a write there is silent
  (`tests/test_machine_config.py`). `.gitmodules` keeps `gitmodules.py`; `pytest_stats.py` reads its own keys with the
  standard library.
- **`locations.py`**: where the workspace and the state are (standard library only: the pytest plugin imports it).
  **`paths.py`**: the state's folders as functions — `state_dir()`, `bin_dir()`, `data_dir()`, `tools_dir()` — asked
  each time, never a module constant fixed at import (a long-running process or a test may point the workspace
  elsewhere); a module calls them as `paths.data_dir()`, so `locations.state_dir` moves them all, and a module's own
  file under them is a function too (`supervisor.config_file()`, `nightly.report_dir()`,
  `pytest_stats.stats_folder()`) (`tests/test_locations.py`).
- **`manager.py`** [manager-split]: `aisland setup` runs its steps in order — provisioning, activation, editable
  installs (`_run_installs`), the generated workspace files, the git aliases — and each step's code is a module of its
  own (`projects.py`, `installers.py`, `paths.py`, `services/base.py`, `editor_files.py`, `mcp_config.py`,
  `uv_config.py`, `playwright_browser.py`, `git_settings.py` — `pull.rebase false` and the git aliases). A new step goes into its own module, never back into `manager.py`. A name another module uses is
  public (no `_`), in every module of the package; a name only its own module and its tests use stays private.
  **`quality.py`** is `aisland check`: each gate runs once, and `isort` and `black` only check (showing the change they
  would make, as the CI jobs do) unless `--write` lets them rewrite the files (`tests/test_quality.py`).
- **A test patches the module whose code makes the call**, never the one that defines the name (`from x import f`
  binds a copy). The workspace root has one source: a test points it at its folder with
  `monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)`. **No test reaches the real workspace unless it
  says so:** the autouse `tests/conftest.py::no_real_workspace` makes `locations.workspace_root()` raise
  `RealWorkspaceReached` naming the test; one that tests the real workspace's facts carries `needs_workspace` or
  `@pytest.mark.real_workspace`. An `aisland` a test starts as its own process is not covered (from a checkout it finds
  the real root by its place): pass its folder another way or run a read-only command
  (`tests/test_no_real_workspace.py`). **Nor its state:** from a checkout `locations.state_dir()` is the checkout itself,
  so the autouse `tests/conftest.py::no_real_state` answers a folder of the test's own instead (a test pointing
  `state_dir` elsewhere replaces it; `real_workspace` keeps the real one).
- **`setup/init.py`**: `aisland workspace init <folder>` sets up a new workspace or an existing repository in place
  (`--local`, `--untracked`; the file: "Workspace root and paths").
- **`workspace/local.py`**: a local workspace (`init --local`) is a root repository that tracks only the projects:
  `.gitignore` `/*`, `.gitmodules` and the projects committed, marked by `aisland.workspace.local` in `.git/config`
  (never a tracked file: the root is no one else's). `settle` commits `.gitmodules` and the projects' paths — nothing
  else staged — at init, after every `activation.reconcile` and after `track set|unset`; a commit git refuses is said,
  never raised; an unmarked workspace is never touched; the `active` plugin adds a project there with `-f`
  (`tests/test_local_workspace.py`).
- **`clones.py`**: `aisland setup` adds a clone the workspace does not list as a submodule BY ITSELF, never asking (user
  ruling 2026-10-23: do not add a prompt back; `tests/test_setup_adds_clones.py::test_setup_never_asks`). A clone is a
  direct, non-hidden subfolder with a `.git` FOLDER, an `origin` and a `pyproject.toml`, not in `.gitmodules`, no alias
  link, in a workspace with `.gitmodules` or a local root. `git submodule add -b <its branch> <its origin> <folder>`
  keeps it as it is (`-f` only in a local root); a refused add is said and setup goes on. Its tracking is
  `tracking.apply`'s (the workspace's `[workspace] track`, this machine's `aisland.track.default`), never inferred from
  the other projects. It runs before `workspace.file.refresh`.
- **`tracking.py`**: `aisland workspace track set|unset|status [PROJECT…]` — whether the root records a project's
  commits. Committed: the project's `track = false` in `workspace.toml` (written as `ignore = all` in `.gitmodules`);
  this machine: `aisland.track.<project>.track` (written as git's `submodule.<name>.ignore` in `.git/config`, which
  `apply` rewrites in every `activation.reconcile` because `submodule deinit` drops git's section). A committed
  `set|unset` drops this machine's choice for those projects. With no project named it is the WORKSPACE's choice (user
  correction 2026-10-23: `[workspace] track = false`, per machine `aisland.track.default`), which every project without
  a `track` of its own follows, those added later included. An `ignore` value aisland does not write (`dirty`,
  `untracked`) is the user's and left alone. **An untracked project follows its branch:** one on no branch is fetched
  and switched to its branch (`follow_branches`, run by every `activation.reconcile` and by `git pull-all`), never
  `-f`; a refused switch is said and the project left; a tracked project on no branch is never moved. Only submodules:
  a workspace without them, and a name that is no submodule's, are refused before anything changes. **`track` alone
  changes nothing** and **`track status` only reads** (`tests/test_tracking.py`, `tests/test_workspace_init.py`).
- **`recording.py`**: `aisland workspace test-stats on|off` ("Test-run statistics"). **`env_example.py`**
  [env-example]: `aisland workspace env-example [--missing]` and `env-test` ("Generated and committed workspace
  files"). **`workspace/file.py`** [workspace-file]: `workspace.toml`, kept current by `aisland setup` (`refresh`).
  **`server.py`**: `aisland-mcp`, aisland's MCP server (the `[mcp]` extra), a chosen set of tools listed in `.mcp.json`
  by setup's discovery. **`discovery.py`** [declarations]: what the projects declare to aisland. **`editor.py`**: this
  machine's VS Code theme and colours (`css_colors.py`: the CSS names). **`setup/configure.py`**: `aisland workspace
  configure`, one set of commands built from each plugin's keys — `source` (`workspace/sources.py` the model: kinds,
  both layers, the effective source; `workspace/source_apply.py` bringing a checkout and the venv in line;
  `setup/source_commands.py` `set`/`unset`, their checks and the editor file they rewrite), `mirror` (`mirrors.py`;
  action `mirror sync`), `active` (`activation.py`) and a workspace-level plugin per selection (`selection.py`).
  **`services/supervisor.py`** (process-compose), **`doctor.py`**, **`completion.py`**, **`clean.py`** (`clean`) over **`backups.py`** (the store, its retention and restore: `aisland backups`; `clean`
  imports it, never the reverse).
- **`pytest_stats.py`**: the `aisland_test_stats` pytest plugin (entry point `pytest11`) recording every run of a
  workspace project — the plugin alone, standard library only; **`stats_report.py`**: `aisland tests report`, which
  reads its records beside the Jenkins builds' JUnit results (the plugin never imports it). **`services/nightly.py`**:
  `aisland tests nightly`, the morning report of the orchestrator's nightly runs (`aisland/data/nightly/<date>.md`, a
  notification, exit 1 on a bad night), run by the `<workspace>.nightly-check` Jenkins job.
- **`wheels/`** — `build.py`: `aisland wheels` ("`aisland wheels`"). `vendor_wheels` is a driver over the run's phases
  [module-split]: `_plan` (a frozen `WheelsPlan`), `_choose_extra`, `_build_all`, `_write_folder`, `_check` (or
  `_unchecked` under `--no-check`), `_commit_or_note`; every refusal comes before the first build, in that order. The
  run's other parts are modules of their own: `wheels/projects.py` (the project model, the target, the closure that
  follows requirement extras), `wheels/published.py` (`pypi_state`, the PyPI wheel downloaded and checked against its
  listed SHA-256), `wheels/export.py` (`git archive` / the working tree), `wheels/report.py`, `wheels/files.py`, `wheels/check.py`,
  `wheels/bundle.py`, `wheels/extra.py`, `wheels/scrub.py`, `wheels/prune.py` (with `wheels/filter_repo.py`), and
  `wheels/install.py` / `wheels/unzip.py` (standard library only: they run where aisland is not). A name the run patches in a test is patched
  where it is CALLED: `wheels.build_wheel`, `wheels.install_and_import`, but `wheels.check.PYPI_INDEX_ARGS`, which both
  modules read from there.
- **`tunnel/`** — `host.py`: `aisland tunnel` — a VS Code Remote Tunnel to the machine aisland runs on. One detached process with
  a pidfile, NOT a `BaseService` (the remote box has no process-compose) and not in `aisland services start`;
  workspace-free (`_WORKSPACE_FREE`). State is PER MACHINE, `~/.aisland/tunnel` (`AISLAND_TUNNEL_HOME` overrides it),
  never derived from the working directory. `code tunnel` is passed NO account option (the sign-in runs first,
  separately). The pidfile's pid is the tunnel only while its command line names `binary.cli_path()` (`_is_tunnel`, read
  with `ps -ww`): after a reboot the number may be any process's, which is never signalled; `stop` removes the pidfile
  only once the tunnel has exited (SIGTERM, then SIGKILL, `STOP_GRACE_S` each), else keeps it and says so
  (`tests/test_tunnel_process.py`). The package's other parts: `state.py` (the per-machine folder, the name rule — the
  leaf the others import), `binary.py` (the VS Code CLI, its archive's url and SHA-256 read from the update service's document
  `CLI_RELEASE_URL` and checked before anything is unpacked — a document without an https url and a digest fetches
  nothing —, unpacked to a `.part` file and moved into place whole),
  `store.py` (the remembered workspaces: every reader treats a bad file as empty); the completers live in
  `cli/tunnel.py`.
- **`tunnel/connect.py` + `tunnel/shell.py`** [tunnel-terminal]: `aisland tunnel connect`, a terminal through the SAME
  VS Code tunnel (no sshd on the box, no second tunnel): `devtunnel connect` forwards its control port 31545 and
  aisland's MessagePack client (`msgpack`, the `[connect]` extra) `spawn`s `tunnel/shell.py` FROM ITS SOURCE
  (`python3 -c`), which runs the account's shell in a pseudo-terminal — interactive, NOT a login shell. `tunnel/shell.py`
  imports the standard library only (the box has a Python, not aisland). A taken port 31545 is refused, never reused;
  the forward aisland starts is always stopped; raw mode is entered with `TCSADRAIN`, never `tty.setraw`'s default
  flush. `tests/fake_devtunnel.py` speaks the VS Code CLI's control protocol and changes with it. The user page is
  `docs/tunnel.md`, its twin `examples/01_tunnel_terminal.py`; an error the page quotes is the code's, word for word
  (`tests/test_tunnel_connect.py`, `tests/test_tunnel_docs.py`).
- **`ci_scaffold.py`**: `aisland jenkins scaffold` — a project's facts (`project_facts`) rendered by `ci_templates` and
  written, each Jenkinsfile linted by the running Jenkins first; `STAGE_LIMITS`, `GH_ACTIONS` and `TIMED_OUT_MESSAGE`
  live here; **`ci/jenkins_job_config.py`**: what each seeded job's `config.xml` holds (text only: the templates, the
  orchestrator's pipeline, `NIGHTLY_CRON`). **`services/`**: `jenkins` (the service: WAR, Java, plugins, credentials,
  launch; the jobs it seeds are `services/jenkins_jobs.py`, which writes them into the Jenkins home, and the home's own
  settings — the dashboard, the global properties, anonymous read — `services/jenkins_settings.py`) and `swiftbar` run under the supervisor; `docker` (Colima) runs on its own; `registry.py`
  lists them, `declared.py` and `plugin.py` run the ones a project brings.
- **`setup.sh`**: interpreter + venv, then `aisland setup -y` and `aisland completion install`. **`setup/harness.py` +
  `harness/`**: the agent harness. **`aisland/git/setup_git.sh`**: the workspace git aliases, local config of the root
  and every active submodule; `pull-all` runs `git_pull_all.sh`. `aisland setup` runs it with the root on every run and
  `workspace init` once (`setup/git_settings.py::install_git_aliases`); it has no command of its own.

Invariants: only supervisor-started services exist (one started elsewhere is invisible; the start-time port pre-flight
naming the PID is the only signal). All service state lives under `<workspace>/aisland/data/` (from a wheel,
`<workspace>/.aisland/data/`).

## Reading a project's files

- **A project's `pyproject.toml` is read through `pyproject.py`, `.gitmodules` through `gitmodules.py` — never a reader
  of a module's own.** [file-readers] `pyproject.py`: `read(path)` (`{}` for a missing, unreadable or broken file —
  lists and probes), `parse(text, path)` (a `PyprojectError(AislandError, ValueError)` naming the file and the line;
  discovery re-raises its words as a `DeclarationError`), `load(path)` (a file already found and checked; tomllib's own
  errors), `name(path)`. `gitmodules.py`: `read(root)`, a frozen `Submodule` (`name`, `path`, `url`, `branch`,
  `active`) per submodule with a `path`, from ONE `git config -f .gitmodules --get-regexp` process (a key written twice
  is read as git reads it, the last value); `projects.submodule_active_map`, `workspace.file.submodules` and
  `workspace.local.submodule_names` are views over it; single-key `--get` reads and writes (`tracking`,
  `mirrors.primary_url`, `sources.remote_url`, `activation`) stay where they are. A new reader adds a view, never a
  second `tomllib.load` of a `pyproject.toml` or a second `--get-regexp`. Exceptions: the standard-library-only modules
  (`wheels/install.py`, `wheels/unzip.py`, `tunnel/shell.py`, `pytest_stats.py`, `locations.py`) and `harness/`, which
  run where aisland is not installed; other TOML files (`workspace.toml`, `uv.toml`, a template set's `template.toml`)
  keep their readers (`tests/test_pyproject.py`, `tests/test_gitmodules.py`).

## Errors exit 1

- **A command that prints an `Error:` line exits 1.** [failures] A failure that ends the command is raised (one
  `Error:` line on stderr). A failure the command goes on past (a project that fails to install while setup installs
  the others, a service that does not start beside the others) is `failures.report(...)`-ed: its line goes to stderr,
  the command finishes, and `cli._base._WorkspaceGroup.invoke` turns the run into exit 1, also over a command's own
  `SystemExit(0)`; a non-zero code of its own is kept. Never `click.echo("Error…")` and return: scripts, CI and the MCP
  server read the exit status (`tests/test_errors_exit_one.py`).
- **Library code speaks through `console.py`, never click itself.** `console.echo(…, err=)`, `console.confirm`,
  `console.prompt` go to the console in charge: click's terminal (`Terminal`) for a command, a `Collector` inside
  `console.collecting()` for each MCP tool call — its stderr lines come back as the tool object's `warnings`, the rest
  is dropped, and a question raises `NoOneToAsk` (an `AislandError`), never a guessed answer. Only `aisland/cli/` and
  `console.py` call `click.echo`/`confirm`/`prompt`; `click.style` stays anywhere. A test answers a question by
  patching `console.confirm`. `tests/test_console.py` reads every use of click's speech from the syntax tree, any
  import or alias (`tests/code_rules.py`) — never a flake8 plugin, which would run in every project of the shared venv
  (`tests/test_server.py`).
- **A warning goes to stderr through `failures.warn`**, never `console.echo("  - Warning: …")`: it fails nothing and
  stays off stdout, which a script reads (`--json`)
  (`tests/test_configure.py::test_a_warning_goes_to_stderr_and_leaves_the_json_whole`).
- **One exception root, and `--debug`.** [errors-root] Library code raises `AislandError` — never
  `click.ClickException` itself, never `SystemExit` (the harness scripts, a standalone script's `__main__` block and
  `aisland-mcp`'s `main` are their own entry points) — and an exception class aisland defines derives from it AND the builtin it stands for
  (`DeclarationError(AislandError, ValueError)`). `_WorkspaceGroup.invoke` turns an `OSError` or a `SubprocessError` (a
  `CalledProcessError`, a `TimeoutExpired`) into one `Error: <it> (run `aisland --debug …` for the traceback)` line; `--debug` re-raises it and prints aisland's
  own refusals' tracebacks too; any other exception is a bug and keeps its traceback. An `except` names what can fail
  there, never `Exception` — except around a project's own code, a completion callback (`errors.quiet_completion`, the
  one guard every completer is wrapped in) and the pytest plugin's hooks. Every download goes through
  `downloads.download`, and a git network call is guarded (`gitcmd.py`)
  (`tests/test_errors_exit_one.py::test_no_library_module_raises_clicks_own_exception_or_exits`, which reads every
  `raise` and exit from the syntax tree, any alias;
  `tests/test_downloads.py`).

## Diagnostics and types

- **A diagnostic is `logging.getLogger(__name__).debug(...)`, shown by `--debug`; results and progress stay
  `console.echo`.** [debug-log] A module that reports aisland's own mechanics no user acts on (a cached download reused,
  a digest that matched, a step with nothing to do, which file was read, which command ran) logs it at debug with the
  standard library's `logging` — never loguru or loggair. `aisland --debug` (`cli._base._show_debug_log`) puts ONE
  handler on the `aisland` logger for the run (`aisland: <module>: <message>` on stderr) and takes it off when the run
  ends; without it nothing below WARNING shows. `console.echo` keeps the command's result, the progress a person watches
  (`==>` steps, `[OK] Created …`), a question, a warning the operator must act on and anything a script or test
  parses; when unsure, it stays output. A line moved to debug is asserted through `caplog` at DEBUG; use `%`-style
  arguments, no work before the call (`tests/test_debug_log.py`).
- **`Any` only with a `# Any: <why>` on its line.** A JSON or TOML shape read by key gets a `TypedDict`
  (`installers.UserspaceTool`, `wheels.ModuleFailure`/`ImportRun`, `pypi.PypiProject`,
  `supervisor.Readiness`/`ProcessState`); a value only passed on is `object` / `Mapping[str, object]`; a completion
  callback takes `Optional[click.Context]`/`Optional[click.Parameter]`. What stays `Any` (a parsed TOML table
  `pyproject.Table`, Jenkins' or VS Code's JSON, a project's plugin object, a service's options) says why on the line
  or once on its alias. No `# type: ignore` is added to make a type fit.
- **The package is held to what `mypy --strict` checks, apart from `no_implicit_reexport`** (`mypy.ini`'s
  `[mypy-aisland,aisland.*]` names the flags, `--strict` being global-only; the tests are not held to it): a container
  names what it holds — never a bare `dict`, `set`, `List[dict]` or `CompletedProcess`. The project records setup
  passes around are `projects.ProjectMetadata`, a client file's servers `mcp_config.McpServer`; a test builds a record
  with `tests/records.py::project_metadata`.
- **Imports sit at the top of each module, and no function imports aisland** (aisland rules → "Top-Level Imports"). A
  cycle is broken by moving the shared piece down, never by a lazy import: `toml_places.py` (`Where`, `table_line`,
  `key_line`) is a leaf every TOML reader imports, and Jenkins' Java is installed by the `workspace provision install`
  command, above both `provision` and `services.jenkins`. A standalone script's optional library stays lazy
  (`wheels/install.py`'s PyYAML, `wheels/filter_repo.py`'s git-filter-repo). `tests/test_imports.py` imports every module
  first with none loaded before it, and fails on an aisland import inside a function.
- **The layers are import-linter contracts** (`[tool.importlinter]` in `pyproject.toml`, run by
  `tests/test_imports.py::test_the_import_graph_keeps_every_contract`; `lint-imports --no-cache` shows them). The
  `layers` contract, top to bottom — a layer imports only those below it, the modules of one line never one another:
  `cli | server`, `doctor | clean`, `backups | services`, `ci`, `wheels`, `setup`, `workspace | tunnel`. Every other
  top-level module is a LEAF (`atomic`, `console`, `errors`, `gitcmd`, `locations`, `paths`, `venv`, …), which the
  `leaves` contract forbids every layer; a new top-level module joins one of the two lists
  (`::test_every_top_level_module_is_in_a_layer_or_a_leaf`). Inside each subpackage no cycle; the
  standard-library-only modules (`atomic`, `locations`, `plugins`, `pytest_stats`, `tunnel/shell.py`,
  `wheels/install.py`, `wheels/unzip.py`) import no third-party package, and the three that run where aisland is not
  import nothing of aisland. Where a lower layer needs what a higher one has, the piece moves down (`venv.py`, the
  `[ci]`/`[harness]` keys in `workspace/file.py`) or the caller moves up (`setup/configure.py`,
  `setup/source_commands.py`), never a lazy import. A contract changes with the rule it holds, never to let an import
  through.

## Workspace root and paths

- **From a checkout aisland works as it always did, and a checkout is one only where a workspace lists it.**
  [wheel-mode] `locations.FROM_CHECKOUT` (`runs_as_checkout`: aisland's `pyproject.toml` beside the package, which no
  wheel carries, AND `listing_workspace` — the nearest folder above, up to five, whose `.gitmodules` has a submodule at
  the checkout's path or whose `workspace.toml` has a project in it) selects the rule. A clone no workspace lists (a
  contributor's, GitHub CI's checkout of aisland alone) works like a wheel; a `TASKS.md` or `.git` above it lists
  nothing. From a checkout the root is the listing workspace (`checkout_workspace_root`'s first step; without
  submodules the OUTERMOST `TASKS.md`, then the outermost `.git`; last resort `parents[2]`; never "first `TASKS.md`
  wins": every project has one), the state follows the package's location, and the cwd and `AISLAND_WORKSPACE` change
  nothing. From a wheel the root is `AISLAND_WORKSPACE`, else `locations.find_workspace` (first hit upwards:
  `.gitmodules`; the start folder when it holds projects; a project's parent; a `.git` folder — the subfolder rule only
  at the start), and the state is `<root>/.aisland/`. Never read the cwd or `AISLAND_WORKSPACE` in checkout mode, and
  never find a path by counting folders up from a module (`parents[N]`): ask `locations`/`get_workspace_root()`
  (`tests/test_locations.py`, `tests/test_workspace_root.py`).
  - Importing never raises; outside a workspace the CLI's group (`_WorkspaceGroup.parse_args`) refuses every command but
    `workspace`, `wheels`, `tunnel`, `doctor`, `notebooks` and `--help`, before anything is written. A new
    workspace-free command joins `_WORKSPACE_FREE`; `completion` stays out
    (`tests/test_packaging.py::test_from_the_wheel_outside_a_workspace_the_doctor_checks_this_machine`).
  - From a wheel setup copies the packaged `project.bashrc` to `.aisland/` and links the root to it (`setup.py` puts it
    in the wheel; in the checkout it stays at `aisland/project.bashrc`); `MANIFEST.in` keeps it in the sdist, from
    which `python -m build` builds the wheel (`tests/test_project_bashrc.py`, `tests/test_packaging.py`).
  - The release's metadata is what PyPI shows: the README as Markdown, `license = "MIT"` (SPDX, no `License ::`
    classifier, setuptools >= 77), the author and GitHub URLs, keywords, a Development Status following the version,
    Python classifiers `requires-python` admits. The build prints no deprecation warning and `twine check --strict`
    passes (`tests/test_packaging.py`).
  - GitHub CI checks aisland out ALONE and installs only `.[dev]`, so `dev` holds everything the gates and suite need
    (the tools, `liquifai`, `setuptools`, `wheel`, `uv`). A test asserting the REAL workspace carries `needs_workspace`
    (`tests/workspace_layout.py`) and is skipped there with its reason, never failed. The runners have no git
    identity: every test commits as `conftest.git_identity`'s author. Reproduce CI in a fresh venv holding only
    `aisland[dev]` and `pip`, in a `git clone` of aisland alone, with `HOME` an empty folder, before changing either.
  - The source package builds the wheel and nothing else: `MANIFEST.in` prunes `tests/`, whose suite needs the
    repository (`tests/test_packaging.py::test_the_source_package_carries_no_part_of_the_test_suite`).
  - A release ships `LICENSE`, `CHANGELOG.md` and `aisland/py.typed`; the newest `## [<version>]` of the CHANGELOG is
    `pyproject.toml`'s version. It is published by `.github/workflows/release.yml` on a `v<version>` tag: the quality
    gates and both test runs (`pytest tests -m ''`), the tag guard, the guard that the CHANGELOG dates the version
    (`## [<version>] - YYYY-MM-DD`; changes since go under `## [Unreleased]`), `python -m build`, `twine check --strict`, a smoke
    test of the wheel in a clean venv from `/tmp`, then Trusted Publishing from the `pypi` environment. Change a step
    and its test together (`tests/test_packaging.py`).
  - `project.bashrc` ALWAYS sets `PROJECT_ROOT` from its own place, never keeps an inherited one; `.env` is SOURCED
    under `set -a`, never word-split through `xargs`, and one with no settings exports nothing; each shell function it
    defines (`cdp`, `cdd`, `cdm`, `cdr`, `h`, `tm`) is `unalias`ed first; Tab after a `cd*` helper completes a folder
    under ITS root under bash 3.2 (no `compopt`), unescaping the typed word without `eval`; `tm` hands `tmux
    new-session` the current folder and every exported variable but the terminal's and tmux's own, read NUL-separated
    (`env -0`) (`tests/test_project_bashrc.py`).
- Binaries, data and logs resolve to `<workspace>/aisland/` from the package's own location, never the cwd; nothing is
  written into the workspace root as service state. A file the package reads or runs (the workspace-file template, the
  git alias scripts) lives INSIDE `aisland/aisland/` and is named in `[tool.setuptools.package-data]`; never read one
  from the project folder beside the package
  (`tests/test_packaging.py::test_the_wheel_carries_every_file_of_the_package`).
- **The workspace's name is `[workspace] name` in `workspace.toml`, else its origin's repository, else its folder.**
  `projects.get_workspace_name` is the one reader (`workspace.file.name_of`); it names `<name>.code-workspace`, the
  Jenkins jobs, the nightly report, the how-to and `init`'s first line. A name that is no plain name is refused naming
  its line; discovery refuses a selection called `name` (`tests/test_workspace_file.py`).
- **A workspace is written down in `workspace.toml`, shaped like `.gitmodules`.** [workspace-file] `workspace/file.py`:
  the how-to comments, `[workspace]`, then a `[project."<name>"]` per project — `path` (its folder; the name when left
  out), `url`, `branch`; other keys of a project are kept as written. `aisland setup` keeps it current (`refresh`, from
  `.gitmodules` or the clones): a new project added, a changed url or path followed, a project the workspace lacks
  KEPT when it has a url (setup's activation step clones it, so writing a row is a way to add a project, and removing
  one means deleting its row too) and dropped otherwise, a branch written once KEPT; a project is matched by its path
  first, so a hand-written name survives; nothing changed, the file is not touched. `init --from` clones each project
  into its path on its branch and copies the file in; there is no `export`. A link in the workspace folder is written
  as the project's `path`. Every refusal is a `FileError` naming `file:line`, and a `--from` file is read BEFORE
  anything is written. `.venv` stays the one venv aisland finds (a link counts; `init`'s `.gitignore` says `.venv`, not
  `.venv/`); never add a venv-name setting without asking (`tests/test_workspace_file.py`,
  `tests/test_workspace_init.py`,
  `tests/test_activation.py::test_setup_clones_a_project_written_into_the_file_by_hand`).
  - **The file carries its `format` and names a key nobody reads.** [workspace-format] `render` writes `[workspace]
    format = 1` first (`workspace.file.FORMAT`); `read` takes a file without it as format 1, refuses a newer one naming
    the line and `pip install --upgrade aisland`, and a value that is no whole number.
    `workspace.check.unknown_keys` names every key no part of aisland reads — a project's (`workspace.file.PROJECT_KEYS`
    and each plugin's: `sources.COMMITTED_KEYS`, `mirrors.MIRROR_KEY`, `activation.ACTIVE_KEY`, `tracking.TRACK_KEY`,
    `pytest_stats.RECORD_KEY`), the workspace's (`name`, `format`, `track`, `test-stats`, every selection) — with its
    line and the closest key; `setup`, `workspace show` and `workspace configure` print them on stderr and go on. **A
    new setting adds its key to `workspace_check` in the same change**, or every workspace using it is warned
    (`tests/test_workspace_check.py`).
  - **A new workspace whose `init` fails half way is removed:** a folder `init` made, or found empty, goes when a clone
    fails or the run is interrupted, and the error says so; a folder that held files is never deleted (the error names
    `.git` and `.gitmodules` to remove); an existing repository set up in place is left as it stopped
    (`tests/test_workspace_init.py`).
  - `init` writes over a starting file ONLY when `--rewrite <file>` names it (a closed set, `workspace.StartingFile`);
    the old copy becomes `<file>.bak`, and a `.bak` already there refuses the whole run before anything is written. In
    an existing repository `--rewrite` writes the named files and nothing else, and beside any other option it is
    refused.
  - **An existing repository is set up in place:** no `git init`, the `--add` projects, the missing starting files,
    aliases, harness, `--untracked`. `--from` (the repository keeps its own `workspace.toml`) and `--local` (that root
    is already shared) are refused there before anything is written (`tests/test_workspace_init.py`).

## User docs (`README.md`, `docs/`)

- **The README is the PyPI page: a quickstart of about 200 lines** (at most `README_MAX_LINES`, 250) — install, a first
  workspace with its output, configure, a common-tasks table and an index of every page; each topic is a
  `docs/<topic>.md`. A new topic adds its page and its README index row in one change; a feature section never grows
  back into the README (`tests/test_docs.py`).
- The README links only absolutely (`https://github.com/Gearlux/aisland/blob/main/…`); a page links relatively, and
  every link lands on a file and, with an anchor, on a heading of it. `docs/architecture.md` opens with a Contents that
  lists every decision once (`tests/test_docs.py`).
- `docs/architecture.md` is the CONTRIBUTORS' decision log (its opening and the README row say so). It keeps its dates
  and measurements, and names a project it measured on by role — `planner`, `trainer`, `corelib`, `datasets`,
  `vision`, `compiler`, `labels`, `canvas`, `curator`, `detector` — never by name; the CHANGELOG and `examples/` are
  held to the same list (`tests/test_docs.py`).
- A user page names no project of the workspace aisland is developed in, nor a company: examples use `acme`, `models`,
  `tools`, `viewer`, `app`, `lib`; the tools aisland drives (liquifai's completion, loggair's log folder) keep their
  names (`WORKSPACE_NAMES`). No dated measurements or test references on a page — those belong in
  `docs/architecture.md` and here — and sample output carries example dates, not real ones. The same list holds what
  the installed package shows: every command's `--help` (examples use `models`) and the usage text of the zip
  `aisland wheels zip` writes. Every file of the package — code, comments, templates — names no private project,
  platform or company (`tests/test_no_private_names.py`). The private, company and domain names are kept as digests
  (`tests/private_names.py`: `DOMAIN`, `PRIVATE`, `words_named`), so no file of the repository spells them, the tests
  that look for them included; a name to add is added as its SHA-256 in lower case; comments may still name the workspace's projects (root rule
  "Docs Never Name Dependent Projects"), code may not.
- Every command, each verb of each group, is shown on a page or the README, and every variable `env.complete` lists for
  a person to set is in the README's table.
- An example in `examples/` is a page's runnable twin, named on its page, and touches only a temporary folder: a command
  that changes a workspace (`setup`, `configure … set`, `clean`) has no example. Each asserts what its page shows and
  runs in CI's Verify Examples and in the test run (`tests/test_examples.py`; one measured at a minute or more joins
  `SLOW_EXAMPLES`).

## Supervisor and services

- Never `Popen` a service, track it by PID file/`pgrep` or kill it with a signal: register it in the generated
  `data/process-compose.yaml` and `apply()` (`tests/test_supervisor.py`).
  1. Stop = `disabled: true` + apply, never a bare `process stop`.
  2. Startup success = `supervisor.wait_for_outcome`, never a momentary `is_running`.
  3. Every HTTP service passes a `readiness={"port", "path"}` probe to `start()`.
  4. `start()` pre-flights the port (`supervisor.port_in_use`).
  5. A re-start after a failed run needs an explicit `process start` (`BaseService.start` does it): `project update`
     ignores an unchanged document.
  6. No secret literal in the document: `${VAR}` refs to `.env` keys, and `apply()` layers `envfile.read()` over
     `os.environ`. No literal `$` in any value.
- **`.env` is read as the shell reads it, by ONE reader.** `envfile.read` is what `project.bashrc`'s
  `set -a; source .env` exports: `export`, quotes (single literal, double expanded, `\` escapes), `$VAR`/`${VAR}` from
  an earlier line then the environment (unset = empty), a trailing ` # note` dropped, a line bash reads as no
  assignment (`KEY = value`) skipped. Services, `apply()` and Jenkins read it; never a second parser.
  `envfile.write` single-quotes a value the shell would change (`tests/test_workspace_env.py`, every case
  checked against bash).
- Unix: socket in `aisland/data/`; Windows: TCP `AISLAND_SUPERVISOR_PORT` (default 9089). process-compose comes from
  Homebrew (macOS) or `USERSPACE_TOOLS` (Linux), installed by the first command that needs it once the user agrees
  (`supervisor.ensure_binary` → `provision.ask_to_install`) [on-demand-tools].
- A userspace tool's archive is unpacked into a scratch folder and moved into place whole, so an interrupted unpack
  leaves no folder a later run takes for finished; an architecture aisland has no build for is refused, never read as
  amd64; a tool naming its builds otherwise (node: `x64`) says so in `arch_names`
  (`tests/test_provision.py`). Every `USERSPACE_TOOLS` entry pins a per-arch `sha256`, checked before extraction; bump
  `version` and digests in one edit; a missing digest is never shipped
  (`::test_registry_pins_wellformed_sha256_for_every_tool`).
- **The Jenkins WAR is pinned the same way** (`JenkinsService.WAR_VERSION`, `WAR_SHA256`): it downloads to a `.part`
  file and moves into place only on a matching digest; bump both together from jenkins.io's `jenkins.war.sha256`; a
  WAR already in `bin/` is used as it is (`tests/test_jenkins_war.py`). **So is its plugin manager**
  (`MANAGER_VERSION`, `MANAGER_SHA256`, from its GitHub release's `.jar.sha256`), through the same `_fetch_pinned`;
  never "latest", since the jar is code `jenkins init` runs; the jars of other versions go once the pinned one is in
  place (`tests/test_errors_exit_one.py::test_a_plugin_manager_whose_digest_differs_is_refused_and_never_kept`).
- **The local Jenkins is this machine's alone.** It listens on `JenkinsService.LISTEN_ADDRESS` (`127.0.0.1`) — never
  every interface, Jenkins' default, which offers its script console and the `.env` values its jobs see to the
  network. Its initial admin password is named by its file (`admin_password_file`), never printed: output reaches
  scrollback, CI logs and, through `aisland-mcp`, an AI client. `jenkins init --local` may allow anonymous read
  (`allow_anonymous_read`: the nightly report, the SwiftBar menu and the scaffold's linter read without a login);
  never add back an option that turns the login or CSRF protection off — a page in the browser can post to
  localhost (`tests/test_jenkins_security.py`).
- **Every service is in ONE list, `services/registry.py` (`ENTRIES`, start order).** `aisland services
  [start|stop|restart|status|info|reset] [NAME…]` reads it: named → those, in the order given; of several, one that
  fails is reported and the others still get their turn (`registry._each`); unnamed → `status`/`info` every service,
  the others the `by_default` ones, `stop`/`reset` last-started first (the container engine last). A name no service
  has is refused before anything is done. There are no top-level `aisland start|stop|restart|status|info|reset`; a new
  service is one `Entry`, never a hand-written list in `aisland/cli/` (`tests/test_services_command.py`).
- **`aisland services` asks process-compose as little as it can.** Each question is a client process: `list_processes`
  and `get_ports` ask their own question alone, and `registry.status`/`info` run inside `supervisor.snapshot()`, where
  ONE `process list` answers every service (`tests/test_supervisor_snapshot.py`). The services the projects bring are
  worked out ONCE per CLI run (`cli._base._WorkspaceGroup._declared`, in `ctx.meta`), and `discovery.installed` scans
  the installed entry points once for every plugin (`tests/test_plugin_services.py`). **Listing the services imports no
  project's code.** [lazy-plugins] A plugin service's `Entry` loads its class on first use (`registry._Plugin`) and its
  group builds its commands on first use (`cli._base._ServiceGroup`), so `aisland --help` reads declarations and
  metadata only; `registry.entries` — what `aisland services …` acts on — loads and checks every class first, so a
  broken one is refused before anything runs.
- **A project declares a service in its `pyproject.toml` and writes no aisland code.**
  `[tool.aisland.service.<name>]` (`command`, `port`, `ready`, `needs`) becomes a `services/declared.py`
  `DeclaredService` — probed only with both `port` and `ready` — appended after aisland's own (`registry.declared`),
  started only when named, offered only while every `needs` choice is in that selection's effective choice. It gets
  `aisland <name> …` from `_WorkspaceGroup.get_command`, after aisland's own commands; its help names the project that
  brings it (`Entry.project`). A name of one of aisland's services is a `DeclarationError` naming the line; a name of
  an aisland command is NOT: the service loses only its shortcut (`Entry.shortcut`), stays `aisland services <verb>
  <name>`, and `aisland workspace show` warns — so a command a later aisland adds never breaks a workspace
  (`docs/stability.md`; `tests/test_declared_services.py`).
- **A project implements a service in its own code, importing nothing of aisland.** An `aisland.plugins` entry point of
  kind `service` (a class needing only `start(ctx)`; `stop`, `status`, `info`, `reset` are `BaseService`'s unless it
  has them, its `info(ctx)` printed after aisland's) becomes a `services/plugin.py` `PluginService`, offered once its
  project is installed, started only when named, with `--port` on `start`/`restart` (`ctx.port`, `None` when not
  given). `ServiceContext` is all it gets: `name`, `workspace`, `data_dir`, `port`, `background`, `run`, `write_env`,
  `ports`, `echo`, `fail`, `workspace_name`, `read_env()`, `choose_command(selection, name)` — grow it only with a real
  service that needs more. A class may declare `options = {"name": "help"}` (offered as `--name` on
  `start`/`restart`; `port`/`background` refused), `supervised = False` when Docker runs it, and `reset_question` when
  its `reset` wipes no data. A moved service disappears with its project; its data folder stays `data/<name>/`
  (`tests/test_plugin_services.py`). `aisland.plugins` (`ServiceContext`, `ServicePlugin` Protocols; standard library
  only) is the typed copy of that contract a project names under `TYPE_CHECKING`, the one module `docs/stability.md`
  keeps: a context attribute grows in `ServiceContext` and the Protocol together, which `mypy aisland` holds
  (`services/plugin.py`'s `TYPE_CHECKING` line;
  `tests/test_plugin_services.py::test_the_context_aisland_hands_a_plugin_is_what_aisland_plugins_types`).
- **A service's state is data: `state()` → `services.base.ServiceState`** (`service`, `state` — `running`, `stopped`,
  `not installed`, `not supported`, `unknown` —, `pid`, `ports`, `ready`). `status()` prints it; `registry.states`
  is what `aisland services status --json` prints and the MCP tool `services_status` returns, and a service tool
  answers with it after its command. A service the supervisor does not run (Colima, Linux Docker, SwiftBar) overrides
  `state()`; a project's class Docker runs (`supervised = False`) is `unknown`, never guessed
  (`tests/test_server.py::test_services_status_is_what_the_command_prints_with_json`).
- A new service extends `BaseService` and implements `launch()` (command, env, probe); the base owns
  start/stop/status/reset. One `aisland/data/<service>/` and log each; no shared state. A Docker-run service overrides
  `launch`/`stop`/`status`/`reset`; its `info()` skips `super().info()`. New services write `.env` through
  `envfile.write` (a project's class: `ctx.write_env`).
- A service never installs its backend: a project's tracking service fails naming `ctx.choose_command("tracker",
  <name>)` — `aisland workspace configure tracker set <the trackers already chosen> <name>` (`selection.add_command`),
  since `set` takes the whole list and naming the missing one alone would drop the rest
  (`tests/test_selection.py::test_the_command_that_adds_a_tracker_keeps_the_ones_chosen`).
- macOS external volumes: Full Disk Access for the RESOLVED process-compose binary, then `aisland supervisor down &&
  aisland services start --background`. Never "fix" it by moving `DATA_ROOT`, editing Jenkinsfiles or loosening volume
  ownership; diagnose with a live `log stream` (`docs/services.md`).
- `aisland jenkins init` seeds a multibranch job per `Jenkinsfile`; `--local` seeds `<project>.local` with
  `Jenkinsfile.local` embedded. Every job's `config.xml` is rendered from its template in `aisland/templates/jenkins/`
  (`jenkins_job_config.render_job`: autoescape, `StrictUndefined`), which escapes every value it interpolates (a remote url,
  a credential id, a name, a path, a pipeline script) once as XML text — never escape one by hand, never `|safe` [jenkins-job-templates] —
  and a url is printed without its `user:token@` (`jenkins_jobs._without_userinfo`; `tests/test_jenkins_security.py`,
  `tests/test_jenkins_job_templates.py`).
- **Jenkins' port is written once, `JenkinsService.DEFAULT_PORT` (9080); everything that talks to it asks
  `JenkinsService.url()`** (`info`, the safe restart, the scaffold's validation, `aisland tests nightly`'s links, the
  SwiftBar menu, the CLI's `--port` default). Never type the number elsewhere (`tests/test_jenkins_port.py`).
- Jenkins (and its plugin manager) runs on the Java its WAR accepts, by FULL path — never the bare `java` on PATH.
  `services/jenkins_java.py` probes each installed JDK, newest first, with `java -jar jenkins.war --version`; no Java
  version is written into aisland and the bare `openjdk` formula is never one of aisland's tools. On macOS
  `aisland workspace provision install` (never setup) and `aisland jenkins start`, after asking, install the `openjdk@N` the WAR names
  (`tests/test_jenkins_java.py`,
  `tests/test_workspace_provision.py::test_aisland_never_needs_the_floating_openjdk_formula`).

## Docker, containers, trackers

- Colima shares change ONLY through `aisland docker start|restart --mount`: `$HOME:w` always re-stated first, symlinks
  resolved, a missing directory refused, a running engine never restarted for the user
  (`tests/test_docker_service.py`) [containers]. `DockerService.is_running()` is `colima status`'s exit code, never its
  text; everything else reuses it.
- **Another engine's `/var/run/docker.sock` is replaced only after a yes** (`configure_socket`, `provision.agree(…,
  default=False)`): without a terminal or on a no it is left, and a warning names the `sudo rm` / `sudo ln -s` that
  switch it. **`sudo` never runs unasked:** a missing socket is linked only after a yes too (`default=True`), so an
  MCP tool, a script or CI never changes `/var/run` — the warning names the `sudo ln -s` (`tests/test_docker_socket.py`).
- **On Linux the container engine is the system's.** [linux-docker] `docker.docker_service()` is `LinuxDockerService`
  there: `docker info`'s exit code says it runs; `start` refuses naming the install page or `sudo systemctl start
  docker`, `stop` leaves it running, `reset` and `--mount` are refused. aisland never installs, starts, stops or wipes
  it (`tests/test_docker_service.py`).

## Test-run statistics (`pytest_stats.py`)

Root `AGENTS.md` "Two Test Runs". [test-stats] (`tests/test_pytest_stats.py`, the report's `tests/test_stats_report.py`,
`tests/test_workspace_fast_run_default.py`)
- **The plugin records only in an aisland workspace.** [test-stats-workspace] `locations.is_workspace`: from a checkout
  always; from a wheel a folder holding `workspace.toml` or `.gitmodules` (`WORKSPACE_MARKERS`), or the one
  `AISLAND_WORKSPACE` names. A lone repository gets nothing written beside it, and `aisland workspace test-stats
  on|off` refuses there; `--machine-only` refuses where the workspace is no git repository.
- It records a run only when pytest's rootdir is `<workspace>/<project>` (a submodule, or a project folder of a
  workspace without `.gitmodules`); never a `pytester` tmp project, and never a Jenkins build (`JENKINS_URL` set:
  Jenkins' own JUnit record is the CI half the report reads).
- It never costs a run its result: standard library only (no `aisland.paths` import — it loads in every pytest start),
  one append per run, a failed write is one printed line, and so is any other error of its own — `pytest_configure` and
  the recorder's hooks catch `Exception` and print `aisland test stats: this run was not recorded (<error>)`.
- **Recording can be turned off, never removed.** `aisland workspace test-stats on|off [PROJECT…] [--machine-only]`
  (`recording.py`): committed `[workspace] test-stats = false` (no project named: every project, those added later
  included) or a project's own `test-stats` where it differs; this machine `aisland.test-stats.default` /
  `aisland.test-stats.<project>.record`, which wins; a committed switch drops this machine's choice for what it names.
  The plugin reads it with the standard library (`pytest_stats.recording`; a file it cannot read = recorded) and the
  report says a project is off with the command that turns it on [test-stats-switch] (`tests/test_recording.py`).
- The data folder is resolved like `paths.data_dir()`; after touching the entry point, `uv pip install -e ./aisland
  --no-deps`. A nodeid and CI's JUnit `classname::name` meet through `junit_address`, pytest's own mapping.
- The report proposes moves; nothing moves a test between legs but an edit to its `@pytest.mark.slow` (or a
  `FAST_SAMPLE`). Never deselect by history at collection time. It proposes only tests the project still has
  (`stats_report._Present` reads the nodeid's file with `ast`, never an import or a collection; anything it cannot tell
  counts as present; `failed` keeps every test), and a test its file marks `slow` now counts as slow whatever its last
  run recorded. The overrun is weighed in test time (`_parallel`: the median over the latest `PARALLEL_RUNS` (5) whole
  fast runs of each run's test seconds over its wall time, at least 1), and the fast run's time is the median of the
  latest `BASELINE_RUNS` (5) whole fast runs, never the window's; a test the project still has that an earlier fast
  run ran and the latest whole fast run did not counts as slow (`_left_the_fast_run`).
- **aisland's own fast run is PARALLEL — `-n 4` in `addopts`, `pytest-xdist` in `dev`** (four is a GitHub runner's
  cores). The plugin records an xdist run as ONE whole run. A test file still passes alone (`-n 0` runs it in one
  process) (`tests/test_workspace_fast_run_default.py::test_aislands_own_fast_run_is_parallel`).
- A project that registers `slow` sets `addopts = "-m 'not slow'"`. aisland's own pytest settings live in
  `[tool.pytest.ini_options]` alone, never a `pytest.ini` (pytest reads a `pytest.ini` INSTEAD of the table, while the
  scaffold reads the table). A test moves to aisland's slow leg only when `aisland tests report aisland` lists it.

## Doctor

- A recurring environment skew becomes a `check_*` in `doctor.py`, listed in `CHECKS`, not just a note. Checks:
  `opencv`, `huggingface_hub`, `mcp`, `docker-creds`, `colima-tls`, `jenkins-java`, `torch-ext-lock`, `tunnel-relay`,
  `tunnel-connect`, `pylance-memory` (rules for project authors: root `AGENTS.md` "Known Environment Skews").
  [doctor]
  - Cheap, read-only, specific; a failure carries a copy-pasteable remedy (`tests/test_doctor.py`).
  - Imports run out-of-process (`_import_probe`); `run_doctor()` exits 0/1. Every probe in a process of its own has a
    limit, `PROBE_SECONDS` (120): an import past it fails saying so, the VM's TLS probe is inconclusive; the MCP
    `doctor` tool must answer (`TestAProbeThatHangsIsGivenUp`).
  - Each check names the invariant it mirrors (`opencv` ↔ `uv_config._ROOT_UV_BASE`). A probe that cannot run passes
    with a note. Never boot a VM (state from `DockerService.is_running()`). Not a venv linter: `pip check`'s nominal
    lines are not breakage.
- `torch-ext-lock` judges THIS venv's torch, read from its metadata (`_torch_version`, never an import): torch >=
  `_TORCH_OS_LOCK_SINCE` (2.14.1) builds under an OS lock a killed process releases, so the check passes saying so; an
  older torch fails on a lock older than two minutes, only in this Python's own folder (`py<ver>_*`)
  (`tests/test_doctor_torch_extension_lock.py`).
- **`pylance-memory` reads the newest language-server log of a VS Code window opened on the workspace** (`Python
  Language Server.log` under `_vscode_logs_root()`, the window picked by the workspace root in its `Starting service
  instance … for workspace "<root>"` line; an unreadable log is passed over). A `JavaScript heap out of memory` line
  with no `Client: Running with node:` after it fails with the user-settings line `"python.analysis.nodeExecutable":
  "auto"`; after one, a crash fails with `"python.analysis.nodeArguments"` (16 GB); a start on its own Node.js after
  the last crash passes saying when. Both settings are `"scope": "machine"`: the remedy names the USER settings, never
  the workspace file, and aisland never writes the user's VS Code settings. No log (a remote machine) passes with a
  note (`tests/test_doctor_pylance_memory.py`).
- The `colima-tls` fix stays manual and doctor-guided (user ruling 2026-08-31, do not re-litigate). Never auto-install
  the network's CA or prompt for it.

## Bootstrap and `aisland setup`

- `setup.sh` (`tests/test_setup_sh.py`): `WORKSPACE_PYTHON` (3.13) is matched EXACTLY — raise it only when every
  installed framework ships wheels for the next version (TensorFlow first); a missing interpreter comes from `uv python
  install`, never Homebrew; only POSIX syntax above the re-exec-under-bash guard; pip is seeded with `ensurepip`; setup
  runs as `aisland setup -y`.
- `aisland setup` re-applies each managed project's effective source, `ref` included, so a checkout switched by hand
  goes back. Keep a branch with `aisland workspace configure source set <p> local --branch <branch> --machine-only`;
  after a branch switch run `uv pip install -e ./<p> --no-deps`.
- **Which projects a machine has is the `active` plugin (`activation.py`).** A project whose `workspace.toml` table has
  `active` is OPTIONAL, the value its default; without the key it is always part of the workspace (a `uv.toml` makes
  nothing optional). `workspace init --from` clones only projects WITHOUT `active`; `aisland setup` asks about each
  optional one (`resolve`: under `-y` only a project this machine never answered) and keeps the answer in
  `aisland.active.<project>.active` (aisland's key, so `submodule deinit` never erases it), then brings the folders in
  line (`reconcile`): active → init, or `git submodule add` from the file's url and branch when `.gitmodules` lacks it;
  inactive → deinit, never `-f`; a `pypi`/`github` source stays empty and records no choice. It writes what git reads
  from the effective value (`.gitmodules` `active = false` exactly for a default-off project, and
  `submodule.<name>.active`); setup's upkeep keeps an optional project `.gitmodules` lacks. There are no
  `--activate`/`--deactivate` flags and no `aisland reconcile`: `aisland workspace configure active set|unset
  [--machine-only]` acts at once. `setup_git.sh` writes no activation key (`tests/test_activation.py`).
- Every choice a project offers for a selection must be an extra of its own: discovery refuses anything else
  (`DeclarationError`, the file and the table's line), so setup stops before it installs anything and every `aisland
  workspace configure` command stops with one `Error:` line
  (`tests/test_selection.py::test_a_choice_that_is_no_extra_is_refused_naming_its_file_and_line`).
- An internal dependency spec must accept the sibling's declared version, extras included; a bare name makes no claim.
  Bumping a version means every sibling spec must still accept it
  (`tests/test_workspace_dependency_specs.py::test_the_real_workspace_has_no_unsatisfiable_internal_specs`).
- **`aisland setup` installs a dependency CYCLE in one `uv pip install` and skips the dependents of a failure**
  (`projects.install_groups` / `_run_installs`; the edges are `projects.internal_dependencies`, each requirement read
  by `projects.requirement_name` — packaging's PEP 508 parse, PEP 503 names — and `dependency_order`, the SwiftBar
  menu's order among them, is the groups flattened: never a string split of a requirement, never a second graph): two projects can require each other (one through its `dev` extra), so
  installing either alone fails — and every dependent then failed too, burying the first error. `install_groups`
  returns dependency-first strongly-connected components (a cycle is ONE group); `_run_installs` runs one `uv pip
  install -e A -e B` per group, SKIPS (and names) a project whose dependency failed, and prints one failed/skipped
  summary. Don't revert to a per-project loop or edit the extras to hide the cycle (`apply_selection` still installs
  singletons) (`tests/test_install_groups.py`).

## Generated and committed workspace files

- Generated, gitignored, rewritten from discovery (never a hand list): root and per-project `pyrightconfig.json`
  (relative paths; kept out of git with the editable install's `**/*.egg-info/` through the project's own
  `.git/info/exclude`, never a `.gitignore` — setup leaves every project's `git status` as it was, `git_exclude.py`,
  `tests/test_setup_leaves_projects_clean.py`), `project.bashrc`, `.gemini/settings.json`, `.mcp.json`, root `uv.toml`,
  `<workspace>.code-workspace`, and `.project.bashrc.completion`. Never commit editor `extraPaths`
  (`tests/test_pyrightconfig.py`, `tests/test_mcp_config.py`).
  - **The root `pyrightconfig.json` excludes every project inside the workspace** (the workspace file opens each as a
    folder of its own; the root reading them too ran Pylance out of memory). Imports still resolve through
    `extraPaths`; a project outside the workspace (`../x`) is not excluded; a project's own file excludes none
    (`tests/test_pyrightconfig.py::TestWritePyrightConfig::test_the_root_excludes_every_project_inside_it`).
- **The workspace's `env.complete` and `env.example` are generated per machine from each project's own two files**
  (`env.complete` every variable, written by the agent; `env.example` the common ones, written by the user; never
  committed; a generated test per project; `--missing`). [env-example] `env_example.read_project` refuses a line that
  is no comment (the files are copied to `.env`: they set nothing), naming its line, and every project file is read
  before either workspace file is written. A variable is listed once, under its OWNER — the first project in
  `env_example.project_order`, the dependency order, so the library that defines it — `also read by:` the others.
  Setup writes them (a malformed project file is said, setup goes on); `init` seeds a missing `.env` from
  `env.example`, else `env.complete`; nothing ever edits an existing `.env` (`--missing` prints). The per-project test
  comes from `templates/test_env_example.py.template` (standard library + pytest only: a project never imports
  aisland; black-, isort- and flake8-clean at 120); a fix to it is made in the template and `aisland workspace
  env-test` re-run. Never write a project's `env.example` for the user (`tests/test_workspace_env_example.py`).
- `.gemini/settings.json` and `.mcp.json` keep ABSOLUTE venv paths (clients launch from any cwd). MCP discovery matches a
  `<pkg>.server:main` whose module imports `mcp.server`, never a class name.
- **A service tool runs `aisland services <verb> <name>`, never the `aisland <name>` shortcut** (a project's service
  named like an aisland command has none), and the server imports no part of `aisland.cli`
  (`tests/test_server.py::test_a_service_named_like_an_aisland_command_is_still_the_service`).
- **aisland's MCP server is aisland's own, never navigaitor's.** [mcp-server] `aisland-mcp` (`server.py`, the `[mcp]`
  extra — never a base dependency; `mcp>=2.0,<3`, since it drops the SDK's resources and prompts handlers through a
  private attribute) serves a CHOSEN set of tools, pinned by `tests/test_server.py::test_the_tools_are_the_agreed_set`:
  reading tools (`destructive_hint=False`) and the editor, tunnel and service tools; `service_start|stop|restart` take
  ANY service of the workspace [mcp-services] (`name` is a string in the schema, checked when the tool runs against
  `registry.entries`; `--background` only where the entry takes it). Never a tool for what prompts, runs for minutes or
  deletes (`setup`, `clean`, `backups prune`, `* reset`, `jenkins init`, `source set`), and never every click command
  generated as tools. A tool returns ONE JSON object — the data its command prints with `--json`, from the same
  function; a service or tunnel tool runs the `aisland` command itself (`_run_aisland`: this interpreter, `-P`, the
  workspace root, stdin EMPTY — the server's is the client's JSON-RPC stream). An anticipated refusal (a `ToolRefusal`, aisland's own `click.ClickException`) is raised as the SDK's
  `ToolError` (`_refusals_reach_the_client`): from mcp 2.1 any other exception reaches the client as `Error executing
  tool <name>` alone. Not a copy of `_LLMSafeMCPServer` (no confluid here): a plain `MCPServer` with the
  resources/prompts handlers dropped (`tests/test_server.py`, `tests/test_server_tunnel.py`,
  `tests/test_packaging.py::test_the_mcp_sdk_is_held_below_its_next_major_version`).
- **Every MCP server is started with the workspace's environment.** With a `project.bashrc` at the root and bash on
  PATH, each entry of both client files is `bash -c 'source "$0" >/dev/null; exec "$@"' <root>/project.bashrc
  <server>` (`mcp_config._with_workspace_env`): a client otherwise starts the server with its own environment. The
  init's stdout is discarded (the server's stdout is its JSON-RPC stream); never copy `.env`'s values into a client
  file (`"env"`). Without either, the plain command
  (`tests/test_mcp_config.py::TestTheServersGetTheWorkspaceEnvironment`).
- Root `mypy.ini` is committed; setup only APPENDS missing projects to `mypy_path` and never removes one. Never make it
  generated (`tests/test_mypy_path_sync.py`).
- Root `uv.toml` = `_ROOT_UV_BASE` (drops `opencv-python`, keeps `opencv-python-headless>=4.12,<5`, in EVERY workspace;
  `lightning` is dropped only where a project names `pytorch-lightning`, `_CONDITIONAL_OVERRIDES`) + each ACTIVATED
  project's `uv.toml`. The opencv pair stays unconditional (kept by the user): no project names opencv itself, so a
  condition on a project's declaration would switch it off; the cost, no `cv2.imshow`, is on `docs/workspace.md`. A
  private index lives only in its project's `uv.toml`; never commit the root file or put an index in the base
  (`tests/test_uv_activation.py`).
  - Each activated project's `wheels/` folder holding a `wheels.yaml` is a `find-links` path in it, relative to the
    file, so setup and every install in the workspace find the wheels without `install.py` (every such folder, tracked
    or not). uv takes a find-links wheel over the index's of the same version; an editable checkout still wins
    (`tests/test_uv_activation.py::TestWheelsFolders`).
- There is no `config.toml` and no `aisland config` (user ruling 2026-10-23): every committed setting is in
  `workspace.toml` — a project's in its table, a selection's choice under `[workspace]`. `uv.toml` never joins it: uv
  reads only a file of its own settings. [config]
- `<workspace>.code-workspace` is GENERATED and gitignored, written by `editor_files.write_code_workspace` on every
  `aisland setup` and `aisland workspace configure source set|unset` (`--machine-only` included); `aisland workspace
  code-workspace` and `aisland workspace bashrc` run setup's two editor-and-shell steps alone and call setup's own
  functions, never a second writer (`tests/test_workspace_files_commands.py`). Folders: the workspace, each project
  with a checkout in dependency order, then loggair's log folder when it exists. Settings and extensions come from
  `aisland/aisland/code-workspace.json` (strict JSON, no `folders`, no `python.analysis.extraPaths`): a shared setting
  changes there, never in the file. The template names the workspace `WORKSPACE`, which takes the workspace's name
  (`tests/test_code_workspace.py`) [folders].
  - The header (`_CODE_WORKSPACE_HEADER`) says what to do when Pylance crashes (put `"python.analysis.nodeExecutable":
    "auto"` in the USER settings, reload the window). Never write that setting into the template: Pylance declares it
    `"scope": "machine"`, which VS Code ignores in a workspace file; the header is the file's one place for a comment.
  - The log folder is loggair's answer, asked out of process (`python -P -m loggair --json`, from the root, `.env` over
    the environment): never a path typed into aisland, never an import of loggair.
  - This machine's editor theme and colours (`aisland workspace theme|color`, `editor.py`) live in `git config --local
    aisland.editor.*` ONLY — never `workspace.toml`, the template or any tracked file — and `write_code_workspace` lays
    them over the template's settings on every write, the colours unscoped in `workbench.colorCustomizations` (they hold
    whatever the theme); an unreadable hand-edited value is skipped with a warning, never a failed setup [editor] (`tests/test_editor.py`).
  - **A machine with no VS Code theme is a remote one: `theme set` keeps the name as typed, unchecked (user ruling
    2026-10-26).** The VS Code server of a remote window ships no themes. The command and the MCP tool's `note` say it
    is unchecked; a machine with any theme still refuses an unknown name. Never detect "remote" by an environment
    variable (an `aisland tunnel connect` shell has no `code`), and never ask the terminal's `$VSCODE_IPC_HOOK_CLI`
    socket for themes (`tests/test_editor.py`, `tests/test_server.py`).

## Project Sources Are Two-Layered

- Kinds `local` / `github` / `pypi`; branch on `REF_KINDS` / `DETACHED_KINDS`, never on a kind name. No
  `git`/`github` renaming: one word apart, and one deletes a checkout. [sources]
- **Committed: the project's table in `workspace.toml`; per machine: `aisland.source.<project>.<key>` (user ruling
  2026-10-22).** [sources] `source`, `version`, and `distribution` ONLY where it differs from the project's name — a
  git source's ref is the project's own `branch` (`set` without `--branch` keeps it; `--branch` writes it), and
  nothing the table already says is written again. Per machine: `.source`, `.branch`, `.version`, a missing `.branch`
  being the committed one. No backward compatibility: an old `[sources.*]` table or one-key `aisland.source.<project>`
  is simply not read — never refused, named or converted by code. A failed `set` puts `workspace.toml` back byte for
  byte, and so does one that raises or is interrupted (Ctrl-C) during the apply. The commands are the generic ones of `aisland workspace configure` (`setup/configure.py`: a `Plugin`'s keys build
  `set`/`unset`/the listing; there is no `aisland source` group and no `source apply` — setup applies). The effective
  one drives the checkout, the `.venv` and, through the checkout, the generated workspace file's folders. No CI file
  reads either; an override never reaches a tracked file (`tests/test_sources.py`, `tests/test_configure.py`).
- `local` installs `selection.editable_target`, the same target as setup.
- Before a deinit (`setup/source_commands.py`): `pypi` resolves `distribution` (persisted only when it differs from the name); `_pypi_provenance`
  requires `github.com/<owner>/` metadata, the owner of the project's own remote [github-owner]; `_remote_ref_exists`
  checks a `github` ref. Unreachable = warn, never reject.
- Never force `git switch` / `submodule deinit`. Report the remedy for the ACTUAL blocker (`_deinit_remedy`: dirty tree
  vs stale gitlink) and roll the config back (`TestSetProjectSourceRollback`).
- `activation.reconcile` folds in `detached_projects()`. The scaffolder seeds a detached dep from the registry's
  `distribution`. The remote comes from `.gitmodules`; `pip_vcs_url` rewrites `git@host:org/repo`.

## Project mirrors (`aisland workspace configure mirror`)

An aisland command, submodules only, run by `/push-all` and `/ship`. [mirror] (`tests/test_mirrors.py`)
- The primary is the `.gitmodules` remote; the mirror (remote `mirror`) only receives. A sync copies `dev/main`, `main`
  (always) and every other branch of the local checkout, each at the PRIMARY's commit (`ls-remote origin`), and the
  tags — never a local commit; a local branch the primary lacks is reported `absent`, a primary-only branch is not
  copied.
- Never force, never delete, never overwrite a tag: a moved branch and a differing tag are REFUSED with the remedy, a
  mirror-only ref is left. Never add a `--force`.
- Committed: the project's `mirror` in `workspace.toml`; per machine `aisland.mirror.<project>.mirror` (the plugin's key
  pattern, no backward compatibility — an old `[mirrors.*]` table or one-key setting is simply not read); the commands
  are `setup/configure.py`'s generic ones and the action `aisland workspace mirror sync`. Setup re-applies the remote. Only a
  remote marked `remote.mirror.aislandManaged` is changed or removed.
- Network calls run with `GIT_TERMINAL_PROMPT=0`; a project without a mirror is never contacted. `/push-all` and
  `/ship` call `aisland workspace mirror sync` last; change its exit status or wording and those workflows together.

## System tools (`aisland workspace provision`, `provision.py`)

No configure plugin (which projects a machine has decides its tools); aisland's own tools in the package
(`tests/test_workspace_provision.py`).
- A project declares `[tool.aisland.sys-dependencies]` `brew`/`userspace` lists; discovery refuses an unknown key, a
  value that is no list of names and a userspace tool `userspace_tools.USERSPACE_TOOLS` lacks, naming the file and the
  table's line — before anything is installed.
- aisland's own tools are `provision.AISLAND_TOOLS`, never a table in its `pyproject.toml` (a wheel workspace has none
  to read).
- `aisland workspace provision` lists this platform's tools, one row each (who needs it, installed or not), `--json`
  the same rows; `install` installs every one, aisland's own and Jenkins' Java included. There is no `aisland
  provision`.
- **aisland installs its own tool only when a feature first needs it, after asking.** `aisland setup` runs
  `provision.install(root, aislands_own=False)`: the projects' declarations only, no Java. A feature that finds its
  tool missing calls `provision.ask_to_install(name)` (the supervisor's process-compose, `docker start`'s colima and
  docker, `swiftbar start`'s SwiftBar) or asks itself (`jenkins` before downloading its WAR,
  `JenkinsService.ensure_war_asking`, and before installing Java, `java_asking`); `aisland check` without `act` fails
  that gate naming `install_command`. A question is asked only when stdin and stdout are a terminal
  (`provision.agree`); without one, or on a "no", the command refuses with the line that installs the tool and installs
  nothing — a script, CI or an MCP call never installs software as a side effect
  (`tests/test_installs_on_demand.py`) [on-demand-tools].

## What the projects declare (`discovery.py`)

[declarations] (`tests/test_discovery.py`)
- **Two stages, and a project never imports aisland.** STAGE 1 (`discover`) reads every project's `pyproject.toml`
  through `projects.workspace_folders` — nothing installed: `[tool.aisland.selection.<name>]` (the table with
  `question` DEFINES it; every project's `choices` are its menu and each must be an extra of its own),
  `[tool.aisland.service.<name>]` and the entry points in `[project.entry-points."aisland.plugins"]`. STAGE 2 (`load`)
  loads only those, from the declaring project's own distribution, once installed; a plugin is any object with a
  `kind` (`PLUGIN_KINDS`) and that kind's methods (`_KIND_METHODS`) — never a subclass of an aisland type.
- Every refusal is a `DeclarationError` naming the file and the line of the TABLE (`Where`): a selection or service
  defined twice, a choice that is no extra, a missing command, an unknown setting, a value of the wrong shape, a plugin
  missing a method. NOT refused: choices for a selection no project defines (`Discovery.undefined`, shown — a
  workspace without the defining project still sets up), an empty project folder, a plugin not installed yet.
- A new plugin kind joins `PluginKind` and `_KIND_METHODS` together, with a contract test. **The contract carries a
  version**, `plugins.PLUGIN_API` (beside the protocols it versions; a class's `aisland_api`, 1 when absent), checked before the kind: a newer one is
  refused naming the plugin, its project and `pip install --upgrade aisland`, one that is no whole number naming its
  shape, an older or equal one never. Raise it only when a release changes what a plugin gets or must have, with its
  `docs/stability.md` and CHANGELOG lines (`tests/test_discovery.py`).

## Selections (`aisland workspace configure <selection>`, `selection.py`)

[frameworks] (`tests/test_selection.py`)
- **aisland names no selection but its own `extras`.** The others are DISCOVERED (`selection.axes`, over
  `discovery.discover`): the project whose `[tool.aisland.selection.<name>]` has `question` defines it, every
  project's `choices` are its menu. Never hard-code a selection's name or question in aisland; a workspace without the
  defining project has no such selection, and the others' choices install nothing. `extras`' menu is every extra a
  project defines but a self-referential one (structural, never a name match).
- Committed: `[workspace] <name> = [...]` in `workspace.toml`; this machine: `aisland.<name>.selected`
  (space-separated, `""` = none), which wins. No backward compatibility: an old `[tool.aisland] frameworks`, a
  `config.toml` key or an `aisland.frameworks` setting is simply not read.
- A project installs its menu ∩ the effective choice, in its MENU's order, each extra once across selections; once
  `extras` is chosen it owns `dev`. CI reads the committed layer only.
- Unset ≠ empty: setup asks on unset (the selection's own question, no hint line), `-y` with nothing chosen takes
  everything, `--all` takes every extra; an answer is committed. `set` takes the WHOLE list and reinstalls the offering
  projects at once (no `--no-apply`); an unknown name is refused before anything is written; the exit code is the
  number of failed installs. `setup` has no `--framework`/`--extra` (choose with `set` first). `unset NAME…` drops just
  those names from the layer `set` writes (this machine's, which starts from the committed list, with
  `--machine-only`) and refuses one that layer does not choose; no name forgets the whole layer. TAB completes `set`'s
  menu and `unset`'s effective choice, and every project plugin's `PROJECT` from `workspace.toml`.
- **An extra a project marks in `[tool.aisland] manual-extras` is installed only when named.** A blank answer, `-y` and
  `--all` leave it out; the menu marks it `(only when named)`; one project marking a name is enough, since a selection
  is one name for every project. Discovery refuses a marked name that is no extra of the project, and a value that is
  no list, naming the file and the line. A declaration, never a structural guess.
- A project that needs one framework outright (`raidar-ultralytics`) keeps it as a base dependency and offers no
  choice. Never fake an extra.

## Generated CI (`aisland jenkins scaffold`)

A bare `::` pin in this section is in `tests/test_jenkins_scaffold.py`.

- **The files come from a TEMPLATE SET.** [ci-templates] `ci_scaffold.scaffold` works out the project's FACTS
  (`ci_scaffold.project_facts` → `ci_templates.Facts`) and renders every file of the chosen set (`ci_templates.render`);
  it writes no file text of its own, so every rule below is a rule of the templates (`aisland/templates/ci/`) and the
  facts. Delimiters are `<[ ]>`, `<% %>`, `<# #>`, never Jinja's braces (`[[…]]` Groovy lists, `${{ }}` and `[[ ]]`
  shell tests appear in CI files); `StrictUndefined`, `keep_trailing_newline`. The set is chosen by `workspace.toml`
  `[ci] templates` (a folder of the workspace, else an `aisland.ci_templates` entry point name) and a project's `ci = {
  templates = … }`; variables: the set's defaults, `[ci.variables]`, the project's `ci` table — an undeclared one and a
  required one unset are refused naming the file. aisland writes each file's provenance header
  (`ci_templates.header`); a set never needs to. A change to aisland's own output is a change to its templates, held
  byte for byte by the existing pins (`tests/test_ci_templates.py`).
- **CI installs what is RELEASED (user ruling 2026-09-26, do not re-litigate).** The runner files ask PyPI per internal
  dep (`released_on_pypi`: a final, non-yanked version whose metadata points at `github.com/<owner>/`, the owner of
  the dep's remote): released → no install line; else `git+https://github.com/<owner>/<dep>.git@main`
  [github-owner]. They never read a source or `ref`; `aisland workspace configure source set|unset` regenerates no CI
  file; an unreachable PyPI stops the scaffold (`ReleaseProbeError`). Tests stub PyPI with the autouse
  `released_packages` fixture (`tests/test_sources.py`) [sources].
- **A project that does not live on GitHub never names one in its CI files** (a company's repository must not reveal
  the workspace it is developed in). For a project whose folder is not in `github.project_repositories`, the
  unreleased dependencies are never git-URL lines: they come from the wheels it commits, through the extra `aisland
  wheels build --extra` writes (`wheels.extra.remembered_extra`); a dependency no wheel of that extra holds — or no
  such extra — refuses the scaffold, naming the dependencies and `aisland wheels build <project> --extra <name>
  --commit`, before any file is written (`tests/test_github_owner.py`).
- **`Jenkinsfile.local` installs nothing.** `Initialize` only checks, with `python3 -P`, that the project's
  DISTRIBUTION records this checkout, else stops naming `aisland setup`. A new dependency fails until `aisland setup`;
  never add an install back. The orchestrator is `agent none` (`::TestTheLocalPipelineInstallsNothing`) [local-pipeline].
- **The local orchestrator runs every night at 01:00 (`NIGHTLY_CRON`, user ruling 2026-10-06)**, so every project's
  slow run lands in Jenkins' record for `aisland tests report`. The timer is in BOTH the job's properties and the
  pipeline's `triggers` (a pipeline without one clears the job's timer on its first run); the GitHub-backed
  orchestrator gets none (`tests/test_orchestrator_executor.py`) [test-stats].
- **The nightly runs are reported by Jenkins, the test moves made by Claude.** `<workspace>.nightly-check` (seeded by a
  local init only; `NIGHTLY_CHECK_CRON`, weekdays 09:30) runs `aisland tests nightly`: every TIMER run of the
  orchestrator since the previous report — a manual run is never a night — written down with each failed project's
  stage, tests and gate lines; a notification and exit 1 when a night failed, had no run, is still building or Jenkins
  did not answer. The report states what it found, never a guessed cause. Moving tests needs judgment and stays with
  an AI agent's scheduled task, which reads the report (`tests/test_nightly.py`).
- **A manual build is the fast run; the nightly one adds the slow tests (user ruling 2026-10-06).** A project with a
  `slow` marker gets a `SLOW_TESTS` parameter (default false) in `Jenkinsfile.local`, and its Slow Tests stage runs
  only `when` it is true; `Jenkinsfile` and `ci.yml` keep the slow leg on every push. The orchestrator passes
  `SLOW_TESTS: slowTests()` (true for its timer's run or a tick on its own `SLOW_TESTS`) to the projects with the
  marker only. Jenkins drops a parameter a job does not define, so a seeded job (`jenkins_job_config.local_job_config`) and
  the orchestrator define it before their first run (`::TestTheSlowMarkerSplitsTheTestInvocations`,
  `tests/test_orchestrator_executor.py`). A registered `slow` marker splits the test run in all three templates (exit 5
  tolerated); an unmarked project's output is byte-identical.
- **A long stage of `Jenkinsfile.local` has a time limit.** `STAGE_LIMITS` (Unit Tests 15, Slow Tests 60, Verify
  Examples 15, Verify Notebooks 15 min; Node Tests 10) is the ONE table; each such stage's steps run inside the
  `limited` helper (`templates/ci/_limited.groovy.jinja`), which past the limit FAILS the stage with
  `TIMED_OUT_MESSAGE` and rethrows an abort clicked before it. Initialize and the quality gates get none. `ci.yml`'s
  jobs get `RUNNER_LIMIT_FACTOR` (3) times their stage's limit (`runner_limit`); the push `Jenkinsfile` gets none;
  `release.yml`'s jobs: tests 45, build 15, publish 10. Each pytest step first removes the files it writes (`rm -f
  test-report.xml coverage.xml`, `rm -f slow-test-report.xml`): a pytest killed before its report leaves the previous
  build's for the `junit` step. `aisland tests nightly` quotes the timed-out line (`nightly._TIMED_OUT`). Raise a
  limit in the table, never in a generated file (`::TestALongStageHasATimeLimit`,
  `tests/test_packaging.py::test_every_release_job_has_a_time_limit`) [stage-limits].
- Deploying a template change to a running Jenkins: `aisland jenkins scaffold --force`, then POST each job's
  `/job/<name>/config.xml` with a crumb and `Content-Type: application/xml; charset=utf-8` (no charset → HTTP 500). A
  running build keeps its script. `aisland jenkins init --local` also works but stops Jenkins and prompts.
- The pre-install closure follows what CI installs: the root's base deps + its CI extras; a transitive dep's base deps
  + only the extras its requirement names (never every extras group); specs via `packaging.Requirement`
  (`::TestTheInternalDepClosureFollowsWhatCIInstalls`) [closure].
- Every `uses:` in `ci.yml` comes from `GH_ACTIONS` (moving major tags on node24; `setup-uv` stays `@v7`); the
  hand-written `*/.github/workflows/release.yml` are bumped in the same change (`::TestTheWorkflowActionsRunOnNode24`).
  Every coverage run of the three templates passes `--cov-branch` (workspace rule "Keep Branch Coverage On"), and
  aisland's own `pyproject.toml` sets `[tool.coverage.run] branch = true` (`::test_every_coverage_run_measures_branches`).
- `playwright` in a `dev` extra: setup and the runner files fetch chromium (`--with-deps` in `ci.yml`);
  `Jenkinsfile.local` fetches nothing (`tests/test_playwright_browser.py`) [playwright].
- **A `package.json` with a `test` script is a Node test run CI runs.** [node-tests] `ci_scaffold.node_test_folders`
  reads the `package.json` files git sees in the project (tracked, or untracked and not ignored; a walk skipping
  `node_modules` and hidden folders outside git) and keeps each folder whose `scripts.test` is a non-empty string (the
  fact `node_tests`, last in `Facts` and defaulted). Each gets a Node Tests stage after the unit tests: `npm ci` (else
  `npm install`) and `npm test` in `Jenkinsfile` and `ci.yml`'s `node-tests` job (`setup-node`, Node.js 24); in `Jenkinsfile.local` only `npm
  test`, a folder without `node_modules` stopping the stage with `<project>/<folder> has no node_modules - run npm
  install there` — the local pipeline installs nothing. The venv is first on the stage's `PATH` (`PATH+VENV`). A
  project without such a folder gets byte-identical files (`::TestAPackageJsonWithATestScriptGetsNodeTests`).
- Verify Examples runs `examples/*.py` and `examples/*/run.py` (`::test_scaffold_emits_directory_example_runner`).
- A declaration a project already makes (a marker, a dependency) is the opt-in; never add an aisland knob for it. After
  a header change, re-scaffold every project (root Generator Provenance rule).

## The agent harness (`setup/harness.py`, `harness/`)

[harness] [harness-sets] (`tests/test_harness.py`, `tests/test_harness_sets.py`, `tests/test_packaging.py`)
- **A set's name is one plain folder name** (`harness._SET_NAME`: letters, digits, `.`, `_`, `-`, starting with a
  letter or digit), checked before a git set is cloned or its old clone removed — it names `.agents/<name>/` and the
  clone's folder under `clones_dir()`, so `..` or `/` would reach outside. A place a set writes never has `.git`, in any
  letter case, as any part (`tests/test_harness_sets.py`).
- **A workspace combines harness sets.** [harness-sets] `workspace.toml` `[harness] sets`, in order, `["aisland"]` when
  absent, `[]` installs nothing: `"aisland"`, a workspace project (its `[tool.aisland] harness` folder, else the
  project itself), a workspace folder, an `aisland.harnesses` entry point name, `{ git, ref, name?, exclude? }` (cloned
  into `harness.clones_dir()`, fetched again only when url or ref changes; no ref, an unreachable url or a missing ref
  is refused naming the line). A set's `harness.toml` (`rules`, `commands`, `scripts`, `[files]`) says what it brings;
  without one the usual places are read (`harness._usual_places`, never the `.claude/commands` link aisland makes).
  A set's `harness.toml` and a template set's `template.toml` carry `format` as `workspace.toml` does
  (`toml_places.format_refusal`: none is 1, a newer one refused naming the line and the upgrade, a value that is no
  whole number refused; `harness.SET_FORMAT`, `ci_templates.SET_FORMAT`); aisland's own sets write `format = 1`
  (`tests/test_harness_sets.py`, `tests/test_ci_templates.py`).
  Rules land in `.agents/<set>/AGENTS.md`, `.agents/rules.md` imports them in order, and the workspace `AGENTS.md`
  imports `@.agents/rules.md` (never edited: setup names the line). Two sets placing one path are REFUSED before
  anything is installed, naming both — never a precedence by position; `exclude` leaves a file out and one naming
  nothing is refused. A new place an agent reads joins `_usual_places`; a new `harness.toml` key joins `_SET_KEYS` and
  `docs/stability.md`.
- **A harness file reaches aisland through its commands, never its modules** (`aisland workspace projects` for the
  dependency order, never `python3 -c "from aisland…"`): `docs/stability.md` keeps the modules out of the interface,
  and the files run in other workspaces against other aisland releases.
- `harness/` holds ONLY what every aisland workspace shares: no workspace's project names, private names, domain
  rules or dated rulings — a "user ruling 2026-…" is one workspace's decision, kept in that workspace's records
  (`::test_the_harness_names_no_project_of_the_workspace_it_was_written_in`,
  `::test_the_harness_carries_no_dated_ruling_of_the_workspace_it_was_written_in`); a workspace's own rule goes in its
  `AGENTS.md` below the import line. A command cites a rule as `aisland rules → "<bold heading>"`, and
  `harness/AGENTS.md` must hold that heading (`::test_every_section_a_command_cites_is_in_the_rules`): rename a rule and
  its citations together.
- From a checkout, links; from a wheel, copies (`locations.FROM_CHECKOUT`). A copy is replaced only while its
  fingerprint is the one `.agents/installed.json` recorded, or it equals its source; anything else is kept and named —
  never overwritten, never deleted. A file in `.agents/` aisland does not ship is never touched, and an existing
  workspace `AGENTS.md` is never edited.
- Every installed file is ignored by its own path, never by its folder, so a workspace's own command stays
  committable. Every project folder (a direct subfolder with a `pyproject.toml`) gets `.claude/commands` too — Claude
  Code reads only the folder a session starts in — excluded through ITS repository's `.git/info/exclude`, never a
  tracked `.gitignore`.
- Setup installs it before provisioning; the commands that commit carry `model: sonnet`. `harness/` is no package: a
  new file kind there joins `[tool.setuptools.package-data]`
  (`tests/test_packaging.py::test_the_package_holds_the_templates_and_the_git_scripts`).

## Shell completion

- `aisland completion install` writes all three families into `.project.bashrc.completion`; `setup.sh` calls it in one
  line. Never move it back into shell (`tests/test_completion.py`) [completion].
  - liquifai apps: `install_for_apps`, in-process. click apps: `_<PROG>_COMPLETE`, bash guarded on `BASH_VERSINFO >=
    4.4` — `aisland` (`NON_LIQUIFAI_CLIS`, its only row) and every script a project lists in `[tool.aisland]
    click-commands` (`completion.workspace_click_clis`, read by discovery: never import a script to tell). Never name a
    workspace CLI in aisland. argcomplete: no member; never add a `streamstudio` row.
  - liquifai is never a dependency of aisland, not even behind an extra: a venv without it has no liquifai app, so the
    install writes the other families; the default shell is `completion.login_shell` (`$SHELL`'s name, else bash).
    `--target-rc` appends marked blocks only; program lookup is venv-first.
  - **The generated block LEAVES a non-interactive shell at once** (`_INTERACTIVE_ONLY`, the block's first line after
    its provenance header): every line below it RUNS its CLI to get the script, so a non-interactive shell that reads
    the file executes them — and with `BASH_ENV` pointing at the file that sources this one, click's own `bash --norc
    -c 'echo "${BASH_VERSION}"'` probe reads it and runs the CLI again, which probes again. Unguarded it filled a
    machine's process table and is SELF-SUSTAINING: a clear is `while` over `pkill -9 -f BASH_VERSION; pkill -9 -f
    'venv/bin/aisland$'` (anchored: the bare, argument-less invocations are the bomb's; `aisland tunnel …` is a
    person's). Never add a line to the block that runs a program before the guard
    (`tests/test_completion.py::test_the_block_does_nothing_in_a_NON_interactive_shell`).
- **An empty word after an aisland command with nothing left to fill offers its options** (click itself waits for a
  `-`). `cli._base._Command.shell_complete` completes `""` as `"-"`; every group is a `cli._base._Group`
  (`command_class = _Command`, `group_class = _Group`), so a command added with `@<group>.command` gets it without a
  `cls=`. A group still offers its commands only; an argument still to fill comes first. A new group class derives from
  `_Group`, never `click.Group` (`tests/test_cli_option_completion.py`).

## `aisland clean` / `aisland backups`

- Clean MOVES into `aisland/data/backups/<project>/<stamp>/`, never deletes (never `git clean -Xdf`)
  (`tests/test_clean.py`) [clean]. ONE project per run (never `--all`); the workspace root is refused by name and path;
  the project dir must be its repo's root. An entry containing the store is split (`_split_around`); the sweep deletes
  only `STAMP_FORMAT`-named directories (`backups.prune_backups`), and a clean lists the backups it sweeps and asks
  about them too, also with nothing to move (`clean._agreed`); symlinks are measured, never followed.
- Restore moves and consumes the backup: a different copy is skipped (exit 1; `--force` asks), an identical one is
  reported `already` and dropped. `backups._same_content` compares bytes, never size+mtime. Manifest paths are
  validated (`backups._safe_rel`: absolute, `..` and the empty path refused). `backups prune` confirms first
  (`tests/test_backups.py`).

## `aisland wheels` (`wheels/`)

An aisland command, only the named project's closure, both checks; each flag's mechanism is a decision record in
`docs/architecture.md`. The invariants: [wheels] (`tests/test_wheels.py`)
- A wheel is built from `git archive HEAD` with `SOURCE_DATE_EPOCH` = the commit's time, never from the checkout (a
  stale `build/lib` leaks deleted files into it; the build's clock makes every rebuild a new file).
- A project with uncommitted changes is refused, naming its files; one with no checkout, naming `aisland workspace
  configure source set <project> local`. Nothing is built after a refusal. `--force` builds a dirty project from its
  working tree (`wheels.export.export_worktree`: `git ls-files --cached --others --exclude-standard`, never the bare folder), its
  commit written `<sha>+uncommitted`; `--force --commit` is refused before any build.
- "On PyPI" = `wheels.published.pypi_state` (the project's GitHub owner in the metadata, a file not yanked, pre-releases INCLUDED); never
  replace it with `released_on_pypi` (final only, CI's question). An unreachable PyPI stops the run.
- The empty venv runs from a scratch folder, so the root `uv.toml` is never read; the target's own is passed with
  `--config-file`. The exit status follows the install and the imports only; *behind* (a file differs from PyPI's
  wheel) is reported, never an exit 1.
- `--no-check` skips BOTH checks and nothing else (the lookup, the refusals, the wheels and the generated files are
  those of a checked run, byte for byte); the report says `checked: false`; `--no-check --commit` is refused before any
  build.
- Every module is imported, not only the packages (`__main__` skipped). A package that fails, or a module missing a workspace project's
  module, is a break; a deeper module missing what no workspace project ships (an optional extra's) is a warning, never
  an exit 1 — only a `ModuleNotFoundError` of such a module; any other error of a deeper module is a break when a
  published project is behind, blamed by `attribute_failure` (the innermost traceback frame in a behind project's code,
  else the behind projects whose checkout-only code uses a name the message quotes, else every behind project;
  nothing behind, nobody blamed) (`TestTheRunWithFakedBuilds`, `TestEndToEnd`).
- What is built is a scope (`WheelsScope`): the default ships what PyPI lacks, `--include-published` every needed
  project with a checkout, `--all` every workspace project with one; a dirty one is refused in every scope.
- `install.py` (a copy of `wheels/install.py`) names each chosen wheel by its FILE — pip takes PyPI's same-version
  wheel over one it only finds through `--find-links`; never go back to `--find-links` alone
  (`tests/test_wheels_install.py`). `wheels.yaml`'s `install:` values are the user's (a rerun keeps them; an unreadable
  file stops the run at its line before any build); `wheels.install.read_wheels` is its one reader.
- The folder is git-ignored (`wheels/.gitignore`: `*`) unless `--commit`, which commits `wheels/` ITSELF — that path
  only, only after a passing check, never the zip; a tracked folder without `--commit` is reported with its `git rm -r
  --cached`, never untracked by the command (`tests/test_wheels_zip.py::TestGit`).
- **A target not on GitHub gets a folder that says nothing of GitHub** (a project hosted off GitHub must not reveal the
  workspace it is developed in) [wheels-off-github]: for a target not in `github.project_repositories`, `wheels.yaml`
  and the README carry no branch or commit, and every wheel built is scrubbed (`wheels.scrub.scrub_wheel`) of its
  METADATA's description, its URL fields naming `github.com` and its author/maintainer fields naming the workspace or
  a GitHub owner (`WheelsPlan.hidden_names`), RECORD following; code, requirements and entry points stay as built, and
  a target on GitHub gets byte-identical files (`tests/test_wheels_scrub.py`).
- `aisland wheels` is a group (`build`, `zip`, `install`); an unknown first word is `build`'s project. The zip runs with
  plain `python` (`wheels/unzip.py`, standard library only), refuses another project's folder before unpacking and is
  byte-identical for an unchanged folder; `install` refuses the workspace's `.venv` (`tests/test_wheels_zip.py`).
- `--include-project`: the target's OWN wheel, kept OUT of `built` (`WheelsReport.project_wheel`) and listed LAST in
  `wheels.yaml`; `install.py` installs it in place of `-e ..` while its `install:` is true (`--no-project` drops both);
  the check installs that wheel, never a second build; a folder that installs its own wheel zips with `STANDALONE =
  True`. Without the flag every file is byte-identical.
- `--bundle`: ONE merged wheel `<project>-bundle` in `wheels/bundle/` (`wheels/bundle.py`; pip knows only that name —
  accepted); its requirements come from each bundled project's `pyproject.toml`, never METADATA; an unsatisfied
  requirement, a non-pure wheel, a path with two contents, an entry point with two targets and `dynamic` dependencies
  are REFUSED, never resolved. The bundle is checked alone, its failures exit 1 and block `--commit`; the zip never
  holds it. Without the flag every file is byte-identical and a stale bundle is removed
  (`tests/test_wheels_bundle.py`).
- `--extra NAME` writes the extra and a `[tool.uv.sources]` entry per wheel into the target's `pyproject.toml`, between
  marker lines a rerun replaces and nothing outside them changes (`wheels/extra.py`); REFUSED before any build: an
  extra NAME missing from `[tool.aisland] manual-extras`, a hand-written extra or source for a built wheel, a broken
  marker, each naming its line; `--commit` refuses a `pyproject.toml` with changes of its own. A flag, never tied to
  `--include-project`. **The block is remembered** (`remembered_extra`): a build without `--extra` and without
  `--include-project` keeps it current; with `--include-project` and no `--extra` it is left and the report says so;
  another name beside the block is refused. A project without the block gets byte-identical files
  (`tests/test_wheels_extra.py`).
- **`aisland wheels prune` / `reset`** (`wheels/prune.py`, `tests/test_wheels_prune.py`) [wheels-prune]:
  - What goes is exactly the `wheels/**.whl` blobs no tip holds — this clone's branches and tags, origin's branches;
    every kept tip's tree is checked unchanged after the rewrite. Without `--apply` nothing changes.
  - The strip is git-filter-repo with `--prune-empty never --prune-degenerate never --preserve-commit-hashes`, so it is
    a function of each commit and `reset` re-runs it to land on origin's ids. Never drop emptied commits or rewrite
    messages, and never replace `reset` by a rebase.
  - `--apply` refuses tracked changes, a branch ahead of or behind origin's and a differing tag, naming each, before
    anything changes; it bundles every ref first and prints the restore command. The push is ONE atomic push leased per
    ref on the fetched sha, never a `+` refspec, which overrides the lease.
  - origin's `refs/wheels-prune/map` is a chain, one commit per prune (`stripped-blobs`, `commit-map`), text only;
    `reset` takes each prune the clone still reaches with that prune's own list. Its name carries no workspace or tool
    name (a Bitbucket project must not reveal them).
  - The root's gitlink moves only to the rewritten id of the commit it already records, committed alone from a scratch
    index — never to a newer commit, so an untracked project stays unrecorded; `reset` does the same through every
    prune's map composed.
  - **`reset` runs on a clone in ANY state (user ruling 2026-10-29).** Changes no commit holds go into
    `refs/wheels-prune/set-aside` (`git stash create`), are rewritten with the refs and applied back with `--index` —
    never `git stash push/pop` (filter-repo moves `refs/stash` without its reflog); a failure keeps the ref and names
    the command. Never add a refusal back to `reset`; `prune --apply` keeps its refusals (it pushes).
  - git-filter-repo runs as `python -m aisland.wheels.filter_repo` (`wheels/filter_repo.py`), which reads the config with `--null`:
    2.47.0's own reader dies on a value of several lines. Never rewrite the user's config to suit it. Reflogs expire for
    every ref but `refs/stash`, symbolic refs included; a detached HEAD is carried through `refs/wheels-prune/head`.
- **A project whose setuptools discovery would take `wheels/` for a package is refused before anything is built
  (refuse with the fix, never hide the folder or edit the project).** `wheels_folder_problem`: a setuptools project (or
  no `build-backend`) without `setup.py`, with no `packages` list or `py-modules`, and either automatic discovery in a
  flat layout (no `src/`) or a `packages.find` reaching `wheels`. The message names the project's own package folders in
  the `include` setuptools recommends (`tests/test_wheels_folder_discovery.py`).
- TAB after `wheels build` / `wheels zip` / the short form `wheels` offers the projects (`cli.wheels._complete_projects`:
  checked out here; `zip` only those with a `wheels.yaml`). It reads folders and `pyproject.toml` only and an
  unreadable workspace offers nothing (`tests/test_wheels.py::TestShellCompletion`).
- Tests reach no network: the end-to-end tests run the real uv with `UV_OFFLINE=1`, a fresh cache, and
  `wheels.check.PYPI_INDEX_ARGS` pointed at a local folder (`offline_uv`); the pip tests run with
  `PIP_CONFIG_FILE=/dev/null` against a local PEP 503 index. A wheel a test writes by hand
  (`tests/test_wheels.py::_wheel`) gives every member one fixed time (`wheels.files.ZIP_TIME`), as a real build stamps
  its commit's.

## Workspace git aliases (`setup_git.sh`, `git_pull_all.sh`)

- Aliases live in LOCAL config (root + every active submodule), never global (`tests/test_setup_git_aliases.py`).
- **Every alias is stored on ONE line.** `set_alias` turns a body's newlines into spaces: git-filter-repo (which
  `aisland wheels prune` runs) reads `git config --list` line by line as `key=value` and crashed on the multi-line
  `tag-all`, `status-all` and `safe`. A body may be written over several lines in the script; each statement ends with
  `;` (`::test_every_alias_is_one_line_so_git_config_list_reads_as_key_value_pairs`).
- **A workspace without `.gitmodules` is read as if each project folder were a submodule.** [folders-as-submodules]
  `projects.workspace_folders` is the project list: `.gitmodules`' submodules when it exists, else every direct,
  non-hidden subfolder holding a `pyproject.toml`, active. A new reader of "the workspace's projects" uses it, never
  `submodule_active_map` (which stays the submodules only: activation, deinit, mirrors; read through
  `gitmodules.read`, ONE `git config --get-regexp` process — a process per submodule made `aisland --help` take 79 s on
  a loaded machine). What needs a submodule is refused naming the reason: a `github`/`pypi` source (it would empty the
  folder for good), a mirror, a wheel of a project in no git repository (`tests/test_folder_workspace.py`,
  `tests/test_submodule_active_map.py`).
- **A link to a folder inside the workspace is a short name, never a project.** [aliases] Every reader of the
  workspace's projects skips a folder for which `locations.is_alias(folder, workspace)` holds (`discover_projects`,
  `workspace_folders`, `harness._project_folders`, `sources.known_projects`, `workspace.file.workspace_projects`); a new
  reader does the same, never an `is_symlink` test of its own. A link to a folder OUTSIDE the workspace stays a
  project. The generated `.code-workspace` names such a folder by its link (`locations.alias_names`), its `path` the
  real folder; `workspace.toml` writes it as the project's `path` (`tests/test_symlinked_folders.py`).
- Without `.gitmodules`, `setup_git.sh` gives every subfolder that is its own clone (and the root when it is a
  repository) every workspace alias with the root baked in (`GIT_PULL_ALL_ROOT`): `git_pull_all.sh`'s `folder_main`
  merges each clone on ITS OWN branch, the root last, and records no pointer; `git_push_all.sh` pushes each clone's
  branch and the root only when all pushed; `git_folder_all.sh <verb>` loops `status-all` (root first), `switch-all`,
  `merge-all`, `tag-all` and runs `safe` in the clone typed in. No clone and no root repository: no aliases. The
  scripts run under bash 3.2 (`/bin/bash`): brace a variable before a non-ASCII character (`${noun}…`)
  (`tests/test_folder_git_aliases.py`, run under `/bin/bash`). `setup_git.sh [root]`: from the checkout the `pull-all`
  alias keeps its root-relative form; installed elsewhere it names the script by its path and passes the root in
  `GIT_PULL_ALL_ROOT`.
- `git pull-all` is `git_pull_all.sh`, run from the workspace root wherever it is typed; the `/pull-all` and
  `/push-all` commands (`aisland/harness/commands/`) call it — change its `needs you:` wording and those commands
  together (`tests/test_git_pull_push_all.py`) [pull-all].
  - A pointer conflict is settled only to the submodule's checkout when it contains BOTH sides. A pointer is recorded
    only for commits that came from origin, never backwards; the record commit takes only its pointers.
  - `git fetch` + `git merge`, never `git pull` (`pull.*` settings, `submodule.recurse`). Uncommitted edits are touched
    only under `--autostash`, only the files the merge needs.
  - **A clone still on a pruned history is never merged.** Each fetch takes origin's configured refspecs plus
    `+refs/wheels-prune/*:refs/wheels-prune/*`; a repository whose HEAD reaches an `<old>` of any record's `commit-map`
    is listed `not merged: … run aisland wheels reset <project>` and left — the merge would bring the stripped history
    back [wheels-prune]. `/pull-all` leaves the reset to the user.
  - **A root without an `origin` remote (what `workspace init` makes)** is neither fetched nor merged, each submodule is
    pulled on ITS OWN branch, and what came in is still recorded; `push-all` skips that root and says so. With an
    origin, a submodule off the root's branch stays listed, never pulled — unless it is on a `dev/<name>` branch of its
    own, which is merged with `origin/dev/<name>` and never recorded in the root (user ruling 2026-10-28, do not
    re-litigate: recording it would point the root's branch at work only that `dev/<name>` holds).
  - **An untracked submodule is never recorded.** `ignore = all` for it (`tracking.py`) — this machine's `.git/config`
    first, else `.gitmodules` — and step 4 skips it; it is still pulled. Never record it "just this once": the root
    keeps the commit it last recorded. `/commit-all` and `/commit-session` leave it unrecorded, never `git add -f`
    (`aisland/harness/commands/commit-*.md`).
  - bash 3.2 (`/bin/bash` on macOS): no `wait -n`, associative arrays, `mapfile`; the tests run it under `/bin/bash`.
- `git push-all` pushes every submodule before the root, and the root only when they all pushed.
- **`git safe` pushes the branch it is on and no other**: on no branch it stops before the first gate, and a refused
  push is an error with the commit kept — never a fallback push to `dev/main` or any branch the user did not name
  (`tests/test_setup_git_aliases.py::test_safe_pushes_only_the_branch_it_is_on_and_a_refused_push_fails`).
