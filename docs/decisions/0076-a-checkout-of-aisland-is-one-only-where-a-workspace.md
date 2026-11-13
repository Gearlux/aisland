# A checkout of aisland is one only where a workspace lists it

*2026-11-02* (narrows "From a checkout everything is found from aisland's place")

## Context

`FROM_CHECKOUT` was true for any folder holding aisland's `pyproject.toml` beside the package — every clone. A
contributor's clone in `~/code/aisland`, installed editable, then took itself as the workspace (its own `TASKS.md`)
and ignored the folder it ran in and `AISLAND_WORKSPACE`. Measured on such a clone, run from a workspace `acme`
beside it: `aisland workspace show` and `services status` worked on the clone, and `aisland setup` refused with
`Virtual environment not found at …/aisland/.venv. Run aisland/setup.sh first.`

## Decision

A checkout runs as one only when a workspace above it lists it (`locations.listing_workspace`): the nearest folder,
up to five above, whose `.gitmodules` has a submodule at the checkout's path or whose `workspace.toml` has a project
in it. Its workspace is that folder. Any other clone works like a wheel. Read with the standard library only: the
pytest plugin imports the module.

## Consequences

The workspace aisland is developed in is unchanged (its `.gitmodules` lists `aisland`). A contributor's clone works on the workspace it is run in.
GitHub CI, which checks aisland out alone, now runs aisland's tests in wheel mode: a test of checkout mode sets
`FROM_CHECKOUT` itself or carries `needs_workspace`.

## Example

```text
~/code/aisland   (clone, pip install -e)      ~/work/acme   (a workspace)
$ cd ~/work/acme && aisland workspace show    # before: worked on ~/code/aisland; now: on ~/work/acme
```

## What you may change

How far above the checkout to look. Never make a `TASKS.md` or a repository above a checkout count as listing it.

---

[The architecture and every decision](../architecture.md#the-decisions)
