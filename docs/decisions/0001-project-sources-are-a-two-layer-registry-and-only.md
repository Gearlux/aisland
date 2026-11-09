# Project sources are a two-layer registry, and only the committed layer reaches a committed file

*2026-07-28; the layers' storage rewritten for `workspace.toml` 2026-10-22* [sources]

## Context

Every workspace project was consumed exactly one way, hardcoded in three unrelated places: the
editable-install loop in `manager.setup_workspace()`, the literal `@main` in the git URLs that
`jenkins.scaffold_pipelines()` writes into each consumer's `Jenkinsfile` / `ci.yml`, and the
hand-maintained `folders` list in `acme.code-workspace`. Once a project is published to PyPI
*and* carries more than one interesting branch, "which build of it am I depending on?" becomes a
real question — and answering it meant hand-editing four kinds of file and hoping they agreed.

The obvious design — one setting per project — runs into a constraint that is easy to miss: the
artifacts that must react to the setting are almost all **tracked files**. A per-machine choice
that rewrote them would make `git status` dirty differently on every machine; a shared choice
that could not be deviated from locally would stop one machine from developing a project the
other consumes.

## Decision

A source is one of three kinds — a closed `Literal` — along two independent axes:

| kind | working tree | pinned by |
|---|---|---|
| `local` | checked out at `ref` | a branch/tag you can edit |
| `github` | none (deinit'd) | a branch/tag on the remote |
| `pypi` | none (deinit'd) | a release version |

`local` and `github` are the same pin at different levels of commitment: "I develop this here"
vs "I depend on that branch". Naming them `git` and `github` was rejected — the two commands
would sit three characters apart while one keeps a checkout and the other deletes it. The
derived predicates (`REF_KINDS`, `DETACHED_KINDS`, `ProjectSource.detached`) exist so no call
site re-tests kinds by hand and a fourth kind cannot silently miss a branch.

Configuration is **two layers**, mirroring the existing `.gitmodules active` +
`.git/config submodule.<name>.active` split:

| layer | storage | committed |
|---|---|---|
| default | the project's table in `<workspace>/workspace.toml`: `source`, `version`, and `distribution` only where it differs from the name; a git source's ref is the project's own `branch` | yes |
| override | `git config --local aisland.source.<project>.source\|branch\|version` | no |

Both are set with `aisland workspace configure source set|unset [--machine-only]`. Until 2026-10-22 the default layer
was the `[sources.<project>]` tables of a separate `config.toml` and the override a single key, `aisland.source.<project>`;
they moved into the project's table so that a project's branch and name were no longer written twice (record
"`aisland workspace configure`: one set of commands built from each plugin's keys").

The layers are **not** interchangeable. Each artifact reads exactly one:

| artifact | reads | why |
|---|---|---|
| `Jenkinsfile`, `ci.yml` | **neither layer** — asks PyPI "is this dep released?" (2026-09-26) | tracked *and shared* — a dev's source or `ref` must not reach another consumer's CI |
| `Jenkinsfile.local` | **nothing** | installs nothing — it tests the `.venv`, where the effective source was already applied |
| `*.code-workspace` `folders` | effective, through the checkout (2026-10-18) | generated per machine, gitignored |
| submodule checkout, shared `.venv` | effective (override ?? committed) | untracked machine state — the whole point of an override |

The middle two rows are the 2026-07-28 revision. Reading the `ref` into a tracked file conflated
two different questions — *which commit do I depend on* (a shared fact) and *which commit am I
editing today* (a personal one) — so a routine `--ref` switch rewrote seventeen projects' CI. CI
now always clones `DEFAULT_REF` and reads exactly one bit: is this dep published? That is also
why `local` and `github` are indistinguishable to a runner — it never has a workspace checkout,
so the axis they differ on does not exist there.

**Revision 2026-09-26 — the bit comes from PyPI, not from the registry.** The one bit used to be
"is this dep `pypi`-SOURCED?", which is a statement about one machine's checkout, not about the
dependency. Two failures followed from reading it: switching corelib to `local` to fix bugs made
every consumer's committed CI install corelib from GitHub `main` (and switching back rewrote
them again — sixteen projects, twice, in one afternoon), and liquifai, released on PyPI but
developed `local`, was built from GitHub `main` by every consumer. The user's ruling: CI "should
always point to released packages, and to GitHub for non-released ones". So the scaffolder asks
`pypi.released_on_pypi(dep, owner)` — a FINAL, non-yanked version whose metadata points at
`github.com/<owner>/`, the owner of the dependency's own remote (an organisation written into aisland until
2026-10-26, record "A project is the workspace's own by its GitHub remote") — and `aisland workspace configure
source set|unset` regenerate nothing at all. The earlier
objection to a scaffold-time probe (an offline run would flip every project to git URLs) is met
by refusing instead of guessing: an unreachable PyPI raises `ReleaseProbeError` and the project's
files are not written. Rejected alternative: a hand-kept "released" list in the committed configuration —
offline, but it has to be remembered at every first release, and forgetting it silently keeps a
released dependency on GitHub.

A project with no entry in either layer is *unmanaged*, and `DEFAULT_REF = "main"` makes an
unmanaged project and an explicit `local:main` produce byte-identical build files — so
introducing the mechanism changed no behaviour anywhere until a source was actually set.

## Consequences

- **`Jenkinsfile.local` needs no source configuration at all**, because it installs nothing: the
  effective source of every dep was applied to the `.venv` by `aisland setup`, and the local
  pipeline tests that venv as it is (see [the local pipeline tests the venv, it does not
  install](0012-the-local-pipeline-tests-the-venv-it-does-not-install.md)). A per-machine override is
  therefore honoured there for free, and no source change of any kind can rewrite the file.
