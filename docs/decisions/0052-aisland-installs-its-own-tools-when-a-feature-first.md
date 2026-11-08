# aisland installs its own tools when a feature first needs one, after asking (2026-10-28)

[on-demand-tools]

## Context

`aisland setup` ran Homebrew for every one of aisland's tools — process-compose, docker, colima, act, SwiftBar — then
downloaded the Jenkins WAR and installed the Java it names, on every machine, whatever the workspace used. A service
command that found process-compose missing installed it at once, adding a third-party tap, from a status-like command
or a script. The release review found a two-project workspace getting a GUI app, a container engine and a JDK it never
asked for.

## Decision

Setup installs what the projects declare (`provision.install(root, aislands_own=False)`). A feature that finds its
tool missing asks — `<who> needs <tool>, which is not installed. Install it (<command>)?` — and installs on a yes;
without a terminal, or on a no, it refuses with the line that installs it and installs nothing. `aisland workspace
provision install`, asked for by name, still installs everything.

## Consequences

- A fresh workspace that never starts a service installs no system software beyond its projects' declarations.
- The first `aisland services start` / `docker start` / `swiftbar start` / `jenkins start` asks once; in CI or an MCP
  call it refuses with the install line.

## Example

```text
$ aisland services start mlflow < /dev/null
Error: aisland's services need process-compose, which is not installed. Install it: brew install f1bonacc1/tap/process-compose
```

## What you may change

- Which features ask for which tool (`provision.NEEDED_BY`).
- Never: installing one of aisland's tools without asking, or asking where no one can answer.

---

[The architecture and every decision](../architecture.md#the-decisions)
