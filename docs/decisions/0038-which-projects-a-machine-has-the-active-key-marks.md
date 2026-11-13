# Which projects a machine has: the `active` key marks a project optional, its value the default

*2026-10-23* [activation]

## Context

A project was optional when it shipped a `uv.toml` or `.gitmodules` said `active = false`; this machine's answer was
git's own `submodule.<name>.active`, which `git submodule deinit` erases (so setup wrote it back after every deinit),
and `--activate`/`--deactivate` were setup flags. `workspace init --from` cloned every project, the private ones too.

## Decision

`workspace.toml` says it: a project with an `active` key is optional and the value is its default. `init --from`
clones only the projects without the key; `aisland setup` asks about each optional one and keeps the answer in
`aisland.active.<project>.active`, then checks out (adding a project the workspace does not have yet from its url and
branch) or empties each folder, and writes what git reads — `.gitmodules` `active = false` for a default-off project
and `submodule.<name>.active` — from the effective value. `aisland workspace configure active set|unset
[--machine-only]` is the one way to change it; the flags and the deinit write-back are gone.

## Consequences

- A fresh machine gets only the projects everyone has, and is asked about the private ones once.
- An optional project `.gitmodules` lacks is not "gone": setup's upkeep keeps it in the file.

## Example

```toml
[project."tools"]
url = "https://bitbucket.example.com/scm/team/tools.git"
active = false
```

## What you may change

- The question's wording and the listing's columns.
- Never: a second place that makes a project optional, or a choice kept in a key git can erase.

---

[The architecture and every decision](../architecture.md#the-decisions)
