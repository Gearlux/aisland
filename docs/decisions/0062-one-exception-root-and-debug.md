# One exception root, and `--debug` (2026-10-29)

## Context

The release review counted 116 `raise click.ClickException` in library code, five exception classes deriving from
click's and three from bare `Exception`, two library functions ending the process with `SystemExit(1)`, a module
printing its own `Error:` lines and returning 1, eight `except Exception: pass`, and no way to see where an error came
from: an `OSError` reached the user as a raw traceback, a refusal never showed one.

## Decision

`errors.AislandError` is the root; it IS a `click.ClickException`, so the CLI's printing (one `Error:` line, exit 1)
needs no wrapper and code catching click's class keeps working. Each class aisland defines derives from it and the
builtin it stands for. The group turns an `OSError` or `SubprocessError` (`CalledProcessError`, `TimeoutExpired`) into one `Error:` line; `--debug` shows
the traceback of both; any other exception is a bug and keeps its traceback. An `except` names what fails there.
Downloads go through `downloads.download` (urllib's `urlretrieve` has no timeout).

## Consequences

`aisland check` now ends with `Error: the quality gates failed: <projects>` instead of a silent exit 1; the source
commands raise instead of returning 1. A library function never ends the process.

## Example

```python
class ReleaseProbeError(AislandError, ConnectionError): ...
# aisland --debug docker start → the traceback, then "Error: the Docker engine is not running. …"
```

## What you may change

Which builtin exceptions the group words as one line (today `OSError` and `SubprocessError`, which holds `CalledProcessError` and `TimeoutExpired`): add one only for a
failure of the machine, never of aisland's own logic.

---

[The architecture and every decision](../architecture.md#the-decisions)
