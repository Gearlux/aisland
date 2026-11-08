# System tools: declared by the projects, checked before installing, aisland's own in the package

*2026-10-23* [provision]

## Context

`aisland provision` (and setup's first step) read `[tool.aisland.sys-dependencies]` from every checked-out project,
aisland's own `pyproject.toml` among them, and installed the lot. A wrong key or a value that was no list was skipped
silently, an unknown userspace tool was named only on Linux in the middle of setup, nothing showed what a machine
needed without installing it, and a workspace whose aisland came from a wheel had no aisland `pyproject.toml` to
read — it got no service supervisor.

## Decision

Discovery reads the declaration (stage 1) and refuses what it cannot use, naming the file and the line. aisland's own
tools are `provision.AISLAND_TOOLS`, in the package. `aisland workspace provision` lists this machine's tools (who
needs each, whether it is installed) and `install` installs them, the function setup runs (since 2026-10-28 for
the projects' declarations only: record "aisland installs its own tools when a feature first needs one, after
asking"); `aisland provision` is gone. There is no configure plugin: which projects a machine has (the `active` plugin) already decides its tools.

## Consequences

- The same tools install from a checkout and from a wheel.
- A bad declaration fails `aisland setup` before its first install.

## Example

```text
$ aisland workspace provision
  TOOL                           HOW   PROJECTS      INSTALLED
  f1bonacc1/tap/process-compose  brew  aisland       yes
  node                           brew  canvas  yes
```

## What you may change

- The listing's columns; a new tool in `AISLAND_TOOLS` or a project's declaration.
- Never: aisland's tools back in its `pyproject.toml`, or an install that starts before the declarations are read.

---

[The architecture and every decision](../architecture.md#the-decisions)
