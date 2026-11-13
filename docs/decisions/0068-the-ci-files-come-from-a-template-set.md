# The CI files come from a template set (2026-10-30)

## Context

`aisland jenkins scaffold` wrote its three files from Python f-strings in one 1267-line function. A team whose CI
looks different — a shared library's steps, a Kubernetes pod agent, a private package index, credentials, only a
`Jenkinsfile` — could not get its pipeline from aisland without changing aisland, and the f-strings' doubled braces
and escaped backslashes made even aisland's own pipeline hard to read or change.

## Decision

The function works out the facts and renders a template set (chosen 2026-10-30): Jinja2 in the core install, the
built-in set in `aisland/templates/ci/`, a workspace's set chosen by `[ci] templates` — a folder of the workspace or
the name an installed package registers (`aisland.ci_templates`) — with variables the set declares. Delimiters
`<[ ]>`, `<% %>`, `<# #>`: Jinja's own braces collide with GitHub's `${{ }}`, and `[[`, first proposed, with Groovy's
lists of maps (`tools: [[parser: …]]`) in aisland's own Jenkinsfile. aisland writes the provenance header itself.

The built-in templates were produced from the f-strings' syntax trees, so no byte was retyped; rendered for every
project of the workspace aisland is developed in (PyPI, GitHub and Jenkins stubbed to fixed answers), the 81 files
were identical to the old output but for the header's "Source template" line.

## Consequences

`jenkins.py` lost 900 lines; aisland's pipeline is now read and changed as the files it writes. `jinja2` (with
`markupsafe`) joins every install. `[ci]` is a section of `workspace.toml`, kept on every rewrite. A set is refused
before anything is written: a missing template, a variable undeclared or unset, a fact nobody has (naming the template
and line).

## Example

```toml
[ci]
templates = "ci-templates"
[ci.variables]
index_url = "https://pypi.example.com/simple"
```

## What you may change

The facts, by adding one to `Facts` with its documentation; aisland's templates, held by the scaffold's pins. Never
give a template a way to run code or read files beyond its set.

---

[The architecture and every decision](../architecture.md#the-decisions)
