# A project's `pyproject.toml` and `.gitmodules` each have one reader (2026-10-30)

## Context

Fifteen places in nine modules opened a project's `pyproject.toml` with `tomllib`, each with its own handling: most
took a broken file as one declaring nothing, discovery and `wheels build --extra` refused it naming the file, the rest
let tomllib's error through. Three modules parsed `.gitmodules`, each with a `git config --get-regexp` process of its
own and its own rule for a key written twice (`submodule_active_map` took the first `path`, `workspace_file` the last,
`local_workspace` kept both).

## Decision

`pyproject.py` holds the three answers the callers had — `read` (`{}`), `parse` (`PyprojectError`, `<file>:
<tomllib's message>`) and `load` (tomllib's errors) — and `name`; every caller keeps the one it had, word for word.
`gitmodules.py` asks git once for `path`, `url`, `branch` and `active` and returns a frozen `Submodule` per submodule;
the three readers keep their names and shapes as views over it. A repeated key is read as git reads it, the last value
(`git config --get`; `git submodule init` registers the last `path` and `url`, measured with git 2.54).

## Consequences

The CI files of every project are byte-identical (the scaffold rendered before and after, PyPI, GitHub and Jenkins
stubbed). A `.gitmodules` repeating `path` inside one submodule now reads the same in all three views, git's last
value. The standard-library-only modules and `harness/` keep their own reads: they run where aisland is not installed.

## Example

```python
from aisland import gitmodules, pyproject

pyproject.read(Path("models/pyproject.toml")).get("project", {})   # {} when missing or broken
pyproject.parse(text, path)                                         # PyprojectError: models/pyproject.toml: ...
gitmodules.read(root)  # [Submodule(name='models', path='models', url='git@…', branch='main', active=True), …]
```

## What you may change

Add a function a new caller needs (a table it reads in several places); never let a caller go back to `tomllib` or its
own `--get-regexp`, and never give `.gitmodules` a second rule for a repeated key.

---

[The architecture and every decision](../architecture.md#the-decisions)
