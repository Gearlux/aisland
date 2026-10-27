# Test-run statistics

How aisland records every pytest run of a workspace project and reports from the record which tests belong in the
fast run and which in the slow one.

## The pytest plugin

Installing aisland registers a pytest plugin, `aisland_test_stats`. pytest loads it in **every** run in the
environment aisland is installed in — the workspace's `.venv`, but also any other venv you `pip install aisland`
into. It does not change a run: no test is added, skipped or reordered, and the exit status is pytest's own.

### What it writes, and where

A run is recorded when the folder pytest runs the project from (the one holding its `pyproject.toml`) sits directly
in a workspace. Installed from PyPI, the workspace is the folder `AISLAND_WORKSPACE` names; without it, a project's
workspace is the folder above the project. So every run of a project is recorded, into
`<the folder above the project>/.aisland/data/test-stats/<project>/<date>.jsonl`:

| The project | Its runs are recorded in |
|---|---|
| `~/ws/app`, in a workspace `aisland workspace init` made | `~/ws/.aisland/data/test-stats/app/` — the workspace's `.gitignore` ignores `.aisland/` |
| `~/code/app`, a repository of its own | `~/code/.aisland/data/test-stats/app/` — outside the repository |
| `~/mono/pkg`, a folder of a repository `~/mono` | `~/mono/.aisland/data/test-stats/pkg/` — an untracked folder in `~/mono` until its `.gitignore` names `.aisland/` |

Run from an aisland checkout (developing aisland itself), the folder is `aisland/data/test-stats/` beside the
package instead.

Each file holds one JSON line for the run and one per test:

```json
{"kind": "run", "run": "20261027T093800-56267", "at": "2026-10-27T09:38:00+01:00", "commit": "", "leg": "fast", "whole": true, "seconds": 0.053, "exit": 0, "tests": 1}
{"kind": "test", "run": "20261027T093800-56267", "test": "tests/test_a.py::test_ok", "seconds": 0.0, "outcome": "passed", "slow": false}
```

- `leg` — which run it was: `fast` (plain `pytest`), `slow` (`-m slow`), `all` (`-m ''`) or `other` (any other
  `-m`); `whole` is false when a file, `-k`, `-x`, `--lf` or `--deselect` ran only part of it.
- `commit` — the project's git commit, empty outside a repository.
- `outcome` — `passed`, `skipped`, `failed` or `error`; `slow` says whether the test is marked `slow`.

Nothing else is recorded: no test output, no source, no environment. Day files older than 90 days are removed when
a run is recorded. A run with `pytest-xdist` is recorded once, by its controller. A run with `JENKINS_URL` set (a
Jenkins build) is not recorded: Jenkins keeps its own results, which `aisland tests report` reads. When the file
cannot be written, pytest prints one line saying so and the run's result is unchanged.

### Switching it off

In a workspace, turn recording off with `aisland workspace test-stats`, the same way `aisland workspace untrack`
works: with no project named it covers every project, those added later included; without `--machine-only` it is
written for everyone in `workspace.toml` (commit it), with it only in this machine's `.git/config`, which wins.

```bash
aisland workspace test-stats off                       # every project: [workspace] test-stats = false
aisland workspace test-stats off models                # one project: [project."models"] test-stats = false
aisland workspace test-stats off --machine-only        # every project, on this machine only
aisland workspace test-stats on models                 # models is recorded again
```

```text
$ aisland workspace test-stats off models
==> test runs not recorded: models (in workspace.toml — commit it)
  - models: not recorded
  - tools: recorded
```

`aisland tests report models` then says so, with the command that turns it back on:

```text
Test runs of models are not recorded: `aisland workspace test-stats on models` records them again.
```

The plugin still loads; for a run it would record it reads the switch first, and writes nothing when it is off.
Outside a workspace, and for a single run, pytest's own switches work as well:

| For | Do |
|---|---|
| a workspace, a project, or this machine | `aisland workspace test-stats off [PROJECT…] [--machine-only]` |
| one run | `pytest -p no:aisland_test_stats` |
| every run in a shell or a CI job | `export PYTEST_ADDOPTS="-p no:aisland_test_stats"` |
| a project, for everyone | `addopts = "-p no:aisland_test_stats"` under `[tool.pytest.ini_options]` in its `pyproject.toml` |
| a venv that only needs aisland's commands | install aisland somewhere else, e.g. `pipx install aisland` |

To keep the records but out of git, add `.aisland/` to the repository's `.gitignore`.

## Test-run statistics (`aisland tests report`)

Every project has two test runs: plain `pytest` is the **fast run** (within 5 minutes — what
you run before and after a change), `pytest -m slow` the **slow run** (CI's), `pytest -m ''`
both. Which tests belong in which run is read from statistics, never guessed — so every run is
recorded:

- **Local runs.** The pytest plugin above records each run of a project: which run it was,
  whether it was whole, its seconds, exit status and commit, and each test's seconds and outcome.
- **CI.** The local Jenkins keeps every build's JUnit results; the report reads them beside the
  local runs.

```bash
aisland tests report aisland                  # the last 30 days
aisland tests report models --days 90
aisland tests report tools --json            # the same report as data
pytest -p no:aisland_test_stats               # one run left out of the record
```

```text
aisland - the last 30 days: 2 local runs, 3 Jenkins builds
Fast run: 1m 00s (median of 1 whole run; last 1m 00s) - budget 5m 00s, within

Slowest fast-run tests:
       1.8 s  0 of 4 failed  tests/test_doctor.py::TestReporting::test_every_registered_check_returns_a_result
       1.7 s  0 of 4 failed  tests/test_setup_git_aliases.py::test_rerun_is_idempotent
       ...

Tests that failed:
    2 of 4    last 2026-09-12  fast  tests/test_workspace_dependency_specs.py::test_the_real_workspace_has_no_unsatisfiable_internal_specs
```

A fast run over 5 minutes adds a list headed `Move to the slow run (never failed, slowest
first) to fit 5m 00s:` — the tests whose seconds cover the overrun; and a slow test that failed
is listed under `Slow tests that failed - move into the fast run:`. Moving one is an edit: add
or remove its `@pytest.mark.slow`.
The fast run's time counts only complete runs of the whole fast run. Why it is built this way:
[`architecture.md`](architecture.md) → "Every pytest run of a workspace project is
recorded".
