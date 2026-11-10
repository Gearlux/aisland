# Warnings go to stderr, and the doctor and the notebook filter run outside a workspace

*2026-11-01*

## Context

The release review ran aisland as a PyPI user would: from a wheel, outside a workspace. `aisland doctor` and
`aisland notebooks clean` were refused there (`No workspace here`), though neither reads a workspace — so three of
the five examples failed — and a warning about a setting printed to stdout broke `--json`: `aisland workspace configure
source --json` started with `  - Warning: …workspace.toml:4: project 'lib' has source 'pypy'…`.

## Decision

- `doctor` and `notebooks` join `cli._base._WORKSPACE_FREE`; `completion` does not — it writes completion for a workspace's
  venv — and its example makes a throwaway workspace.
- `failures.warn` prints a warning on stderr and fails nothing; every `  - Warning:` line goes through it.
- The pytest plugin's hooks catch any error of the plugin's own and print one line: the plugin loads in every pytest
  run of the environment.

## Example

```bash
cd ~/Downloads && aisland doctor              # Environment doctor — …/bin/python, one line per check
aisland workspace configure source --json | python -m json.tool   # the warning on stderr, the JSON whole
```

## What you may change

Add a command to `_WORKSPACE_FREE` when it reads no workspace. Never print a warning on stdout.

---

[The architecture and every decision](../architecture.md#the-decisions)
