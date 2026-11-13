# The example environment: each project lists its variables, aisland concatenates them (2026-10-27)

## Context

Every config and project reads machine settings from the workspace's `.env` (`DATA_ROOT`, a service's token, a
library's switch), and a new machine had nothing to start from but `workspace init`'s five-line template; which
variables a project reads lived in its README if anywhere, and drifted. The maintainer asked for the variables in every
project and an aisland command that concatenates them into a file to copy to `.env`; their choices: two files per
project — `env.complete` (every variable, written by the agent) and `env.example` (the ones a person normally sets,
written by the user) — the workspace's two files generated per machine and never committed, a variable several
projects read merged under the project that defines it, a generated test in each project, `--missing` to compare an
existing `.env`.

## Decision

- A project's file is a small line grammar (`env_example.read_project`): `# NAME=VALUE  description` (two spaces part
  value and description), `# -- heading`, `# not for .env: NAME … — reason`, other comments. A line that is no
  comment is refused naming its line — the file is copied to `.env`, so it must set nothing.
- `aisland workspace env-example` (and `aisland setup`, step 5b) reads every checked-out project's files of both kinds
  before writing either, and renders each kind in `env_example.project_order`: the editor file's dependency order
  (`projects.dependency_order` over the folders sorted by name), so the first project reading a variable is the
  library that defines it. A variable is listed once, there, `also read by:` the others, each other wording of its
  description kept under it; the `not for .env` names stay out. `workspace init` seeds a missing `.env` with the
  workspace's `env.example`, else its `env.complete`, else the short template.
- `aisland workspace env-test` writes `tests/test_env_example.py` into a project from
  `templates/test_env_example.py.template`: standard library and pytest only (a project never imports aisland),
  regexes over the non-test `.py` files and the YAML under `config/`, `examples/`, `graphs/` with its comments left
  out (prose like "`$VAR` is expanded" reads nothing). `env.complete` must list every name read; `env.example` may
  hold only `env.complete`'s names. The template's output passes black, isort and flake8 at 120 columns unchanged,
  or every project's gates would rewrite it and `env-test` would never be up to date.

Rejected: one `aisland env check` scanning every project (a project's own test run would not catch its drift, and
the general projects cannot depend on aisland); committing the workspace's files (they follow what this machine has
checked out); choosing the short list automatically (the variables two projects read — a token only one project
reads would never be in it); editing `.env` (`--missing` prints, the person pastes).

## Consequences

- A name the code builds at run time (a prefix plus a key) is not seen by the test; the project lists it by hand.
- A project that discovery does not know (no installable `pyproject.toml`) comes after the others, in the workspace's
  listing order.
- The Bitbucket projects' generated test names aisland in its header, as their generated Jenkinsfiles already do.

## Example

```
$ aisland workspace env-example
==> Wrote env.complete (67 variables from 26 projects)
==> Wrote env.example (0 variables from 0 projects)
    no env.example: loggair, corelib, liquifai, …
```

## What you may change

The header text, the section layout, the folders the test reads. Do not let any line of a generated file set a
variable, and do not make the workspace's files committed or the command edit `.env`.

---

[The architecture and every decision](../architecture.md#the-decisions)
