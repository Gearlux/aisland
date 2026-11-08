# `--extra NAME`: one uv command installs the project with its wheels (2026-10-27)

## Context

A project installed outside the workspace from a checkout took two commands: `python wheels/install.py --installer uv
--all --no-project --reinstall`, then `uv pip install -e ./<project> --no-deps`. The maintainer wanted one:
`uv pip install -e ./<project>[wheels]`. An extra holds requirement names; what makes uv take the folder's wheels
is a source per name. Measured with uv 0.13.5: a `[tool.uv.sources]` entry conditional on an extra
(`{ path = "wheels/<file>", extra = "wheels" }`) is read by `uv pip install` of the project, applies only while that
extra is asked for (an install without it is unchanged), reaches a wheel only another wheel needs once the extra
lists it, and a wheel rebuilt under the same version is installed again (uv records the file it came from). `uv
lock` resolves every extra, so the sources are locked too (`{ path = … }` in place of the find-links registry).

## Decision

`aisland wheels build … --extra NAME` writes both pieces into the target's `pyproject.toml` between marker lines
(`wheels/extra.py`): the extra at the end of `[project.optional-dependencies]`, the sources at the end of
`[tool.uv.sources]` (each table appended when missing). A rerun removes what is between the markers and writes it
again, so the file is byte-identical when nothing changed and a hand edit outside the markers survives. Everything is
checked before any build: NAME must be in `[tool.aisland] manual-extras` (taken by `aisland setup`, the wheels would
replace the workspace's checkouts), and no key outside the markers may hold NAME or a source of a built wheel.
`--commit` commits `pyproject.toml` with `wheels/`.

## Consequences

- The generated `wheels/README.md` leads with the uv command; `install.py` stays for pip, which reads no uv sources.
- `uv run` in the project resolves every wheel's requirements, a wheel the project does not need included (a wheel
  needing a private index makes `uv run` need it too).
- The project's own wheel built from the commit carries the extra as that commit had it.

## Example

```bash
aisland wheels build app --all --include-project --extra wheels
uv pip install -e ./app[wheels]        # from the folder holding app, on the other machine
```

## What you may change

- Another name than `wheels`, or several runs writing several extras (each its own markers) — not built yet.
- Remembering (asked for 2026-10-27): a build without `--extra` keeps an existing block current, since a
  forgotten flag left the sources naming wheel files the rebuild deleted and the install failed on the other
  machine. Not with `--include-project`, whose build installs the project's own wheel and leaves the block unless
  `--extra` is given; the regenerate command names `--extra` only there.
- Never: tying the extra to `--include-project` (aisland's own run would publish it), or a hand-maintained copy of
  the list beside the generated one.

---

[The architecture and every decision](../architecture.md#the-decisions)
