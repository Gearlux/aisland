# A renamed project's old distribution is uninstalled before its new one

*2026-10-29* [renamed-installs]

## Context

Two projects of the workspace aisland is developed in were renamed on 2026-10-21: `aisland-manager` became `aisland`
in its own folder, and the curator project took a new name, folder and all. `aisland setup` runs `uv pip install -e
<folder>[extras]`, which installs the name the folder's `pyproject.toml` has now and leaves every other distribution
alone. So the venv held `aisland-manager` and `aisland`, two editable distributions owning one package and one
`bin/aisland`, and uninstalling the old one afterwards deleted the script both had written; it held the curator's old
name too, installed from a folder that no longer existed. The local Jenkins job's Initialize check then found aisland
"not installed", and `aisland.__version__` fell back to `0.0.0.dev0`, which setup wrote into `workspace.toml`'s how-to.

## Decision

The install step (`setup/manager.py`'s `_run_installs`) asks the venv's editable distributions once — `uv pip list
--editable --format json`, the same uv from the same folder as the installs — and uninstalls (`setup/renamed.py`):

- a distribution installed from a project's folder under another name than its `pyproject.toml` has (PEP 503
  normalised), just before that project's group installs; a failed uninstall is that group's failed install, since
  installing beside the old name is what it prevents;
- a distribution installed from a folder directly in the workspace root that no longer exists, before any group
  installs; a failed uninstall is reported and the installs go on.

Each one is said in one line, with where it was installed from. A distribution installed from anywhere else is never
touched: outside the workspace, deeper in it (a session's worktree), or from a folder still there — a deactivated
project's, which `git submodule deinit` leaves empty. A uv that cannot list the distributions gives none: the install
right after runs the same uv and reports the failure itself.

## Consequences

A rename, in place or with its folder, fixes itself on the next `aisland setup`; nobody runs `uv pip uninstall` by
hand. A venv where an older setup already installed both names is fixed too: uv reinstalls a local editable on every
`uv pip install -e` (measured: `~ tool==0.1.0`), so the shared script the uninstall deletes is written again. A group
skipped because its dependency failed keeps its old install, which still works. Setup's install step now runs one
`uv pip list` more.

The first setup after a rename still ran with no `aisland` record: `workspace_file.refresh` writes the how-to before
`_run_installs` installs the new name, and `__version__` is read once, at import. So `aisland/__init__.py` reads the
version from the `pyproject.toml` beside the package when the record is missing — a checkout has one, no wheel does —
with `tomllib` and nothing that raises on import; `0.0.0.dev0` is left only to a wheel without its record, a
`--bundle`. It reads the file itself rather than through `pyproject.py`, which imports click by way of `errors.py`:
every `import aisland` runs this. Moving the how-to after the installs was rejected: it changes setup's step order,
and the file is written first so the installs read the current projects.

```text
the venv has no aisland record; the checkout's pyproject.toml says 0.1.0
before: __version__ = 0.0.0.dev0    how-to: ./aisland-0.0.0.dev0-py3-none-any.whl
after:  __version__ = 0.1.0         how-to: ./aisland-0.1.0-py3-none-any.whl
```

The fix was written on a branch of its own on 2026-10-29 and landed on 2026-11-21, after `setup/` had become a
package: `renamed.py` moved under it and speaks through `console`, and the version no longer asks
`locations.FROM_CHECKOUT`, which by then also asked for a workspace listing the checkout.

## Example

```text
$ .venv/bin/aisland setup
==> Uninstalling oldlib: installed from /w/oldlib, which no longer exists.
==> Installing lib (/w/lib[dev])...
==> Uninstalling tools-manager: installed from /w/tools, whose project is now tools.
==> Installing tools (/w/tools[dev])...
```

## What you may change

What else counts as left over by a rename; never a distribution installed from outside the workspace or from a folder
that is still there. Pinned by `tests/test_renamed_project_install.py` (the real uv, offline); the version without a
record by `tests/test_version.py`.
