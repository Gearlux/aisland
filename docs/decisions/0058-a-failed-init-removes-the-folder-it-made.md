# A failed `init` removes the folder it made (2026-10-28)

## Context

A finding of the release review's second pass: `init --from` stopped by a project that cannot be cloned left a
repository holding the first projects, and the same command then refused it ("already a git repository").

## Decision

A folder `init` made, or found empty, is removed when a clone fails or the run is interrupted, and the error says so:
the same command runs again. A folder that held files is never deleted; the error names the `.git` and
`.gitmodules` to remove. An existing repository set up in place is left as it stopped.

## Consequences

A failed `init` of a new workspace leaves nothing behind but the error.

## Example

```text
$ aisland workspace init acme --from workspace.toml      # tools cannot be cloned
Error: [2/2] tools: could not add … as a submodule: …
/home/you/acme is removed again: this init made it. Run the same command once the cause is fixed.
```

## What you may change

- The wording, with `tests/test_workspace_init.py::test_a_failed_init_from_a_file_removes_the_folder_it_made_and_runs_again`
  and its neighbours.
- Never: deleting a folder that held files before the init.

---

[The architecture and every decision](../architecture.md#the-decisions)
