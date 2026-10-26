# Test-run statistics

How aisland records every pytest run of a workspace project and reports from the record which tests belong in the
fast run and which in the slow one.

## Test-run statistics (`aisland tests report`)

Every project has two test runs: plain `pytest` is the **fast run** (within 5 minutes — what
you run before and after a change), `pytest -m slow` the **slow run** (CI's), `pytest -m ''`
both. Which tests belong in which run is read from statistics, never guessed — so every run is
recorded:

- **Local runs.** aisland installs a pytest plugin (`aisland_test_stats`, loaded by every
  `pytest` in the workspace `.venv`). Each run of a workspace project appends to
  `aisland/data/test-stats/<project>/<date>.jsonl` (`.aisland/data/test-stats/…` in the workspace when aisland is installed from PyPI; git-ignored; kept 90 days): one line for the
  run — which run it was (`fast`, `slow`, `all`, `other`), whether it was the whole run or a
  file / `-k` / `-x` part of it, its seconds, exit status and commit — and one line per test —
  its seconds, `passed` / `failed` / `error` / `skipped`, and whether it is marked `slow`.
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
