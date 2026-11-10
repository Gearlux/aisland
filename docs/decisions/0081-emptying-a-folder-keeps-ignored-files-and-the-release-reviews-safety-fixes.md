# Emptying a project folder keeps its ignored files, and the release review's safety fixes

*2026-11-10* (follows "The release review's service, tunnel and MCP fixes")

## Context

The third 0.1.0 review found what deletes, overwrites or asks where nothing should. `git submodule deinit` without
`-f` refuses uncommitted work, yet deletes what git ignores with the rest of the folder: switching a project to a
`pypi` source, or `configure active set <p> false`, deleted a run's checkpoint under `runs/` and exited 0. `wheels
prune` kept the wheels of every branch and tag but not of the commit the workspace root records, so the root was
moved to a commit whose `wheels.yaml` named files origin no longer had. `setup -y` still asked about `sudo tee
/etc/shells`, and without a terminal the prompt aborted setup half way. A `workspace.toml` whose `project` or
`workspace` was no table crashed with a traceback. `configure mirror`, a listing, added remotes and made four network
calls per project. Setup replaced the whole server list of `.mcp.json`, servers a person added included. On a Mac's
zsh, completion wrote zsh's script into the file the bash `project.bashrc` sources. A `CDPATH` made `project.bashrc`
read the wrong root. A CI template set could name `../x` or `.git/hooks/…` as a file to write. The orchestrator read
`lib~=1.0` as a project of that name and built it beside its dependency. Jenkins saved its `config.xml`, which holds
`.env`'s values, readable by everyone. The generated shell steps named the venv unquoted. The release workflow ran its
tag guards after 45 minutes of tests, kept a token beside every dependency's code and built with whatever toolchain
was newest.

## Decision

- Before a project folder is emptied, its git-ignored content is listed (`projects.ignored_content`,
  `git status --ignored=matching`); anything but what is made again — a cache folder, a `.venv` or `node_modules`, a
  `*.egg-info`, the files setup writes there — refuses the change, naming the paths and `aisland clean <project>`, which moves them into a backup.
- A prune keeps the wheels of a detached HEAD and of the commit the root records (`prune._kept_commits`), and checks
  their rewritten twins' trees as it checks a ref's.
- `console.can_ask()` is the one test of a person at the terminal; the shell switch asks only then and never under
  `setup -y`, and otherwise prints the two steps.
- `workspace.toml`'s `project` and `workspace` must be tables, refused naming the line. A sub-project's broken
  `pyproject.toml` is refused naming it.
- `configure mirror` reads the configuration alone; `mirror sync --dry-run` fetches and shows what would be pushed.
- The MCP client files replace only aisland's own servers: an entry starting a console script of the workspace's
  venv, directly or through the shell init.
- Completion writes the script of the shell that reads the file: bash for a `*bashrc*` file, zsh for a `*zshrc`.
- Every `$(cd … && pwd)` of the shell files is `CDPATH= cd --`; `setup.sh` keeps a `.vscode/tasks.json` that is no
  link.
- A template set's files and templates stay below their folders and out of `.git`, as a harness set's do.
- The orchestrator's phases follow `projects.requirement_name` and each project's folder.
- Jenkins' home is `0700`; a view carries one `<owner>`; every generated `sh` step quotes the venv and the
  workspace path.
- `release.yml`: a `guards` job first, `contents: read` for every job, no credentials kept by the tests' checkout, a
  macOS leg, and an exact build toolchain without isolation.

## Consequences

Each is pinned by a test that failed before the change (`tests/test_sources.py`, `tests/test_activation.py`,
`tests/test_wheels_prune.py`, `tests/test_provision.py`, `tests/test_workspace_file.py`,
`tests/test_workspace_model.py`, `tests/test_mirrors.py`, `tests/test_mcp_config.py`, `tests/test_completion.py`,
`tests/test_project_bashrc.py`, `tests/test_setup_sh.py`, `tests/test_ci_templates.py`,
`tests/test_orchestrator_aisland.py`, `tests/test_jenkins_security.py`, `tests/test_jenkins_port.py`,
`tests/test_jenkins_scaffold.py`, `tests/test_packaging.py`). A person whose project folder holds ignored data runs
`aisland clean <project>` before the change; nothing is deleted for them. The other projects' generated Jenkinsfiles
take the quoting at their next `aisland jenkins scaffold --force`.
