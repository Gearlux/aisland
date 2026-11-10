# A link to a folder of the workspace is a short name, never a project (2026-10-21)

## Context

A workspace may give its long project folders short names by links in its root (`ml -> machine_learning`,
`od -> object_detection/`). Every loop that looked for projects asked `is_dir()`, which follows a
link, so each such folder was a project twice: `aisland setup`'s extras prompt listed `machine_learning, ml`, `mypy.ini`
gained `od` and `od/torchsig` beside the real paths, and the install, pyright and the VS Code folder list did the
same.

## Decision

- `locations.is_alias(folder, workspace)`: a symbolic link whose resolved target lies inside the (resolved)
  workspace is an alias. Every reader of the workspace's projects skips it — `projects.discover_projects` (which
  recurses with the workspace root, so a link inside a project is judged against the workspace),
  `projects.workspace_folders`, `model.Workspace.top_level_folders`, `model.Workspace.known_names`,
  `workspace.file.workspace_projects`.
  The folder it names is found under its own name.
- A link to a folder OUTSIDE the workspace is no alias: it is how a checkout kept elsewhere joins the workspace.
- Both sides are resolved, so a workspace entered through a link of its own (`~/acme -> /build/acme`) tells
  its aliases all the same.
- The short name is shown where a person reads it: `locations.alias_names` maps each aliased folder to its link's
  name (non-hidden links in the root only; of two links to one folder the first by name), and
  `write_code_workspace` uses it as the folder's `name`, its `path` still the real folder — a file opened in VS Code
  keeps its real path.

## Consequences

- A link inside the workspace to a folder no reader reaches (`x -> data/proj` with no project at `data/`) is not
  found either: a project lives in a project folder, a link only names one.
- Setup only appends to `mypy.ini`: entries an earlier setup added through a link stay until removed by hand.
- A workspace without such links gets byte-identical files.
- `workspace.toml` writes the short name as the project's `path` (decided 2026-10-21: links come back as direct
  checkouts): a root link to a project, or a folder checked out under a name other than its repository's (the
  project is then named after its repository), is written as `path`; `init --from` clones the project straight into
  that folder — never a link, which stays a choice a user makes by hand. (The `alias` key the removed
  `<name>.workspace.toml` carried for this is gone with it.)

## Example

```python
from pathlib import Path
from aisland import editor_files, locations

root = Path("~/acme").expanduser()        # ml -> machine_learning, od -> object_detection/
locations.is_alias(root / "ml", root)     # True
locations.alias_names(root)[(root / "machine_learning").resolve()]   # 'ml'
editor_files.write_code_workspace(root)          # folders: {"name": "ml", "path": "machine_learning"}, ...
```

## What you may change

- The rule for which link names a folder when several do (`alias_names`), with
  `tests/test_symlinked_folders.py::test_of_two_short_names_for_one_folder_the_first_by_name_wins`.
- A new reader of "the workspace's projects" skips aliases through `is_alias`, never its own `is_symlink` test.
- Never: treating a link to an outside folder as an alias — it is the one way such a checkout joins.

---

[The architecture and every decision](../architecture.md#the-decisions)
