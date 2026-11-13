# `workspace.toml` says its format, and a key nobody reads is named (2026-10-30)

## Context

`workspace.toml` becomes a public format with the first release, and it had no version: a later aisland that changes
its shape could only guess which shape a file is. And it kept every key of a project's table as written, because the
later steps own them, so a typo was kept too and did nothing: `sorce = "pypi"` left the project local without a word.

## Decision

`[workspace] format = 1`, written first by every render; a file without it is format 1, a newer one is refused naming
the upgrade. The keys every part of aisland reads are listed together in `workspace_check` — the file's own, each
plugin's, the workspace's switches and every selection — and any other key is named with its line and the closest
known key. `aisland setup`, `workspace show` and `workspace configure` print the warnings and go on.

## Consequences

A file gains the line the next time aisland writes it for another reason; until then it reads as format 1. A setting added to aisland adds its key to
`workspace_check`, or every workspace that uses it is warned. A future format 2 can read format 1 and say so.

## Example

```text
[workspace]
format = 1

[project."models"]
url = "git@github.com:acme/models.git"
sorce = "pypi"
# Warning: workspace.toml:6: project 'models': sorce is no setting aisland reads (did you mean source?)
```

## What you may change

Which commands print the warnings; never turn a warning into a refusal — the file is the user's.

---

[The architecture and every decision](../architecture.md#the-decisions)
