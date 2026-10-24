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
  workspace file and the shell start-up file `project.bashrc`.
- **`aisland workspace configure`** chooses, per project, where it comes from (`source`: a checkout, GitHub or
  PyPI), whether this machine has it (`active`) and where it is mirrored (`mirror`), and per workspace the extras
  the projects offer.
- **`aisland services`** runs the local services under process-compose — Jenkins, MLflow, Label Studio, FiftyOne,
  Aim, ClearML, IQEngine — and a service a project declares in its own `pyproject.toml`.
- **`aisland jenkins scaffold`** writes a project's `Jenkinsfile`, `Jenkinsfile.local` and GitHub `ci.yml`.
- **`aisland doctor`** checks the environment and prints the command that fixes what it finds.
- **`aisland wheels build <project>`** builds the wheels a project needs that are not on PyPI, for a machine
  without the workspace.
- **`aisland tests report <project>`** reads every recorded test run, and **`aisland clean <project>`** moves a
  project's ignored files into a backup **`aisland backups`** can restore.
- **`aisland completion install`** writes shell completion for every command-line tool in the workspace.
- **`aisland-mcp`** (the `[mcp]` extra) offers a chosen set of these commands to an AI client.
