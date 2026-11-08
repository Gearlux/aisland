# A project off GitHub never names one in its CI files (2026-10-28)

## Context

`aisland jenkins scaffold` installs a dependency that is not released on PyPI from its GitHub repository
(`uv pip install --no-deps git+https://github.com/<owner>/<dep>.git@main`). For a project whose own remote is a
company's Bitbucket server that wrote the workspace's GitHub owner and project names into the company's repository —
the very thing such a repository must not reveal. Two such projects had these lines committed: 16 in one `ci.yml`.

## Decision

For a project off GitHub the unreleased dependencies come from the wheels it commits: the extra
`aisland wheels build --extra <name>` writes (its block is found by `wheels.extra.remembered_extra`) joins the install
line, and no git-URL line is written. A dependency no wheel of that extra holds, or a project with no such extra, is
refused before any file is written, naming the dependencies and the command that ships them. A released dependency
resolves from PyPI as before.

## Consequences

A project off GitHub rebuilds its wheels when a new unreleased workspace project joins its dependencies, or its next
scaffold refuses. Its CI installs the wheels extra on every run.

## Example

```text
$ aisland jenkins scaffold tools --force
Error: tools does not live on GitHub, so its CI files never name a project that does, and lib is not released on PyPI.
Ship it as wheels with the project — aisland wheels build tools --extra wheels --commit (the extra listed in
[tool.aisland] manual-extras) — then scaffold again.
```

## What you may change

- The wording of the refusal.
- Never: a GitHub URL, owner or project name in a project off GitHub, as an install line or a comment.

---

[The architecture and every decision](../architecture.md#the-decisions)
