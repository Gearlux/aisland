# `--no-check` builds the wheels and checks nothing, and says so (2026-10-20)

## Context

Every build ran both checks: each project the target takes from PyPI downloaded and compared with the
wheel its checkout builds, then the target installed in an empty venv and every module imported. They
are the slow part of a run and need the network, and a rebuild whose wheels the person does not need
checked (the same commits again, a zip for a machine where they will be tried anyway) paid for both.

## Decision

`aisland wheels build … --no-check` (chosen 2026-10-20) skips both checks and nothing else:

- the closure, the PyPI lookup (it still decides what is built), every refusal, the wheels, `wheels.yaml`,
  `install.py`, the README, `.gitignore` and `--zip` are those of a checked run — the generated files byte
  for byte;
- each project taken from PyPI is listed with its version and the note `not compared (--no-check)`; no
  PyPI wheel is downloaded and no checkout wheel built for it; the target's wheel is not built;
- the report's `checked` is false, the text says "Not checked (--no-check)" where the install section was,
  and the exit status is 0 — no check failed because none ran;
- `--no-check --commit` is refused before anything is built: the folder is committed only once the
  install check passed.

Rejected: skipping only the empty-venv check. The comparison is the other half of the cost (a download
and a build per published project), and a person who wants no check wants neither.

## Consequences

- An unchecked folder may not install on the other machine; the report, the JSON's `checked` and the
  refusal of `--commit` are what keep that visible.

## Example

```python
from aisland import wheels
from aisland.workspace.projects import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "planner", scope="all", make_zip=True, check=False)
report.checked, report.ok, report.imported
# (False, True, [])
```

## What you may change

- The wording of the report's two lines, together with the tests that read them.
- What must not change: `--no-check` changes no file the run writes; it never goes with `--commit`.

---

[The architecture and every decision](../architecture.md#the-decisions)
