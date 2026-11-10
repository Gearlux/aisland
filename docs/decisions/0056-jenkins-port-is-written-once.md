# Jenkins' port is written once (2026-10-28)

## Context

A finding of the release review's second pass: Jenkins' port 9080 was typed in thirteen places. Started with
`--port 9081`, Jenkins still got its safe restart, its Jenkinsfile validation and the SwiftBar links on 9080.

## Decision

The port is written once, `JenkinsService.DEFAULT_PORT`, and everything that talks to Jenkins asks
`JenkinsService.url()`: the port it runs on, else the default — `info`, the safe restart, the scaffold's Jenkinsfile
validation, `aisland tests nightly`'s links, the SwiftBar menu's links and the CLI's `--port` default.

## Consequences

A Jenkins started on another port is reached on that port by every part of aisland while it runs.

## Example

```python
def url(self) -> str:
    """Where Jenkins answers: the port it runs on, else :attr:`DEFAULT_PORT` (it is not running, or not here)."""
    ports = self.get_ports()
    return f"http://localhost:{ports[0] if ports else self.DEFAULT_PORT}"
```

## What you may change

- The default, in `DEFAULT_PORT` alone, with `tests/test_jenkins_port.py`.
- Never: a port typed beside `DEFAULT_PORT`.

## A port once given stays (2026-11-07)

`restart` and `jenkins init` brought Jenkins back on 9080 even when it had been started with `--port`: the CLI's
`--port` defaulted to `DEFAULT_PORT` and `init` relaunched with no port. `JenkinsService.port()` is now the port it runs
on, else the one its supervisor registration records (the last it ran on; a `reset` drops it), else `DEFAULT_PORT`.
`launch(port=None)` takes it, `url()` reads it, and `--port` has no default of its own. The number is still written
once.

---

[The architecture and every decision](../architecture.md#the-decisions)
