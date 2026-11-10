# The workspace's name can be written in `workspace.toml` (2026-10-23)

*2026-10-23* [workspace-file]

## Context

The name — the editor file, the Jenkins jobs, the how-to's `--prompt` — was the origin's repository name, else the
folder's. A workspace in a folder called `workspace`, with no origin, was named `workspace` with no way to change it,
and the how-to used the folder's name even where the origin gave another.

## Decision

`[workspace] name` in `workspace.toml` comes first, then the origin, then the folder, read in one place
(`projects.get_workspace_name` through `workspace.file.name_of`); the how-to uses the file's name too. A name must be
one plain name; a selection may not be called `name`, since a selection's choice is committed under its own name in
the same table.

## Consequences

- Setting a name renames the editor file at the next setup (the old one stays until removed) and the Jenkins jobs.
- A workspace without a name is named as before.

## Example

```toml
[workspace]
name = "studio"
extras = ["dev", "local"]
```

## What you may change

- The order after the file's name.
- Never: a second place a name is read from.

---

[The architecture and every decision](../architecture.md#the-decisions)
