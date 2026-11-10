# Listing the services imports no project's code (2026-10-29)

## Context

`aisland --help` took 0.65 s, 0.6 s of it importing the class of every service a project implements — the listing
needed each class's options to build its group's commands, although `--help` shows only the group's one line.

## Decision

A plugin service's `Entry` loads its class on first use (`registry._Plugin`), and its group builds its commands on
first use (`cli._base._ServiceGroup`). `registry.entries`, what `aisland services …` acts on, still loads every class first.

## Consequences

`aisland --help` takes 0.24 s and reads only the projects' declarations, one `git config` and the installed metadata.
A plugin that cannot be imported is listed and fails when used, instead of hiding every project service from the
listing.

## Example

```text
$ aisland --help        # imports no acme_plugin
$ aisland acme-db start --help   # imports it: the class's options become the command's
```

## What you may change

Anything the listing shows must come from the declaration (`pyproject.toml`) or the metadata, never from the class.

---

[The architecture and every decision](../architecture.md#the-decisions)
