# From a checkout aisland finds everything from its own place; from a wheel, from where it runs (2026-10-19)

## Context

Every path aisland uses was found from the package's own file: the workspace by walking up from
`aisland/aisland/setup/manager.py`, the state as `aisland/data/` beside the package, the git scripts as
`<root>/aisland/…`, the CI scaffolder as `parents[3]`. That is right for the checkout it was
written in, and meaningless for a wheel. Measured in an empty venv, run from another project's
folder, before this record:

```text
workspace root: …/venv/lib/python3.13
DATA_DIR      : …/venv/lib/python3.13/site-packages/data
template      : …/site-packages/code-workspace.json False
```

The workspace that owns this checkout must keep working exactly as it does (decided
2026-10-19), and a wheel must find a workspace of the user's choosing.

## Decision

One standard-library module, `aisland.locations`, decides, and every path asks it:

- **Which installation is this?** `FROM_CHECKOUT` is aisland's `pyproject.toml` beside the package.
  A wheel never carries one, an editable checkout always has it. No setting, no environment
  variable: the answer is a fact about the files.
- **From a checkout**, the old rule, moved verbatim (`checkout_workspace_root`), and the state in the
  checkout's own folder. The cwd and `AISLAND_WORKSPACE` are not read.
- **From a wheel**, `AISLAND_WORKSPACE` when set, else `find_workspace` (the next record: a folder of
  projects is a workspace too); the state in `<root>/.aisland/`.
- **Nothing raises at import, and no path is fixed then.** The state's folders are functions
  (`paths.data_dir()`, `supervisor.config_file()`, the pytest plugin's `stats_folder()`), asked each time
  through `locations.state_dir()`; outside a workspace they fall back to the cwd, and the CLI's top-level
  group refuses every command that works on a workspace before it writes anything. (They were module
  constants until 2026-11-04: a process that outlived a change of workspace, and a test that pointed the
  root elsewhere, kept the folders of the workspace found at import — the pytest plugin's report tests read
  the real workspace's recording switch that way.) Creating a workspace (`aisland workspace init`) and installing wheels elsewhere
  run anywhere.
- **What a wheel cannot find beside itself, it carries.** The editor template, the git scripts and
  `init`'s templates are package data; `project.bashrc` stays at `aisland/project.bashrc` (its place
  there makes the root its parent) and `setup.py` copies it into a wheel, from which `aisland setup`
  writes it to `.aisland/` — where its parent is again the root. `MANIFEST.in` keeps it in the source
  package too: `python -m build` builds the wheel from the source package, not from the checkout.
- **The scripts take the root from outside.** `setup_git.sh [root]`; installed elsewhere it writes a
  `pull-all` alias that names `git_pull_all.sh` by its path and passes the root in
  `GIT_PULL_ALL_ROOT`. In the checkout the alias keeps its old form.

Rejected: resolving every path from the cwd in both modes (a checkout run from `/tmp` or another
repository would act on that folder — the ruling forbids the change); turning the path constants
into functions (dozens of call sites and default arguments, for a value that never changes within
one process); a marker file (`aisland.toml`) a workspace must hold (one more file to know about,
when a git repository already marks one).

## Consequences

- A checkout's behaviour is pinned path by path (`tests/test_locations.py`), so a change that lets
  the cwd leak into checkout mode fails there.
- From a wheel, the aliases point into the venv: recreating it needs `aisland setup` again, which installs them.
- aisland's own system tools then came from its `pyproject.toml` `sys-dependencies`, which a wheel workspace does
  not discover; since 2026-10-23 they are `provision.AISLAND_TOOLS`, in the package (record "System tools"), so a
  wheel workspace has them too.
- The editor template names the workspace only as `${workspaceFolder:WORKSPACE}` and `WORKSPACE-bash`; the
  name is replaced by the workspace's.

## Example

```text
$ aisland workspace init acme            # aisland from a wheel
==> Created workspace acme in /Users/you/acme
$ cd acme/models && python -c 'from aisland import paths; print(paths.data_dir())'
/Users/you/acme/.aisland/data
$ git config alias.pull-all
!GIT_PULL_ALL_ROOT="$(git rev-parse --show-superproject-working-tree --show-toplevel | head -n 1)" bash "/Users/you/acme/.venv/lib/python3.13/site-packages/aisland/git/git_pull_all.sh"
```

## What you may change

- The wheel-mode rule (`find_workspace`) and the state folder's name (`STATE_FOLDER`), with their
  tests.
- Which commands run outside a workspace (`cli._base._WORKSPACE_FREE`).
- Never: the checkout's rule, or anything that makes a checkout read the cwd or `AISLAND_WORKSPACE`.

---

[The architecture and every decision](../architecture.md#the-decisions)
