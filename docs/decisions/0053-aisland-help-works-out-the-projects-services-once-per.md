# `aisland --help` works out the projects' services once per run (2026-10-28)

## Context

A finding of the release review's second pass: `aisland --help` took 2.4 s in a workspace bringing six services.
`registry.declared` ran once for the listing and once per group (click asks each group for its short help), and
every run scanned the installed entry points once per plugin — 42 scans, each reading every distribution's metadata.

## Decision

The top-level group works out the services the projects bring once per CLI run (`cli._base._WorkspaceGroup._declared`,
kept in `ctx.meta`), and discovery scans the installed entry points once for every plugin (`discovery.installed`).
0.9 s remained then: importing the plugin classes, which their groups needed — removed the next day (record "Listing
the services imports no project's code").

## Consequences

`--help` and every lookup of a project's service read the declarations and the installed plugins once; a changed
declaration is read by the next command, since nothing outlives the run.

## Example

```python
def _declared(self, ctx: click.Context) -> List["service_registry.Entry"]:
    key = "aisland.declared-services"
    if key not in ctx.meta:
        ctx.meta[key] = service_registry.declared(get_workspace_root(), reserved=self.commands)
    return cast(List["service_registry.Entry"], ctx.meta[key])
```

## What you may change

- Where the answer is kept, with `tests/test_plugin_services.py::test_one_help_finds_the_projects_services_once` and
  `::test_loading_reads_the_installed_entry_points_once`.
- Never: a group or a plugin that reads the declarations or scans the entry points again within one run.

---

[The architecture and every decision](../architecture.md#the-decisions)
