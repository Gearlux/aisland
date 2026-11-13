# The wheels folder is the machine's: ignored unless `--commit`, carried as one self-installing zip (2026-10-18)

## Context

A `--all` build of one project is 26 wheels and 5.1 MB, rebuilt whenever a dependency moves. The folder sat
in the project's tree with nothing ignoring it, so every `/commit-all` swept the wheels into history, and the
user wanted them out unless asked for. The wheels also have to reach a machine that has neither the
workspace nor aisland (`aisland` was not on PyPI then), so carrying them must not need aisland there.

## Decision

Chosen 2026-10-18: the opt-in flag commits by itself; both command shapes; a project's
already-tracked wheels untracked.

- **Every run writes `wheels/.gitignore` holding `*`.** The folder ignores itself, the ignore file
  included, so git never sees it. A run that finds files of the folder still tracked prints the
  `git rm -r --cached` that stops tracking them; it never runs it.
- **`--commit` writes `*.zip` instead and commits `wheels/` in the target itself** — only when the
  empty-venv check passed, and only that path (`git commit -- wheels`): another session's staged or
  unstaged work in the project stays out. The message names the build command. The zip is never
  committed.
- **`aisland wheels zip` packs the folder with a generated `__main__.py`** (`aisland/wheels/unzip.py`,
  standard library only), so `python <project>-wheels.zip` unpacks into `./wheels` and runs its
  `install.py` with the rest of the line. It refuses a folder whose `pyproject.toml` names another
  project before unpacking anything. Members are written in name order with one fixed time, so a zip of
  an unchanged folder is the same file.
- **`aisland wheels install ZIP --python PYTHON`** runs that zip in another interpreter; the workspace's
  `.venv` is refused (install.py installs with pip, and the workspace installs through `aisland setup`).
- **`aisland wheels` is a group** (`build`, `zip`, `install`); a first word that names no command is the
  project to build, and `--install ZIP` on the group is the short form of `install`.

Rejected:
- **A flag that only leaves the folder visible to git** (`--track`): the maintainer wanted the command to
  commit.
- **Installing from the zip through aisland only**: the machine that needs the zip has no aisland.

## Consequences

- A project whose wheels were committed before keeps them in history; untracking stops new ones only.
- `--commit` creates a commit without `/commit-all`; it is the one aisland command that commits in a
  project.

## Example

```bash
aisland wheels build planner --all --zip
# … Zip: planner/wheels/planner-wheels.zip (…) — install it from the planner folder with: python planner-wheels.zip
cd ~/src/planner && python ~/Downloads/planner-wheels.zip --dry-run   # the other machine
```

```python
from aisland import wheels
from aisland.workspace.projects import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "planner", commit=True, make_zip=True)
report.committed, report.commit_note, report.zip_file
```

## What you may change

- The zip's name, its `__main__.py` options and the report's wording, with `tests/test_wheels_zip.py`.
- What must not change: the folder is ignored unless `--commit`; `--commit` commits only `wheels/`, only
  after a passing check, never the zip; the zip installs without aisland and refuses another project's
  folder; `install` refuses the workspace's `.venv`.

---

[The architecture and every decision](../architecture.md#the-decisions)
