# `aisland workspace configure`: one set of commands built from each plugin's keys; `source` the first

*2026-10-22* [sources] — the committed and per-machine layers of a project's source move.

## Context

Each setting had its own command group (`aisland source`, `aisland mirror`, `aisland framework`, `aisland tracker`),
each with its own list/set/unset written by hand, and the source's committed half sat in `config.toml`'s
`[sources.*]` while the project's url and branch sat in `workspace.toml` — a project's branch twice, its
distribution often written equal to its name.

## Decision

`aisland workspace configure` builds `<plugin>` (the listing, `--json`), `set PROJECT VALUE [--<key> VALUE…]
[--machine-only]` and `unset PROJECT [--machine-only]` from a `configure.Plugin`'s keys; a plugin writes no command.
`source` is the first: its settings go into the project's table of `workspace.toml` (`source`, `version`, and
`distribution` only where it differs from the name; a git source's ref is the project's own `branch`), this machine's
into `.git/config` as `aisland.source.<project>.source|branch|version`. No backward compatibility: an old table or
one-key override is simply not read (decided 2026-10-22, `mirror`'s step). `source apply` is gone: setup applies.
`mirror` followed the same way: the project's `mirror`, `aisland.mirror.<project>.mirror`, the action
`aisland workspace mirror sync` one level up.

## Consequences

- `aisland source …` answers `No such command 'source'`; the generic commands carry every later plugin.
- The workspace aisland is developed in: its three entries and three per-machine keys were converted by hand; the effective source of each is unchanged.

## Example

```text
$ aisland workspace configure source set loggair local --branch dev/main --machine-only
$ git config --local --get-regexp '^aisland\.source\.loggair'
aisland.source.loggair.source local
aisland.source.loggair.branch dev/main
```

## What you may change

- A plugin's keys, help and listing columns; a new plugin in `configure.PLUGINS`.
- Never: a plugin with commands of its own, or a setting written twice in `workspace.toml`.

---

[The architecture and every decision](../architecture.md#the-decisions)
