# A project is the workspace's own by its GitHub remote: no organisation is written into aisland

*2026-10-26* [github-owner]

## Context

Three questions asked "is this one of ours?" by looking for `github.com/Gearlux/` in a URL: which projects CI
installs from GitHub (`discover_gearlux_projects` matched `.gitmodules` against the organisation), whether a PyPI
release is the project's own (`released_on_pypi`, `_pypi_provenance`, `wheels.published.pypi_state`) and which owner the CI's
GitHub App token is for. A workspace of another owner got CI files without the install lines its unreleased
dependencies need, a `pypi` source refused as "owned by a different project", and wheels shipped for packages PyPI
already serves. aisland was about to be released for such workspaces.

## Decision

The owner is read from each project's own remote (`github.project_repositories`: `.gitmodules`, or a clone's
`origin`; `github_repository` reads every spelling git accepts). A project is internal when that remote is on
github.com, and a runner installs it from `git+https://github.com/<owner>/<repository>.git@main`. A release is the
project's when its metadata names `github.com/<owner>/` (case-insensitive); a project off GitHub has no owner and
matches no release. The token step names the private dependencies' owner and its secrets `<OWNER>_APP_ID` /
`<OWNER>_APP_PRIVATE_KEY`; private dependencies of two owners are refused (one App token reaches one owner).

## Consequences

- For a workspace whose projects are all one owner's (the workspace aisland is developed in), every generated CI file is byte-identical: the 78
  files of `aisland jenkins scaffold --force` compared before and after.
- A submodule of another GitHub owner is now internal; before, only the one organisation's were.
- A detached project is internal only when its remote is on GitHub (before, every detached project was).

## Example

```python
>>> github_repository("git@github.com:acme/lib.git")
GitHubRepository(owner='acme', name='lib')
>>> released_on_pypi("lib", "acme")        # metadata names github.com/acme/… and a final version exists
True
```

## What you may change

- Another host (a GitLab owner) — a second parser beside `github_repository`, with its own install URL.
- Never: an organisation written back into aisland, or a release accepted without its owner's URL.

---

[The architecture and every decision](../architecture.md#the-decisions)
