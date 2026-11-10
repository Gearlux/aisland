# Contributing to aisland

How to change aisland itself: get a checkout running, find where a change goes, and keep the gates green.
How the package is built is [docs/architecture.md](docs/architecture.md); why each mechanism is the way it is lives in
the decision records under [docs/decisions/](docs/decisions/), which it indexes. This page is the short way in.

## A checkout that runs the tests

There are two ways to work on aisland, and they differ only in where the checkout sits.

**A clone of its own** is enough to change aisland and run its tests:

```bash
git clone https://github.com/Gearlux/aisland.git && cd aisland
uv venv --seed .venv && uv pip install --python .venv/bin/python -e '.[dev]'
.venv/bin/pytest            # the fast run: under five minutes, four workers
.venv/bin/pytest -m slow    # the slow run
```

A clone that no workspace lists works like an installed wheel: it finds no workspace of its own, and the tests run
against folders they make themselves. The `dev` extra holds everything the gates and both test runs need.

**A checkout inside a workspace** — the workspace lists it as a project in `.gitmodules` or `workspace.toml` — is
the workspace's aisland: every `aisland` command works on that workspace, wherever you run it, and its state lives
in the checkout. `setup.sh` bootstraps such a workspace from its root:

```bash
bash aisland/setup.sh      # the interpreter, the .venv, aisland editable, then `aisland setup -y`
source project.bashrc      # activates .venv and exports .env
```

The gates CI runs, each from the checkout:

```bash
.venv/bin/black --check . && .venv/bin/isort --check-only . && .venv/bin/flake8 .
.venv/bin/mypy .            # the package and the tests, held to what --strict checks (mypy.ini)
.venv/bin/lint-imports --no-cache   # the import layers below
```

## Where a change goes

The package is layered. Each layer imports only the layers below it, and import-linter holds that order
(`[tool.importlinter]` in `pyproject.toml`). From top to bottom:

| layer | what it is |
|---|---|
| `cli/`, `server.py` | the two front doors: the click commands and the MCP server. Both stay thin and call the layers below. |
| `doctor.py`, `clean.py` | commands over the whole workspace |
| `backups.py`, `services/` | the backup store; the services, the process-compose supervisor, Jenkins and its jobs |
| `ci/` | the generated CI files, the quality gates, the test-run report, the Jenkins job definitions |
| `wheels/` | `aisland wheels` |
| `setup/` | `aisland setup` and its steps, `workspace init`, the `configure` commands, the agent harness |
| `workspace/`, `tunnel/` | the workspace model (its file, projects, sources, selections); the remote tunnel |
| the other top-level modules | leaves every layer shares: `console`, `errors`, `gitcmd`, `paths`, `venv`, `atomic`, … |

If a lower layer needs something a higher one has, move that piece down to the lower layer, or move the caller up.
Never import inside a function to get round the order. A new top-level module goes either in a layer or in the
`leaves` contract.

## The house rules a review checks

- **Speak through `console`**, never `click.echo`, outside `cli/`. An MCP tool call collects what a command says, and
  a question asked with no one there to answer raises an error instead of guessing.
- **Raise `AislandError`** (or a subclass that also derives from the builtin it stands for). Never raise
  `click.ClickException` or `SystemExit` in library code. A failure the command goes on past is
  `failures.report(...)`, and the run still exits 1.
- **Run git through `gitcmd.git`**, downloads through `downloads.download`, and a status probe through `probe.run`.
  Each sets a time limit and never prompts.
- **Write a state file through `atomic.write_text`**, so it is written whole or not at all.
- **Type everything.** `Any` appears only with a `# Any: <why>` on its line, and nothing is silenced with
  `# type: ignore`.
- **Test behaviour, never source text.** Patch a name in the module that calls it. No test may reach the real
  workspace or its state: autouse fixtures in `tests/conftest.py` stop one that tries.

## Adding …

- **a command**: put it in its group's module under `cli/`, as `@<group>.command()`. Its work goes in the module of
  its subject. A command that prints data offers `--json`, from the same function the MCP tool would call.
- **a service**: add one `Entry` to `services/registry.py` and a `BaseService` subclass that implements `launch()`.
  A project's own service needs no aisland code at all ([services](docs/services.md)).
- **a doctor check**: add a `check_*` function to `doctor.py` and list it in `CHECKS`. It is cheap and read-only, and
  a failure prints the command that fixes it.
- **a `workspace.toml` key**: add it to `PROJECT_KEYS` or `WORKSPACE_KEYS` in `workspace/check.py`, in the same
  change. Otherwise every workspace using it is warned about an unknown key.
- **a dependency**: give it a floor you have run the suite on (`uv pip install --resolution lowest-direct`), and say
  why in a comment beside it in `pyproject.toml`.

## A release

Between releases the CHANGELOG's newest entry is the next release, `## [<version>] - Unreleased`, and
`pyproject.toml`'s `version` is that version. Changes are listed under it as they land. Then:

1. Merge `dev/main` into `main`. The release workflow refuses a tag whose commit is not on `main`.
2. On `main`, date the entry: `## [<version>] - YYYY-MM-DD`, the day of the commit you will tag. The workflow
   compares it with that commit's date, so a heading dated ahead of time, or left `- Unreleased`, is refused.
3. Tag that commit and push the tag: `git tag v<version> && git push origin v<version>`. This runs
   `.github/workflows/release.yml`: it runs the gates and both test runs, checks the tag against the version, the
   CHANGELOG's date and `main`, builds the package, checks it with `twine check --strict`, tests the wheel in a clean
   venv, and publishes it to PyPI.
4. Open the next entry, `## [<next version>] - Unreleased`, and set `pyproject.toml`'s `version` to it. The README's
   links name the release's tag (`blob/v<version>/…`), so they move with the version; `tests/test_docs.py` checks
   they match.

What a release must keep working for its users is listed in [docs/stability.md](docs/stability.md).
