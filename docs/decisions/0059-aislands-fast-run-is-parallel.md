# aisland's fast run is parallel (2026-10-28)

## Context

The fast run took 13 minutes on a loaded machine (8 min 22 s the 30-day median) against the workspace's 5-minute
budget. The report's rule — move the slowest tests that never failed — could not reach it: 775 of 2105 tests had
failed at some point in 30 days (most of them in development runs, a test written before its code), and they took
539 of the run's 787 seconds; even counting only whole-run failures, moving all 1849 others left 332 seconds and a
fast run of 256 tests.

## Decision

Run it over four worker processes (`pytest-xdist`, `-n 4` in `addopts`, the plugin in `dev`). Most of a test's time
is waiting on git, uv and builds in subprocesses, which spreads across cores. Measured on the same busy machine: 790 s
serial, 490 s over six workers, all 2105 tests passing, the test-stats plugin recording one whole run. Four is what a
GitHub runner has.

## Consequences

A single test file starts four workers (`-n 0` for one process). A test that shares state across processes would now
fail; none did.

## Example

```toml
[tool.pytest.ini_options]
addopts = "-m 'not slow' -n 4"
```

## What you may change

- The worker count, measured on an idle machine and on a runner.
- Never: deselecting by history, or a second setting for CI.

---

[The architecture and every decision](../architecture.md#the-decisions)
