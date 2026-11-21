# Changelog

All notable changes to aisland are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The newest entry is the
next release: it reads `- Unreleased` until the release dates it.

## [0.1.2] - Unreleased

### Added
- **`/detach`, `/export-session` and `/import-session`**, three agent commands of the harness. `/detach <n>` opens a
  proposed next action as a session of its own that waits for the go; `/export-session` writes a handoff of a session
  (a brief, the commits no remote has, its own uncommitted edits) and `/import-session` carries it on in a workspace
  on another machine ([harness](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/harness.md#agent-commands-and-rules-in-every-workspace-the-harness)).

### Fixed
- **A renamed project is installed under its new name alone.** `aisland setup` uninstalls what the old name left in
  `.venv` first, renamed in its folder or with it, and still writes the right version into `workspace.toml`'s how-to
  ([workspace](https://github.com/Gearlux/aisland/blob/v0.1.2/docs/workspace.md#a-renamed-project)).

## [0.1.1] - 2026-11-13

### Added
- **`aisland wheels prune --all`** strips every committed wheel version from every branch and tag, the ones a branch,
  a tag, HEAD or the root's commit holds too, so no tip keeps a wheel ([wheels](https://github.com/Gearlux/aisland/blob/v0.1.1/docs/wheels.md#old-committed-wheels-aisland-wheels-prune-and-aisland-wheels-reset)).

## [0.1.0] - 2026-11-13

The first release on PyPI: `pip install aisland`.

### Added
- **A workspace in two commands.** `aisland workspace init <folder>` makes one git repository whose projects are
  submodules, listed in `workspace.toml`. `aisland setup` installs every project editable into one `.venv`,
  dependencies first, along with the system tools the projects declare, the git aliases (`git pull-all`,
  `git push-all`), the editor's workspace file and `project.bashrc` ([workspace](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/workspace.md)).
- **Per-project and per-machine choices.** `aisland workspace configure` picks where each project comes from (a
  checkout, GitHub or PyPI), whether this machine has it, where it is mirrored, and which extras the projects
  install ([sources](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/sources.md), [selections](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/selections.md)).
- **Local services.** `aisland services` runs Jenkins, the container engine, SwiftBar and the services projects
  declare, under one process-compose supervisor. Jenkins is a pinned LTS release with checked downloads, and it
  listens on this machine only ([services](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/services.md), [jenkins](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/jenkins.md)).
- **Generated CI and the quality gates.** `aisland jenkins scaffold` writes a project's `Jenkinsfile`,
  `Jenkinsfile.local` and GitHub `ci.yml` from a template set, aisland's own or yours. `aisland check` runs isort,
  black, flake8, mypy, pytest and `act` the way CI does ([ci](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/ci.md)).
- **Wheels for a machine without the workspace.** `aisland wheels build|zip|install` carries a project and its
  unreleased dependencies elsewhere. `aisland wheels prune|reset` drops old committed wheels from a project's
  history, with a backup bundle (the `prune` extra; [wheels](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/wheels.md)).
- **The machine's environment.** `aisland doctor` checks it and prints the fix for each problem it finds.
  `aisland workspace env-example` collects every variable the projects read into `env.complete` and `env.example`
  ([doctor](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/doctor.md)).
- **Test-run statistics.** A pytest plugin records each run of a workspace project, and `aisland tests report`
  proposes which tests to move between the fast and slow runs ([tests](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/tests.md)).
- **AI coding agents.** `aisland setup` installs the agent rules and commands from the harness sets
  `workspace.toml` lists. `aisland mcp claude|gemini` writes the client files, and `aisland-mcp` (the `mcp` extra)
  offers a chosen set of aisland's commands to an AI client ([harness](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/harness.md), [mcp](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/mcp.md)).
- **Remote machines.** `aisland tunnel` opens a VS Code Remote Tunnel, and `aisland tunnel connect` (the `connect`
  extra) a terminal over it ([tunnel](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/tunnel.md)).
- **Predictable failures and versioned files.** Every error is one `Error:` line with exit code 1, and
  `aisland --debug` adds the traceback. `workspace.toml`, template sets and harness sets carry a `format`, so a file
  written for a newer aisland is refused with the upgrade to make ([stability](https://github.com/Gearlux/aisland/blob/v0.1.0/docs/stability.md)).

[0.1.2]: https://github.com/Gearlux/aisland/releases/tag/v0.1.2
[0.1.1]: https://github.com/Gearlux/aisland/releases/tag/v0.1.1
[0.1.0]: https://github.com/Gearlux/aisland/releases/tag/v0.1.0