- **Order of operations in a `pypi` switch is fixed by information loss.** The distribution name
  (`acme` → `acme-tools`) can only be read from `pyproject.toml`, which the deinit
  removes — so it is resolved first and then *persisted* into the committed registry.
- **Provenance, not existence, is what gets validated.** PyPI names are global, so a workspace
  project can share a name with an unrelated package; checking only that the name resolves would
  install a stranger's code into the shared venv. The workspace convention that every pyproject
  declares its `github.com/<owner>/…` repository URL makes the published metadata a usable marker,
  so `_pypi_provenance` requires the owner of the project's own remote. `github` gets the same treatment one level down:
  `_remote_ref_exists` refuses a ref the remote does not have. Both probes are *inconclusive*
  rather than rejecting when they cannot reach the network — an offline machine must not be
  blocked by a check it cannot perform.
- **Detaching a project hides it from discovery, which the scaffolder had to be taught.**
  The scaffolder's project list (`ci_scaffold.discover_github_projects`) learns each project's distribution name by
  reading its `pyproject.toml`; an emptied directory has none. For `pypi` the resulting drop was accidentally
  correct (the dep is meant to leave the git-URL loop), which is exactly why the gap survived
  until `github` existed — there it erases the pin the kind exists to express, and CI silently
  installs the PyPI release instead of the requested ref. The committed source's `distribution` field,
  recorded for the deinit case anyway, is the fallback.
- **Nothing is ever forced, and a failed apply rolls back.** `git switch` and `git submodule
  deinit` run without `-f`; a dirty submodule refuses, is reported, and the venv is left
  untouched — matching `activation.reconcile`'s protective deinit rather than inventing a second
  policy. `set_project_source` then puts `workspace.toml` back byte for byte and restores the override, because a
  committed source claiming a state the working tree does not have would be re-attempted by every
  subsequent `aisland setup`.
- **Two mechanisms could fight over one submodule**, so they don't: `activation.reconcile` folds
  `sources.detached_projects()` — BOTH detaching kinds, not just `pypi` — into its active set,
  otherwise the next `aisland setup` would re-init a directory the source says is empty.
- **No source change re-scaffolds anything** (2026-09-26). CI reads no source, so `configure source set`
  and `unset` have nothing to regenerate; what changes a CI file is a dependency's FIRST RELEASE,
  picked up by the next `aisland jenkins scaffold`.
- **Generating CI needs PyPI** (2026-09-26). `released_on_pypi` is asked once per distribution
  per process (cached) and raises when the index cannot answer — committed file content must never
  depend on whether the network happened to be up, so the run stops rather than guessing.
- **A consumer builds against the RELEASE of a released dependency, never its `main`.** Using
  something a released dependency has only on `main` fails that consumer's CI until the release —
  the accepted cost of the ruling (measured on day one: canvas requires `liquifai>=0.3.0`,
  and 0.3.0 exists only on liquifai's `dev/main`).
- Discovery still does the heavy lifting: a `pypi`-sourced project's directory is empty, so
  `discover_projects()` drops it from the editable-install loop with no special-casing.

## Example

```bash
$ aisland workspace configure source set models github --branch dev/main   # from local:main — no CI touched
==> models: committed source set to github:dev/main in workspace.toml
==> models: github @ dev/main (git+https://github.com/acme/models.git@dev/main)

$ aisland workspace configure source set models pypi                       # no CI touched either
==> models: committed source set to pypi in workspace.toml
```

```toml
# workspace.toml — the committed layer is the project's own table
[project."models"]
url = "git@github.com:acme/models.git"
branch = "dev/main"
source = "github"
```

```python
# The scaffolder reads NO source — it asks PyPI, per internal dependency, for the runner files.
# `Jenkinsfile.local` calls none of this: it installs nothing, so it depends on nothing.
def _dep_is_released(dep: str) -> bool:
    return released_on_pypi(dep, github_projects[dep].owner)   # released -> `.[dev]` resolves it; else git+…@main
```

## What you may change

- Adding a source kind means extending the `SourceKind` `Literal`; everything that enumerates
  kinds (the click `Choice`, the docs table) derives from it via `get_args` — do not restate the
  set anywhere.
- The committed keys are additive: a new per-project key is fine, as a string scalar in the project's
  table, written through `workspace.file.set_settings` (as `write_committed_sources` does) and listed in
  `sources.COMMITTED_KEYS`, so `workspace_check` does not name it as unknown.
- A new kind must decide two things before anything else: does it carry a `ref` (`REF_KINDS`) and
  does it leave a working tree (`DETACHED_KINDS`). Every call site branches on those, not on the
  kind name.
- What must not change: the layer→artifact mapping above; that CI reads no source and no `ref`
  (only whether PyPI has a release); that an unreachable PyPI stops the scaffold rather than
  guessing; and that `Jenkinsfile.local` reads nothing at all. Reintroducing per-kind
  branching — or any install — in that file would quietly restore the churn this record exists
  to remove.
- The `local` kind's editable install has ONE spelling, `selection.editable_target` (the `dev`
  extra plus the selected extras on both axes), shared with `setup_workspace` (2026-08-22). Do
  not inline a bare `uv pip install -e <dir>` at a new call site: that is how setting a source to
  `local` once dropped every extra `setup` had just installed, and the helper exists so the two
  installs cannot disagree again.

---

[The architecture and every decision](../architecture.md#the-decisions)
