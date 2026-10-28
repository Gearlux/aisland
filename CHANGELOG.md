# Changelog

All notable changes to aisland are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - unreleased

The first release on PyPI: `pip install aisland`.

### Added
- **`aisland workspace init <folder>`** creates a workspace — one git repository whose projects are submodules,
  written down in `workspace.toml` — and `aisland setup` sets it up: the system tools the projects declare (aisland's
  own are installed when a command first needs one, after asking), each project installed
  editable into the workspace's one `.venv`, the git aliases (`git pull-all`, `git push-all`), the editor's
  workspace file and the shell start-up file `project.bashrc`. While `init` clones, it says each project as it
  starts and ends, with git's download progress on a terminal. A project written into `workspace.toml` with its url
  is cloned by the next `aisland setup`, and setup leaves every project's `git status` as it found it. An `init` that
  fails half way removes the folder it made, so the same command runs again.
- **`aisland workspace configure`** chooses, per project, where it comes from (`source`: a checkout, GitHub or
  PyPI), whether this machine has it (`active`) and where it is mirrored (`mirror`), and per workspace the extras
  the projects offer.
- **`aisland services`** runs the local services under process-compose or Docker — Jenkins, the container engine,
  SwiftBar — and a service a project declares in its own `pyproject.toml` or implements in its own
  code (its class may name the question `reset` asks first); `aisland --help` names the project that brings each.
- **`aisland jenkins scaffold`** writes a project's `Jenkinsfile`, `Jenkinsfile.local` and GitHub `ci.yml`.
- **`aisland doctor`** checks the environment and prints the command that fixes what it finds.
- **The workspace's `.env`** is read the way `project.bashrc` sources it — quotes, `export`, `$VAR` — by the services
  and Jenkins alike.
- **`aisland workspace env-example`** writes the workspace's `env.complete` (every environment variable the
  projects read) and `env.example` (the ones a person normally sets) from each project's own two files, every line
  commented out, a variable several projects read listed once under the project that defines it; a new machine
  copies `env.example` to `.env`, and `--missing` prints the variables an existing `.env` does not mention.
  `aisland setup` writes both, `workspace init` starts `.env` from them. `aisland workspace env-test` writes each
  project's test that fails when its code reads a variable its `env.complete` does not list.
- **`aisland wheels build`** refuses, before it builds anything, a project whose setuptools discovery would take the
  `wheels/` folder it writes for a package, and names the `[tool.setuptools.packages.find]` table that fixes it.
- **`aisland wheels build <project>`** builds the wheels a project needs that are not on PyPI, for a machine
  without the workspace. With `--extra NAME` it also writes the extra NAME into the project's
  `pyproject.toml`, so `uv pip install -e ./<project>[NAME]` installs the project with every wheel.
- **`aisland wheels prune <project>`** strips from a project's history the committed wheel versions no branch or tag
  holds and force-pushes origin; **`aisland wheels reset <project>`** brings every other clone onto the pruned
  history (the `prune` extra).
- **A pytest plugin, `aisland_test_stats`,** loaded by every `pytest` in the environment aisland is installed in,
  records each run of a project — its seconds and each test's outcome — in `.aisland/data/test-stats/` in the
  folder above the project; `aisland workspace test-stats off [PROJECT…] [--machine-only]` turns it off for a
  workspace, a project or this machine, and `pytest -p no:aisland_test_stats` leaves one run out.
- **`aisland tests report <project>`** reads every recorded test run, and **`aisland clean <project>`** moves a
  project's ignored files into a backup **`aisland backups`** can restore.
- **`aisland completion install`** writes shell completion for every command-line tool in the workspace; a project
  names its click scripts in `[tool.aisland] click-commands`.
- **`aisland tunnel`** opens a VS Code Remote Tunnel to a remote machine, and `aisland tunnel connect` (the
  `[connect]` extra) a terminal on it over that tunnel.
- **`aisland-mcp`** (the `[mcp]` extra) offers a chosen set of these commands to an AI client.
