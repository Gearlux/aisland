# A project names its click CLIs, and the package names nothing of the workspace it was written in (2026-10-28)

## Context

A finding of the release review's second pass: the package carried names of the workspace it was written in — a
completion row for one project's CLI, a fallback e-mail address, a private project and a company in comments, the
editor template's placeholder name, a setup note naming a log tool in workspaces that never had it. The completion
row could not simply be computed: importing each script to ask "is it click?" was measured first, 2 minutes in the
workspace aisland is developed in, and it still missed the one CLI it was for, whose `main` is a plain function
calling a click group.

## Decision

A project names its click scripts in `[tool.aisland] click-commands`, read in stage 1 of discovery
(`discovery._click_commands`) and refused naming the line when no `[project.scripts]` entry has the name; `aisland
completion install` gives them click's completion beside aisland's own row of `NON_LIQUIFAI_CLIS`. The editor
template's placeholder is `WORKSPACE`; the unused fallback address is gone; the private and company names left the
comments, pinned over every file of the package.

## Consequences

A workspace with a click CLI of its own declares it, or it gets no completion — the same opt-in as everything else a
project brings.

## Example

```toml
[project.scripts]
app = "app.cli:main"

[tool.aisland]
click-commands = ["app"]
```

## What you may change

- The refusal's wording, with `tests/test_completion.py::test_a_projects_declared_click_command_is_found_without_importing_it`.
- Never: importing a project's code to discover its CLI, a workspace's CLI named in aisland, or a private name in
  the package (`tests/test_no_private_names.py::test_no_file_of_the_package_names_a_private_project_or_a_company`).

---

[The architecture and every decision](../architecture.md#the-decisions)
