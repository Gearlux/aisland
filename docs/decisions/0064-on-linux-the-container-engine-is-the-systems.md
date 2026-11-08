# On Linux the container engine is the system's (2026-10-29)

## Context

`aisland services start` started Docker first by default, and Docker meant Colima: on Linux, where Colima is no tool
aisland installs, the start raised before Jenkins was reached.

## Decision

`docker_service()` is `LinuxDockerService` on Linux: the system's Docker Engine, checked with `docker info` and never
installed, started, stopped or wiped by aisland (all of that takes root). Of several services, one that fails is
reported and the others still start (`registry._each`), the rule `failures.py` already stated.

## Consequences

On a Linux machine without Docker, `aisland services start` starts Jenkins, prints the engine's error and exits 1.

## Example

```text
$ aisland docker start      # Linux, engine stopped
Error: the Docker engine is not running. Start it: sudo systemctl start docker
```

## What you may change

What the Linux engine reports; keep it read-only.

---

[The architecture and every decision](../architecture.md#the-decisions)
