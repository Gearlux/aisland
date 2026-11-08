# The test-run record is written only in an aisland workspace (2026-10-29)

## Context

The pytest plugin loads in every pytest run of an environment aisland is installed in. Installed from a wheel, it took
the workspace to be whatever `find_workspace` answered, and for a lone repository that is its parent: `pytest` in
`~/code/app` wrote `~/code/.aisland/data/test-stats/app/<date>.jsonl`, uninvited, on a machine that had never set up a
workspace. The off switch did not help there: `test-stats off --machine-only` said "not recorded" and wrote nothing
(`~/code` is no git repository), and `test-stats off` wrote a `workspace.toml` into `~/code`.

## Decision

Record only in an aisland workspace (chosen 2026-10-29, option 1): `locations.is_workspace` — from a checkout
always; from a wheel a folder holding `workspace.toml` or `.gitmodules`, or the one `AISLAND_WORKSPACE` names.
`test-stats on|off` refuses outside one, and `--machine-only` refuses where the workspace is no git repository.
`find_workspace` keeps its rules: `workspace init` and `setup` still start in a lone repository.

## Consequences

A workspace `aisland workspace init` made, or `aisland setup` wrote down, records as before, and so does the
workspace aisland is developed in. A folder of projects that never ran `setup` records nothing until it does.

## Example

```python
locations.is_workspace(Path("~/code").expanduser(), {})          # False: nothing written beside ~/code/app
locations.is_workspace(Path("~/code").expanduser(), {"AISLAND_WORKSPACE": str(Path("~/code").expanduser())})  # True
```

## What you may change

The markers (`locations.WORKSPACE_MARKERS`) — add one only for a file aisland itself writes into every workspace.

---

[The architecture and every decision](../architecture.md#the-decisions)
