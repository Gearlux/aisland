# The test-run record can be turned off per workspace, project or machine

*2026-10-27* [test-stats-switch]

## Context

The pytest plugin records every run of a workspace project, and that record is what `aisland tests report` judges the
fast run by. Installed from PyPI it loads in every venv aisland is in, so a workspace that does not want the record
had only pytest's own switches (`-p no:aisland_test_stats`, `PYTEST_ADDOPTS`, an `addopts` line) — each set by hand,
none visible to aisland or its report.

## Decision

`aisland workspace test-stats on|off [PROJECT…] [--machine-only]`, shaped like `track set|unset` (the user's choice over
a per-project `configure` plugin): no project named covers the whole workspace, those added later included; the
committed layer is `workspace.toml` (`[workspace] test-stats = false`, a project's own value only where it differs),
this machine's is `.git/config` (`aisland.test-stats.default`, `aisland.test-stats.<project>.record`) and wins. The
plugin still loads; for a run it would record it reads the switch with the standard library — `tomllib` and one
`git config --get-regexp` — and a file it cannot read means recorded, so the switch never costs a run.

## Consequences

- A recorded run costs one more `git` call (a few milliseconds); a run the plugin would not record costs nothing.
- `aisland tests report` and its JSON say when a project is off (`recording`, `recorded_by`) and name the command
  that turns it on — `--machine-only` when this machine turned it off.

## Example

```text
$ aisland workspace test-stats off models
==> test runs not recorded: models (in workspace.toml — commit it)
  - models: not recorded
  - tools: recorded
```

## What you may change

- A listing (`aisland workspace test-stats` alone printing each project's state) — not built.
- Never: removing the plugin's record to turn it off, or a switch the plugin needs more than the standard library to read.

---

[The architecture and every decision](../architecture.md#the-decisions)
