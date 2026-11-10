# A project installed outside the workspace gets its unpublished dependencies as wheels

*2026-10-08*

## Context

A project that is not on PyPI (planner, say) has to be installable on a machine that has
neither the workspace nor access to the workspace's GitHub repositories. Its dependencies are
workspace projects of two kinds: some are on PyPI (corelib, datasets), some are not (a
sibling not yet released). The second kind has to travel with the project. And because those
wheels are built from this checkout, they can use code that the PyPI releases of the first kind
do not hold yet — measured on 2026-10-08: a sibling's module imported `datasets.draws`, which
datasets 0.1.0a2 on PyPI lacks, so an install that resolves fine fails at `import`.

## Decision

`aisland wheels <project>` (`aisland/wheels/build.py`, chosen 2026-10-08: an aisland command
rather than a root script; only what the project needs; both checks below):

- **What it needs** is the closure of its `[project] dependencies` over the workspace's
  projects: base dependencies, plus an extra only when a requirement names it. Every marker is
  followed, because the target machine's platform is not this one's.
- **On PyPI** means a version, pre-releases included, whose metadata points at
  `github.com/<owner>/` (the owner of the project's own remote), with a file that is not yanked. This differs on purpose from
  `pypi.released_on_pypi` (final releases only): that one decides what CI installs, while this
  one asks what pip on another machine will find, and pip takes a pre-release when no final
  release exists.
- **Each wheel is built from `git archive HEAD`**, with `SOURCE_DATE_EPOCH` set to the commit's
  time. Building from the folder put a file that only lived in a stale `build/lib` into the
  wheel (measured), and without the timestamp an unchanged project rebuilt into a different
  file, so git saw every wheel change on every run (measured). A dirty project is refused,
  because its wheel would silently lack the change.
- **The PyPI half is checked twice.** First, each PyPI wheel is compared with the wheel its
  checkout builds, file by file plus `Requires-Dist`; a difference is reported as *behind*.
  Second, the project is installed editable in an empty venv from its wheels plus PyPI, and every
  module of the project and of its wheels is imported (`__main__` excepted: importing it runs the
  program). A failed import names the project that owns the missing module, which is the one to
  *release first*. Only the second
  check decides the exit status: being behind is normal between releases, while a failed import
  means the project cannot be used.

Rejected:
- **Direct `file:` references in the project's dependencies.** PEP 508 has no relative path, so
  they would carry this machine's absolute path into a committed file.
- **A local version label per build** (`0.1.0+<n>.g<sha>`) to make reinstalls automatic. It
  would put a second version into the files, against the rule that the version lives once, in
  `pyproject.toml`. A reinstall is one documented command instead.
- **Every workspace project by default.** Wheels the project never imports (aisland,
  planner, compiler) would be shipped and committed on every rebuild. `--all` builds them on
  request (next record).

## Consequences

- A run needs PyPI (an unreachable PyPI stops it before anything is built, because a guess would
  ship a wheel too many or one too few) and takes about 35 s with uv's cache warm.
- The import check imports every module, because a module no package imports is otherwise
  never checked (measured 2026-10-08: importing only the outermost packages of a project with
  unreleased dependencies found 2 modules missing `datasets.draws`; importing every module
  found 9). The optional extras' modules, whose
  dependencies the empty venv does not install, are kept from failing the run by one rule: a
  package that fails, or any module missing a workspace project's module, is a break (exit 1);
  a deeper module missing something no workspace project ships is listed under "Not imported"
  and breaks nothing. Only a `ModuleNotFoundError` is that: a `TypeError` from the liquifai
  release on PyPI (0.3.1: `LiquifyApp.command() got an unexpected keyword argument
  'config_files'`) was once filed there, though it is the outdated release's doing — with
  `--include-published` the module imported. Any other error of a deeper module is therefore a
  break once a published project is behind, blamed on one project when it can be: the
  traceback's innermost frame in a behind project's code, else the behind projects whose
  checkout-only code uses a name the message quotes (a bad keyword is raised at the call, so
  the callee has no frame), else all of them. The cost of that rule: a deeper module missing an undeclared third-party
  dependency is only listed, never a failure. A package that fails is reported alone, since the
  modules under it cannot be reached.
- A child project that ships inside its parent package (the `extend_path` shim) is imported by
  its dotted name. Its wheel is detected as a child either by the missing parent `__init__.py`
  or by a parent `__init__.py` that only extends `__path__`.
- The editable install in the empty venv writes the target's own `*.egg-info`, which is
  git-ignored like the workspace's own install of it.

## Example

```text
$ aisland wheels planner            # exit 0
...
Installed planner in an empty venv (its wheels + PyPI) and imported every module of its 1 package:
  17 modules imported

Not imported, missing what no workspace project ships (an optional extra's?):
  planner.server         ModuleNotFoundError: No module named 'vision'
  planner.visualisation  ModuleNotFoundError: No module named 'matplotlib'

Behind, nothing breaks: corelib, liquifai, datasets
```

```python
from aisland import wheels
from aisland.workspace.projects import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "planner")
report.release_first      # []: a module missing a workspace project's module would name it here
report.to_dict()["ok"]    # True: what an MCP tool would return
```

## What you may change

- The report's wording and layout, with `tests/test_wheels.py::TestReport`.
- How packages to import are found (`wheel_modules`) and how their modules are walked, keeping
  the rule that a module missing what no workspace project ships breaks nothing.
- What must not change: wheels are built from the commit, never from the folder; their
  timestamp is the commit's time; a dirty project is refused; the empty venv never reads the
  workspace's root `uv.toml`; the exit status follows the install and the imports, not *behind*.

---

[The architecture and every decision](../architecture.md#the-decisions)
