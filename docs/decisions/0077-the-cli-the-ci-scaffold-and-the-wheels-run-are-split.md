# The CLI, the CI scaffold and the wheels run are split by what they do

*2026-11-02* (the record [module-split]; follows "aisland setup runs steps that each live in a module of their own")

## Context

The release review found three modules the size `manager.py` had been before its split. `cli.py` held all 103
commands in 2,234 lines. `services/jenkins.py` (1,773 lines) mixed the Jenkins service with CI-file generation:
`JenkinsService.scaffold_pipelines` was 341 lines and used `self` only for `self.url()`. `wheels.py` (1,854 lines)
ran its whole `aisland wheels build` in `vendor_wheels`, 277 lines with 83 branches and three nested closures.

## Decision

- `aisland/cli/` is a package. `_base.py` holds `main`, the group and command classes and what several groups share;
  each command group is a module (`workspace`, `configure`, `editor`, `toplevel`, `wheels`, `services`, `tunnel`).
  A module defines its group without `main` (`click.group(cls=Group)`), and `__init__.py` adds every group, so no
  command module imports the package and nothing registers itself on import. The entry point stays `aisland.cli:main`.
- `ci_scaffold.py` is `aisland jenkins scaffold`: `scaffold(project, force, jenkins_url)` over `project_facts`, which
  is the old method's body as functions of its own (the package folder, the selected extras, the internal
  dependencies, the token owner). `services/jenkins_jobs.py` holds the jobs aisland seeds, functions of the Jenkins
  home; only `init_pipelines` takes the service, which it stops and starts around them. `services/jenkins.py` keeps
  the service itself (456 lines).
- `vendor_wheels` is a driver over the run's phases: a frozen `WheelsPlan` of what the target needs and what PyPI has,
  then the extra, the builds, the folder, the checks, the commit. `wheels/report.py`, `wheels/files.py` and
  `wheels/check.py` take the report, the files beside the wheels and the checks; they import nothing of `wheels.py`.
- The code moved unchanged, by script, every multi-line string checked byte for byte; a private name another module
  now uses became public (`provenance_header`, `ZIP_TIME`, `PYPI_INDEX_ARGS`, `owner_of`, `write_wheels`, …).

## Consequences

What a user sees is unchanged: every command's `--help` (163 commands in the workspace aisland is developed in) is
byte for byte what `cli.py` printed. The generated files that named their generator by path now name the new one:
`Jenkinsfile.local`'s limit line (`STAGE_LIMITS in aisland/ci/ci_scaffold.py`) and the wheels folder's `README.md` and
`wheels.yaml` headers (`wheels/files.py`) change on their next regeneration. Tests patch the module whose code makes
the call: `ci_scaffold.is_private_repo`, `jenkins_jobs.setup_global_properties` (since the layering of 2026-11-06 `services.jenkins_settings.setup_global_properties`), `wheels.check.PYPI_INDEX_ARGS`, and
the workspace root through `locations.workspace_root`, never a name bound in one CLI module. In earlier records
`cli._X` is now `aisland/cli/_base.py`'s `_X`, `jenkins.scaffold_pipelines` is `ci_scaffold.scaffold`, and
`services/jenkins.py`'s limits and actions are `ci_scaffold.py`'s.

## Example

The sizes in lines on 2026-11-02; the layering of 2026-11-06 ([architecture](../architecture.md#the-layers))
then moved `ci_scaffold` into `ci/` and the jobs' settings into `services/jenkins_settings.py`.

```text
aisland/cli/        __init__ 33  _base 300  workspace 448  configure 182  editor 141  toplevel 336
                    wheels 294  services 335  tunnel 310
aisland/            ci_scaffold 527   services/jenkins 456   services/jenkins_jobs 816
aisland/            wheels 1,074  wheels_report 419  wheels_files 328  wheels_check 307
```

## What you may change

Which module a command group or a phase lives in, as long as no command module imports the package, the lower wheels
modules import nothing of `wheels.py`, and every `--help` stays what it was unless a change means it to differ.

---

[The architecture and every decision](../architecture.md#the-decisions)
