# Changelog

All notable changes to aisland are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - unreleased

The first release on PyPI: `pip install aisland`.

### Added
- **`aisland workspace init <folder>`** creates a workspace — one git repository whose projects are submodules,
  written down in `workspace.toml` — and `aisland setup` sets it up: the system tools, each project installed
  editable into the workspace's one `.venv`, the git aliases (`git pull-all`, `git push-all`), the editor's
  workspace file and the shell start-up file `project.bashrc`. While `init` clones, it says each project as it
  starts and ends, with git's download progress on a terminal.
- **`aisland workspace configure`** chooses, per project, where it comes from (`source`: a checkout, GitHub or
  PyPI), whether this machine has it (`active`) and where it is mirrored (`mirror`), and per workspace the extras
  the projects offer.
- **`aisland services`** runs the local services under process-compose or Docker — Jenkins, MLflow, the container
  engine, SwiftBar — and a service a project declares in its own `pyproject.toml` or implements in its own
  code (its class may name the question `reset` asks first).
- **`aisland jenkins scaffold`** writes a project's `Jenkinsfile`, `Jenkinsfile.local` and GitHub `ci.yml`.
- **`aisland doctor`** checks the environment and prints the command that fixes what it finds.
- **`aisland wheels build <project>`** builds the wheels a project needs that are not on PyPI, for a machine
  without the workspace. With `--extra NAME` it also writes the extra NAME into the project's
  `pyproject.toml`, so `uv pip install -e ./<project>[NAME]` installs the project with every wheel.
- **A pytest plugin, `aisland_test_stats`,** loaded by every `pytest` in the environment aisland is installed in,
  records each run of a project — its seconds and each test's outcome — in `.aisland/data/test-stats/` in the
  folder above the project; `aisland workspace test-stats off [PROJECT…] [--machine-only]` turns it off for a
  workspace, a project or this machine, and `pytest -p no:aisland_test_stats` leaves one run out.
- **`aisland tests report <project>`** reads every recorded test run, and **`aisland clean <project>`** moves a
  project's ignored files into a backup **`aisland backups`** can restore.
- **`aisland completion install`** writes shell completion for every command-line tool in the workspace.
- **`aisland tunnel`** opens a VS Code Remote Tunnel to a remote machine, and `aisland tunnel connect` (the
  `[connect]` extra) a terminal on it over that tunnel.
- **`aisland-mcp`** (the `[mcp]` extra) offers a chosen set of these commands to an AI client.
