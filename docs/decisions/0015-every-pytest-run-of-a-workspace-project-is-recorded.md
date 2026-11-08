# Every pytest run of a workspace project is recorded, and the split is read from the record

*2026-10-05*

## Context

The workspace splits each project's tests into a fast run (plain `pytest`, within five minutes)
and a slow run (`pytest -m slow`, CI's), and keeps in the fast run only the integration tests
that break (decided 2026-10-05, aisland rules → "Two Test Runs"). Both halves of that rule
are measurements — how long a test takes, how often it fails — so the rule needs a record. The
local Jenkins already keeps one (each build's `junitResult.xml`), but it measured the wrong
thing: code reaches CI only after it passes on the developer's machine, and of the 351 builds it
held on 2026-10-05, 11 had a failing test. The breaks that decide what belongs in the fast run
happen in the runs nobody kept: the ones agents and developers make while they work.

## Decision

`aisland/pytest_stats.py` is both halves of the record.

- **A pytest plugin, installed by entry point** (`pytest11` → `aisland_test_stats`), so every run
  in the workspace `.venv` loads it with no per-project wiring. It records a run only when
  pytest's rootdir is `<workspace>/<project>` (a `.gitmodules` in the parent) — a test's own tmp
  project driven by `pytester` is not a run of the project — and never inside a Jenkins build
  (`JENKINS_URL` set): the local Jenkins builds in the project folders themselves
  (`customWorkspace`) with the workspace `.venv`, so the plugin loads there too, but Jenkins
  keeps its own JUnit record and the report reads that one. Each run appends ONE write to
  `aisland/data/test-stats/<project>/<date>.jsonl`: a line for the run (which leg — `fast`,
  `slow`, `all`, `other` — from the `-m` expression; whether it was the whole leg; wall seconds;
  exit status; commit) and a line per test (seconds over setup, call and teardown; the worst
  phase's outcome; its `slow` mark). Day files past 90 days are removed as a run is written.
- **The plugin never costs a run its result.** It imports only the standard library (it loads in
  every pytest start, so not `aisland.setup.manager` with click and yaml; it resolves the data folder
  the same way, pinned by a test), and a record it cannot write is one printed line.
- **The report, `aisland tests report <project>`,** merges the local lines with the Jenkins
  builds' JUnit records — a nodeid lands on the same test as CI's `classname::name` through
  `junit_address`, the mapping pytest's own JUnit writer uses — and answers the rule's
  questions: the median of the complete whole fast runs against the 300-second budget, the
  slowest fast tests, the tests that failed, the never-failed fast tests that would cover an
  overrun (slowest first), and the slow tests that failed. `--json` prints the same
  `StatsReport` as data.
- **The report proposes, it does not move.** A test changes leg by an edit to its
  `@pytest.mark.slow` (or a reference file to a `FAST_SAMPLE`) that a person reviews; the plugin
  never marks a test at collection time from the record. Rejected: deselecting tests dynamically
  from their history — the leg a test runs in would change between two runs of the same commit,
  and nobody could read from the code which tests the fast run holds.
- **Rejected: CI only.** It would leave the "breaks" column almost empty (above). **Rejected: a
  committed statistics file** — every test run in every session would dirty the tree.

## Consequences

- A run in a workspace project costs one `git rev-parse` and one file append at its end.
- The records are per machine and git-ignored (`data/`): the report on one machine knows that
  machine's runs plus its Jenkins.
- A partial run (a file, `-k`, `-x`, `--lf`, `--sw`, `--deselect`) still records its tests' times
  and outcomes, but its wall time never counts as the fast run's.
- An xdist worker records nothing; its controller records the run.
- The CI half fills because the local orchestrator builds every project at 01:00 each night
  (`NIGHTLY_CRON`, update 2026-10-06): development and `/ship` run only the fast run, so without
  a schedule nothing ran the slow runs at all. The timer sits in the job's properties (armed when
  the job is seeded) and in the pipeline's `triggers` (re-applied at each run). A machine asleep
  at 01:00 misses that night. Only that run asks for the slow tests: a manual build is the fast
  run (decided 2026-10-06). The project jobs learn which run they belong to from a
  `SLOW_TESTS` parameter the orchestrator passes — true when its build cause is the timer —
  because a downstream job cannot see its upstream's cause; rejected: a second, nightly-only
  orchestrator job (two lists of projects to keep in step).
- A night's failure is reported by Jenkins, and the tests are moved by an AI agent (decided
  2026-10-06). `aisland tests nightly` (`aisland/services/nightly.py`), run by the
  `<workspace>.nightly-check` job at 09:30 on weekdays, writes `aisland/data/nightly/<date>.md`,
  notifies and turns red when a night failed or had no run — it runs whether or not the agent's
  app is open, and its result shows in Jenkins beside the night it judges. Moving a test between
  the runs needs judgment (a timeout under load is no caught break), so an agent's
  scheduled task does that from the report. Rejected: the agent's task alone (it runs only
  while the app is open, and Jenkins showed nothing); Jenkins alone (no judgment for the moves).
- One run is disabled with `-p no:aisland_test_stats`.

## Example

```console
$ aisland tests report canvas
canvas - the last 30 days: 4 local runs, 3 Jenkins builds
Fast run: 6m 10s (median of 2 whole runs; last 6m 02s) - budget 5m 00s, 1m 10s over

Move to the slow run (never failed, slowest first) to fit 5m 00s:
      40.2 s  tests/test_app.py::TestPaletteAndForm::test_the_form_gives_every_node_its_parameter_schema
      36.8 s  tests/test_app.py::TestPaletteAndForm::test_the_palette_lists_at_least_the_datasets_own_classes
```

```python
# What an MCP tool wrapping the report returns: the same object as data.
from aisland.pytest_stats import build_report

build_report("canvas", days=30).to_dict()
# {"project": "canvas", "fast_run_seconds": 370.0, "to_slow": [{"test": "...", ...}], ...}
```

## What you may change

- The budget (`FAST_RUN_BUDGET_SECONDS`), the retention and the default window — the budget only
  with the user, since it is their ruling.
- What the report lists and how it words it (`render_report`), with its tests in
  `tests/test_pytest_stats.py`.
- What must not change: the plugin records only workspace projects' own runs, never fails a run,
  and never moves a test between legs by itself; the leg of a test is the code's
  `@pytest.mark.slow`.

---

[The architecture and every decision](../architecture.md#the-decisions)
