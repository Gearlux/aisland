# A project's wheels folder is a find-links path of the generated `uv.toml` (2026-10-23)

*2026-10-23* [wheels]

## Context

A project shipping the wheels of what PyPI lacks (`aisland wheels build`) already carried `[tool.uv] find-links =
["wheels"]`, but uv reads a project's `[tool.uv]` only when it runs inside that folder. `aisland setup` runs uv from
the workspace root, so the wheels were never seen: installing the project failed on a dependency only the folder held
until `python wheels/install.py` had run by hand.

## Decision

The generated root `uv.toml` lists each activated project's `wheels/` folder (marked by its `wheels.yaml`) under
`find-links`, relative to the file. Measured: uv resolves the relative path against the file's folder, from the root
and from a folder below it; it installs a wheel only the folder has; and it takes the folder's `corelib-0.4.1` over
PyPI's `corelib-0.4.1` whose files differ — pip, through `--find-links`, takes PyPI's. The maintainer chose every such
folder, tracked in git or not.

## Consequences

- Setup, `aisland workspace configure … set` and any `uv pip install` run in the workspace use the wheels.
- A dependency taken from PyPI (`source = pypi`) comes from a wheels folder whenever the folder holds the same or a
  newer version; a project checked out in the workspace still installs from its checkout.

## Example

```toml
# <workspace>/uv.toml, generated
find-links = ["bridge/wheels"]
override-dependencies = ["opencv-python ; sys_platform == 'never'", "opencv-python-headless>=4.12,<5"]
```

## What you may change

- Which folders count (the `wheels.yaml` marker).
- Never: an absolute path in the generated file, or `install.py` named as a step of setup.

---

[The architecture and every decision](../architecture.md#the-decisions)
