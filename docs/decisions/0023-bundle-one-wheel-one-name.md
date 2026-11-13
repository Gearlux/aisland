# `--bundle`: one wheel, one name (2026-10-23)

## Context

The wheels folder installs through `install.py` or the zip, both run with `python`. The maintainer asked for the
wheels packaged into ONE wheel, to install with plain `pip install <file>.whl`, and accepted what that costs
(option 1 of the choice put to them): pip knows the bundle's name only.

## Decision

- **One distribution, `<project>-bundle`.** A wheel carries exactly one `.dist-info`; pip refuses more
  (`multiple .dist-info directories found`, `pip/_internal/utils/wheel.py`), and a wheel inside a wheel is
  not installed. So the bundle merges the files: each wheel's members as they are, its `.data` folder renamed
  to the bundle's, the entry points merged, the top-level names joined, the licence files kept under
  `licenses/<distribution>/`. Nothing else of a dist-info comes over.
- **Beside the separate wheels, never instead of them**, in `wheels/bundle/`: a subfolder, so the zip (the
  folder's files) and the run's `*.whl` cleanup never take it for a separate wheel. Every other generated
  file is byte-identical with and without the flag; a bundle an earlier run wrote is removed by every run.
- **What it holds**: the project's own wheel and the separate wheels it needs (`BuiltWheel.needed`) — the
  scope decides, as for the folder. The project is held to its own wheel's rules (`_refuse_uncommitted` with
  the target), as under `--include-project`.
- **What it requires is read from each `pyproject.toml`**, never from the built METADATA, where an extra is
  only an `extra == "…"` clause inside the environment markers. A requirement on a bundled project is
  replaced by that project's base requirements and those of the extras it names, under its marker; every
  other requirement is kept as written; the project's extras stay the bundle's, without what the base
  already requires. A requirement the bundled version does not satisfy is refused, so a `--no-check` run
  cannot ship one. A project whose `dependencies` are `dynamic` is refused: pyproject does not hold them.
- **Refused, never resolved by a rule**: a wheel that is not pure Python (the bundle is `py3-none-any`), one
  path holding different bytes in two wheels, one entry point name pointing at two objects.
- **Checked alone**: `uv pip install <bundle>` in an empty venv of its own, then every module of the same
  packages imported; its failures are breaks (exit 1, no `--commit`). The empty-venv check of the folder
  runs as before.
- **Reproducible**: members in name order, one fixed time — an unchanged workspace rebuilds the same file.
  METADATA's description carries the generator header and each project's commit.

Rejected:
- **Several `.dist-info` folders in one wheel**: pip refuses it.
- **The bundle under the project's own name**: it would clash with `--include-project`'s wheel in the same
  folder, and pip would show a version that hides what is inside.
- **Requirements from METADATA**: splitting `extra == "…"` out of an arbitrary marker is parsing a language.

## Consequences

- On the other machine `pip show <dependency>` finds nothing, a package's `__version__` read from its
  distribution falls back to `0.0.0.dev0`, a later install of something requiring a bundled project puts
  PyPI's copy over the bundle's files, and uninstalling is all or nothing (the table in `docs/wheels.md`).
- Plugins found by entry point group keep working: the groups are merged into the bundle's.

## Example

```bash
aisland wheels build planner --bundle
# … Bundle: planner/wheels/bundle/planner_bundle-<version>-py3-none-any.whl (…) — … in one wheel …
pip install planner_bundle-<version>-py3-none-any.whl   # the other machine
```

```python
from aisland import wheels
from aisland.workspace.projects import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "planner", bundle=True)
report.bundle.wheel, report.bundle.projects, report.bundle_import_failures
```

## What you may change

- The report's wording, its section of `docs/wheels.md` and the bundle's description, with `tests/test_wheels_bundle.py`.
- What must not change: one `.dist-info`; the separate wheels and every other file unchanged by the flag; a
  conflict refused, never resolved silently; the bundle's requirements read from `pyproject.toml`.

---

[The architecture and every decision](../architecture.md#the-decisions)
