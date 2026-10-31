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
- **`workspace.toml` carries its `format`** (1): a file a newer aisland wrote is refused with the upgrade to make, and a
  key no part of aisland reads (a typo such as `sorce`) is named with its line and the key it is closest to.
- **`aisland workspace configure`** chooses, per project, where it comes from (`source`: a checkout, GitHub or
  PyPI), whether this machine has it (`active`) and where it is mirrored (`mirror`), and per workspace the extras
  the projects offer.
- **`aisland services`** runs the local services under process-compose or Docker — Jenkins, the container engine,
  SwiftBar — and a service a project declares in its own `pyproject.toml` or implements in its own
  code; `aisland --help` names the project that brings each without importing its code. One service that cannot
  start never keeps the others down: its error is printed and the command exits 1. On Linux the container engine is
  the system's own Docker Engine, which aisland checks and never manages. `aisland supervisor status|attach|down` shows, attaches
  to and stops the process-compose supervisor behind them, and `aisland swiftbar` puts the workspace's Jenkins
  builds and GitHub Actions runs in the macOS menu bar.
- **`aisland jenkins scaffold`** writes a project's CI files from a template set — aisland's own (`Jenkinsfile`,
  `Jenkinsfile.local` and GitHub's `ci.yml`), or one the workspace names in `workspace.toml`'s `[ci]`: a folder of
  Jinja2 templates with variables of its own, or a set an installed package registers; `aisland jenkins templates
  export` copies aisland's set to start from. A folder of a project whose `package.json` has a `test` script gets a
  Node Tests stage in all three files — `npm ci` and `npm test` on the push pipelines, `npm test` on the checkout's own
  packages in the local one;
  `aisland jenkins init [--local]` seeds the Jenkins jobs for them, and `aisland tests nightly` reports the local
  Jenkins' nightly runs.
- **`aisland check`** runs the quality gates (`isort`, `black`, `flake8`, `mypy`, `pytest`, `act`) on every project
  or on the ones named.
- **`aisland --version`** says which aisland runs, inside a workspace or not. Every error is one `Error:` line and
  exit 1; **`aisland --debug <command>`** shows its traceback too.
- **`aisland doctor`** checks the environment and prints the command that fixes what it finds.
- **The workspace's `.env`** is read the way the shell reads it, by the services and Jenkins alike.
- **`aisland workspace env-example`** writes the workspace's `env.complete` (every environment variable the
  projects read) and `env.example` (the ones a person normally sets) from each project's own two files, every line
  commented out, a variable several projects read listed once under the project that defines it; a new machine
  copies `env.example` to `.env`, and `--missing` prints the variables an existing `.env` does not mention.
  `aisland setup` writes both, `workspace init` starts `.env` from them. `aisland workspace env-test` writes each
  project's test that fails when its code reads a variable its `env.complete` does not list.
- **`aisland wheels build <project>`** builds the wheels a project needs that are not on PyPI, for a machine
  without the workspace. `--bundle` merges the project and those wheels into one wheel plain `pip` installs,
  `--include-project` builds the project's own wheel too, and `--extra NAME` writes the extra NAME into the project's
  `pyproject.toml`, so `uv pip install -e ./<project>[NAME]` installs the project with every wheel. It refuses,
  before it builds anything, a project whose setuptools discovery would take the `wheels/` folder it writes for a
  package, and names the `[tool.setuptools.packages.find]` table that fixes it.
- **`aisland wheels zip <project>`** packs a project's wheels into a zip that installs with plain `python`, and
  **`aisland wheels install`** unpacks one into a project's `wheels/` and installs it into the environment you name.
- **`aisland wheels prune <project>`** strips from a project's history the committed wheel versions no branch or tag
  holds and force-pushes origin; **`aisland wheels reset <project>`** brings every other clone onto the pruned
  history (the `prune` extra), whatever its state: uncommitted changes are set aside and put back, a config value
  over several lines is read, and the workspace root's pointer moves to the rewritten commit.
- **A pytest plugin, `aisland_test_stats`,** loaded by every `pytest` in the environment aisland is installed in,
  records each run of a project of an aisland workspace — its seconds and each test's outcome — in the workspace's
  `.aisland/data/test-stats/`, and writes nothing beside a repository of its own; `aisland workspace test-stats off [PROJECT…] [--machine-only]` turns it off for a
  workspace, a project or this machine, and `pytest -p no:aisland_test_stats` leaves one run out.
- **`aisland workspace show`** lists what the projects declare to aisland — selections, services and code plugins.
  **`aisland workspace provision`** lists the system tools this machine needs, who needs each and whether it is
  installed; `provision install` installs them.
- **`aisland workspace track|untrack [PROJECT…]`** chooses whether the root repository records a project's commits;
  **`aisland workspace code-workspace`** and **`aisland workspace bashrc`** run alone the two setup steps that write
  the VS Code workspace file and link `project.bashrc`.
- **`aisland workspace theme|color`** sets this machine's VS Code theme and colours for the workspace window, kept
  in `.git/config` and never committed.
- **`aisland tests report <project>`** reads every recorded test run, and **`aisland clean <project>`** moves a
  project's ignored files into a backup **`aisland backups`** can restore.
- **`aisland completion install`** writes shell completion for every command-line tool in the workspace; a project
  names its click scripts in `[tool.aisland] click-commands`.
- **`aisland tunnel`** opens a VS Code Remote Tunnel to a remote machine, and `aisland tunnel connect` (the
  `[connect]` extra) a terminal on it over that tunnel.
- **`aisland mcp claude|gemini`** writes the client file (`.mcp.json`, `.gemini/settings.json`) that lists the
  workspace's MCP servers, and **`aisland notebooks clean`** is a git filter that clears notebook outputs before they
  are committed.
- **`aisland-mcp`** (the `[mcp]` extra) offers a chosen set of these commands to an AI client; its service tools
  start, stop and restart any service of the workspace, and its tunnel tools read, start and stop the remote tunnel
  to this machine.
