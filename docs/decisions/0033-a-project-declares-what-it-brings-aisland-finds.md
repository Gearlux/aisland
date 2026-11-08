# A project declares what it brings; aisland finds it in two stages and the project never imports aisland

*2026-10-22* [declarations]

## Context

aisland hard-coded what belongs to the projects: the `framework` and `tracker` selections are a trainer's ideas
(`selection.py`'s `FRAMEWORKS`, `TRACKERS`), and the services each project needs (`aim`, `clearml`, `labelstudio`,
`fiftyone`, `iqengine`) lived in `aisland/services/`. A workspace without the trainer still offered `aisland
framework set`, and a project could not bring a service of its own.

## Decision

A project declares what it brings, and aisland finds it in two stages. STAGE 1 reads every project's
`pyproject.toml` — `[tool.aisland.selection.<name>]`, `[tool.aisland.service.<name>]` and the entry points of the
group `aisland.plugins` it declares — so a fresh machine can ask a selection's question before the project defining
it is installed. STAGE 2 loads those entry points once the project is installed. A plugin is any object with a
`kind` and that kind's methods: the project implements a protocol and imports nothing from aisland, so it never
gains a dependency on its dev tool. Only what a workspace project declares is loaded.

Rejected: plugins that subclass an aisland base class (every library would depend on aisland); entry points alone
(nothing can be read before the install, so setup could not ask its questions first); a hand-written list of
plugins in aisland (the hard-coding this replaces).

## Consequences

- `aisland workspace show` lists what was found and from which project; a declaration aisland cannot use stops it
  with one line naming the file and the line of the table.
- Choices offered for a selection no project defines are reported, not refused: a workspace without the defining
  project, or with it switched off, still sets up.
- The later steps of the redesign built on this: `workspace.toml` and the generated `aisland workspace configure`,
  the trainer declaring `framework`/`tracker`, and services declared by the projects that need them (the records
  below).

## Example

```toml
[tool.aisland.selection.framework]
question = "Which ML frameworks should this machine install?"
choices = ["torch", "keras"]

[project.entry-points."aisland.plugins"]
acme-db = "acme_plugin:Database"
```
```python
class Database:            # the project's own code — no aisland import
    kind = "service"
    def start(self, ctx): ...
    def stop(self, ctx): ...
    def status(self, ctx): ...
```

## What you may change

- A new declaration (`[tool.aisland.<kind>.<name>]`) or plugin kind (`PluginKind` with `_KIND_METHODS`), each with
  its refusals and tests in `tests/test_discovery.py`.
- The wording of `aisland workspace show`.
- Never: a plugin that must import aisland, or loading an entry point no workspace project declares.

---

[The architecture and every decision](../architecture.md#the-decisions)
