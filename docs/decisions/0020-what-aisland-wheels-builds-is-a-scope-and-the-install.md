# What `aisland wheels` builds is a scope, and the install is one generic script naming each wheel by its file

*2026-10-09*

## Context

Shipping only what PyPI lacks leaves the other machine at the mercy of the PyPI releases: on
2026-10-08 a project's modules could not import because datasets 0.1.0a2 on PyPI lacked
`datasets.draws`. The maintainer asked for an option that ships the published dependencies as
wheels too, and one that ships every workspace project. A wheel built from a checkout ahead of
its release carries the SAME version as the release, and the old install line
(`pip install --find-links wheels -e .`) then took PyPI's: measured 2026-10-09 in an empty venv,
`pip install --find-links wheels datasets` installed PyPI's 0.1.0a2 (`datasets.draws`
missing), while `uv pip install --find-links wheels datasets` installed the folder's. Named
by its file (`pip install --find-links wheels wheels/datasets-0.1.0a2-py3-none-any.whl`), pip
installed the folder's.

## Decision

- **A run has a scope** (`WheelsScope`, chosen 2026-10-09): `unpublished` (the default, the
  previous record), `needed` (`--include-published`: every project the target needs that has a
  checkout here) and `all` (`--all`: every workspace project that has one). A project the scope
  covers without a checkout is not built and is said so; a needed one comes from PyPI (user
  choice: report and go on, rather than stop). A dirty project is refused in every scope,
  including one `--all` builds although the target does not need it (chosen).
- **`wheels.yaml` lists the wheels; its `install:` values are the user's.** Each entry carries the
  wheel, the commit, `needed`, the version PyPI has and `install:`, which defaults to `needed`. A
  rerun reads the file it replaces and keeps every `install:` the user set (chosen); a file
  it cannot read stops the run at its line before anything is built, because rewriting it would
  drop the choices.
- **`install.py` is one generic script** (`aisland/wheels/install.py`, copied under a generated
  header): it reads `wheels.yaml`, chooses (`install: true` by default, `--all`, `--needed`,
  `--only`) and runs ONE `pip install --find-links wheels -e <project> <each chosen wheel's file>`
  in the interpreter running it — the file names are what make the folder's wheels win. It
  installs PyYAML first when it is missing, and passes everything after `--` to pip or uv.
- **The empty-venv check runs that script** (`--installer uv --needed`), so what is checked is what
  the other machine runs, and the target's private index (`uv.toml`) still reaches the install.

Rejected:
- **`pip install wheels/*.whl`.** Under `--all` it would install every workspace project with its
  dependencies (detector and trainer pull torch and Lightning).
- **A local version label per build**, again (previous record): naming the files solves the
  same-version choice without a second version in the files.

## Consequences

- The check installs through uv while the user's default is pip. The two choose differently only
  between a found and an indexed wheel of one version, and the script names files, which both
  take: `tests/test_wheels_install.py` pins pip's side with a real offline install against a local
  PEP 503 index (the index wins over `--find-links`; the named file wins over the index).
- The wheels folder of a `--include-published` run holds the workspace's commit of every needed
  project; the project then no longer depends on the PyPI releases being current.
- The user's pip configuration reaches every pip run: an `extra-index-url` in `~/.pip/pip.conf`,
  which `--index-url` does not replace, answered a test's `libx` with another project's package
  needing pandas (checked with `pip config debug`, 2026-10-09). The pip tests run with
  `PIP_CONFIG_FILE=/dev/null` and a package name nobody publishes.

## Example

```bash
aisland wheels planner --include-published
python wheels/install.py --dry-run      # on the other machine, from planner/
# $ python -m pip install --find-links …/wheels -e …/planner …/wheels/corelib-0.4.1-py3-none-any.whl …
```

```python
from aisland import wheels
from aisland.workspace.projects import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "planner", scope="needed")
[(b.project, b.on_pypi) for b in report.built][:2]   # [('corelib', '0.4.1'), ('liquifai', '0.3.1')]
report.not_built                                      # []: a needed project without a checkout here would be listed
```

## What you may change

- The script's flags and the YAML's fields, together with `aisland/wheels/install.py`'s
  `read_wheels` (the one reader both sides use) and both test files.
- What must not change: the install names each chosen wheel by its file; the check runs the
  shipped script; a rerun keeps the user's `install:` values; a dirty project is refused in
  every scope unless the run is told `--force` (record "`--force` builds a dirty project from its
  working tree").

---

[The architecture and every decision](../architecture.md#the-decisions)
