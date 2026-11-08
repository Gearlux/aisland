# Imports at the top, containers typed, one completion guard

*2026-11-02* (follows "The CLI, the CI scaffold and the wheels run are split by what they do")

## Context

After the module split the release review still counted 40 imports inside functions (13 in the CLI alone), 22 bare
`dict`/`set` types that `mypy --strict` named, and eight copies of one `try … except Exception: return []` around the
shell-completion callbacks. Two job builders of `services/jenkins_jobs.py` were 154 and 177 lines.

## Decision

- Every in-function aisland import moved to the top where the module-level import graph stays acyclic: 30 did. Two
  stay, each closing a real cycle (`projects` ↔ `workspace_file` through `discovery`, `provision` ↔
  `services.jenkins`); `supervisor`'s, commented as a cycle, was none, and its copy of the state paths now comes from
  `paths.py`. `tests/test_imports.py` imports every module first in a fresh interpreter: with the `projects` cycle put
  back, 25 modules fail it.
- `disallow_any_generics` for the package: `projects.ProjectMetadata` and `mcp_config.McpServer` (TypedDicts) for the
  shapes read by key, `AbstractSet[str]`, `Dict[str, str]`, `CompletedProcess[str]` elsewhere.
- `errors.quiet_completion` wraps every completion callback; it is the one `except Exception` around aisland's own code.
- `init_pipelines` and `_create_orchestrator_job` read as their steps (`_projects_to_seed`, `_seed_project_job`,
  `_multibranch_config`, `_stop_with_a_config_xml`; `_build_phases`, `_phase_stages`, `_build_stage`,
  `_orchestrator_script`, `_orchestrator_config`).

## Consequences

Nothing a user sees changes: every job's `config.xml` the old and the new `aisland jenkins init` write for one
workspace — the CI and the local jobs, the orchestrators, the nightly check, the global config — is byte for byte the
same, as is what it prints; `import aisland.cli` is not slower (0.34 s against 0.45 s before). A guarded callback now
covers its whole body, not only its read.

## What you may change

Which helper a step lives in. Never an import moved to the top that makes `tests/test_imports.py` fail; break the cycle
or leave that import in its function with the reason on its line.

---

[The architecture and every decision](../architecture.md#the-decisions)
