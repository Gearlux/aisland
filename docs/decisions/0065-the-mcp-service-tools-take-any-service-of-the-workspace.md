# The MCP service tools take any service of the workspace (2026-10-29)

## Context

The schema of `service_start|stop|restart` was `Literal["jenkins", "mlflow"]`: MLflow had become a project's service,
and Docker, SwiftBar and every other project's service could not be reached. A schema becomes public with the release.

## Decision

`name` is a string; the tool checks it when it runs against the workspace's services (chosen 2026-10-29,
replacing jenkins and mlflow only) and passes `--background` only where the service takes it.

## Consequences

The schema names no service, so it never goes stale as projects come and go; an unknown name is refused in the
command's words, listing the services.

## Example

```text
service_start(name="clearml") → error: no service 'clearml' (the services: docker, jenkins, swiftbar)
```

## What you may change

The set of tools; never a tool for what prompts, runs for minutes or deletes.

---

[The architecture and every decision](../architecture.md#the-decisions)
