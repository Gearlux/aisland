# `--include-project`: the project's own wheel, installed in place of its folder (2026-10-21)

## Context

`aisland wheels build` shipped what a project NEEDS; the project itself was installed editable from its
folder, so the other machine needed a checkout of it. The maintainer asked for an option that builds the named
project's wheel too, and for its zip to install from any folder (option 2 of the choice put to them).

## Decision

- **`--include-project` builds the target with the same rules as every other wheel**: from its commit, a dirty
  one refused unless `--force`. Its own `wheels/` folder is left out of that check — the folder is what this
  command writes, and on a first run it is untracked.
- **The wheel is kept out of `WheelsReport.built`** (`project_wheel` instead), so "needs N projects", the
  tables and the JSON of what it needs mean what they meant; `wheels.yaml` lists it last.
- **`install.py` knows its project by `wheels.yaml`'s own `project:`.** That entry is never a dependency:
  `--all`, `--needed` and `--only` choose among the others, and while its `install:` is true it replaces
  `-e ..`; false gives the editable folder back, `--no-project` drops both. No new field in the file.
- **The check installs and imports that very wheel** — no second build of the target in the scratch folder.
- **The zip's `__main__.py` carries `STANDALONE`**, filled from the folder (`installs_own_wheel`: the own entry
  marked to install, and its file there). A standalone zip runs in a folder with no `pyproject.toml` too; it
  still refuses another project's `pyproject.toml`, and a `wheels/` whose `*.whl` belong to someone else (the
  unpacked `wheels.yaml` names another project, or there is none) — it would delete them.

Rejected:
- **A `needed`/`target` field in `wheels.yaml`**: the file already names its project, and an older
  `install.py` would choke on the field.
- **Unpacking a standalone zip into `./<project>-wheels/`**: the maintainer chose `./wheels`, as everywhere else.

## Consequences

- An old zip (no `STANDALONE` line) keeps working: the template's default is the old behaviour.
- A user who sets the own wheel's `install:` to false must run the zip from the project's folder again; pip
  names the missing project if they do not.

## Example

```bash
aisland wheels build app --include-project --zip
# … app's own wheel (--include-project), installed in place of its folder:
# … Zip: app/wheels/app-wheels.zip (…) — install it from any folder with: python app-wheels.zip
mkdir /tmp/anywhere && cd /tmp/anywhere && python ~/Downloads/app-wheels.zip --dry-run   # the other machine
```

```python
from aisland import wheels
from aisland.workspace.projects import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "app", include_project=True, make_zip=True)
report.project_wheel, report.zip_standalone
```

## What you may change

- The report's wording and its section of `docs/wheels.md`, with `tests/test_wheels.py`.
- What must not change: without the flag every generated file is byte-identical; the own wheel is never in
  `built`; a standalone zip never unpacks over another project's folder or wheels.

---

[The architecture and every decision](../architecture.md#the-decisions)
