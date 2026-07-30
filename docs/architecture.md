# aisland — architecture decisions

Design rationale for aisland's mechanisms: *why* each exists, what it protects, and what a
change would ripple into. Usage lives in [`../README.md`](../README.md); the agent-facing
rulebook lives in [`../AGENTS.md`](../AGENTS.md). These three cross-link and never duplicate.

---

## Project sources are a two-layer registry, and only the committed layer reaches a committed file

*2026-07-28*

### Context

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

### Decision

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
| default | `<workspace>/project-sources.toml` | yes |
| override | `git config --local aisland.source.<project>` | no |

The layers are **not** interchangeable. Each artifact reads exactly one:

| artifact | reads | why |
|---|---|---|
| `Jenkinsfile`, `ci.yml` | committed, **published bit only** | tracked *and shared* — a dev's `ref` must not reach another consumer's CI |
| `Jenkinsfile.local` | **nothing** | resolves it at build time instead (see the guard below) |
| `*.code-workspace` `folders` | committed only | tracked, hand-maintained |
| submodule checkout, shared `.venv` | effective (override ?? committed) | untracked machine state — the whole point of an override |

The middle two rows are the 2026-07-28 revision. Reading the `ref` into a tracked file conflated
two different questions — *which commit do I depend on* (a shared fact) and *which commit am I
editing today* (a personal one) — so a routine `--ref` switch rewrote seventeen projects' CI. CI
now always clones `DEFAULT_REF` and reads exactly one bit: is this dep published? That is also
why `local` and `github` are indistinguishable to a runner — it never has a workspace checkout,
so the axis they differ on does not exist there.

A project with no entry in either layer is *unmanaged*, and `DEFAULT_REF = "main"` makes an
unmanaged project and an explicit `local:main` produce byte-identical build files — so
introducing the mechanism changed no behaviour anywhere until a source was actually set.

### Consequences

- **The build-time guard is the mechanism, not defensive polish.** It asks at *build* time exactly
  the question the configuration answers — "does this machine have a checkout?" — so
  `Jenkinsfile.local` needs no configuration at all: one identical line per dep, editable if the
  checkout is there, `git@main` otherwise. Deciding it at build time rather than scaffold time is
  also what lets a per-machine override work, since by construction it never reaches this tracked
  file. Its accepted cost is that a `pypi`-sourced dep is built from `main` here rather than from
  the released wheel — the local pipeline exists to catch breakage *before* a release, so that is
  the useful direction to err in.
- **The guard tests `pyproject.toml`, not the directory.** `git submodule deinit` empties the
  working tree but leaves the directory itself, so a `[ -d ]` predicate answers "yes, a checkout"
  for every detached dep — the editable branch then runs against an empty directory, `uv` reports
  *"does not appear to be a Python project"*, and the git fallback that exists for exactly this
  case is unreachable. Testing the file the editable install actually consumes is what makes the
  predicate agree with the question. The wider lesson: when a guard stands in for a configuration
  value, its predicate has to be *equivalent* to that value, not merely correlated with it — and
  the way to establish that is to execute the emitted shell against the real on-disk state, which
  an assertion on the emitted string can never do.
- **Order of operations in a `pypi` switch is fixed by information loss.** The distribution name
  (`aisland` → `aisland-manager`) can only be read from `pyproject.toml`, which the deinit
  removes — so it is resolved first and then *persisted* into the committed registry.
- **Provenance, not existence, is what gets validated.** PyPI names are global, so a workspace
  project can share a name with an unrelated package; checking only that the name resolves would
  install a stranger's code into the shared venv. The workspace convention that every pyproject
  declares a `github.com/Gearlux/…` repository URL makes the published metadata a usable marker,
  so `_pypi_provenance` requires it. `github` gets the same treatment one level down:
  `_remote_ref_exists` refuses a ref the remote does not have. Both probes are *inconclusive*
  rather than rejecting when they cannot reach the network — an offline machine must not be
  blocked by a check it cannot perform.
- **Detaching a project hides it from discovery, which the scaffolder had to be taught.**
  `discover_gearlux_projects` learns each project's distribution name by reading its
  `pyproject.toml`; an emptied directory has none. For `pypi` the resulting drop was accidentally
  correct (the dep is meant to leave the git-URL loop), which is exactly why the gap survived
  until `github` existed — there it erases the pin the kind exists to express, and CI silently
  installs the PyPI release instead of the requested ref. The registry's `distribution` field,
  recorded for the deinit case anyway, is the fallback.
- **Nothing is ever forced, and a failed apply rolls back.** `git switch` and `git submodule
  deinit` run without `-f`; a dirty submodule refuses, is reported, and the venv is left
  untouched — matching `reconcile_submodules`' protective deinit rather than inventing a second
  policy. `set_project_source` then restores the previous registry and override, because a
  registry claiming a state the working tree does not have would be re-attempted by every
  subsequent `aisland setup`.
- **Two mechanisms could fight over one submodule**, so they don't: `reconcile_submodules` folds
  `sources.detached_projects()` — BOTH detaching kinds, not just `pypi` — into its active set,
  otherwise the next `aisland setup` would re-init a directory the source says is empty.
- **Almost no source change re-scaffolds anything.** Since CI reads one bit,
  `_publishedness_changed` gates the regeneration on that bit flipping: a `--ref` switch or a
  `local` <-> `github` move rewrites nothing. Crossing the `pypi` boundary still re-scaffolds
  dependents with `--force`, which also absorbs any unrelated template drift those files had
  accumulated — a real, visible side effect; `--no-scaffold` exists for when it is unwanted.
- **PyPI availability is decided once, not on every scaffold.** `aisland source set … pypi`
  probes the index; the scaffolder never does. A scaffold-time probe would make the content of
  *committed* files depend on network reachability, so an offline run would flip every project to
  git URLs and churn the tree.
- Discovery still does the heavy lifting: a `pypi`-sourced project's directory is empty, so
  `discover_projects()` drops it from the editable-install loop with no special-casing.

### Example

```bash
$ aisland source set loggair github --ref dev/main   # from local:main — no CI touched
==> loggair: committed source set to github:dev/main in project-sources.toml
==> loggair: github @ dev/main (git+https://github.com/Gearlux/loggair.git@dev/main)
==> Updated acme.code-workspace folders (20 enabled)

$ aisland source set loggair pypi                    # crosses the boundary — CI regenerates
==> loggair: committed source set to pypi in project-sources.toml
==> Regenerating CI files for 17 dependent project(s): annotaide, chainwind, confluid, …
```

```python
# The scaffolder reads the COMMITTED layer only — never `effective_source` — and only one bit
# of it. `Jenkinsfile.local`'s loop calls none of this: it emits the same guarded line for every
# dep, so the file cannot depend on configuration at all.
committed_sources = read_committed_sources(workspace_root)

def _dep_is_pypi(dep: str) -> bool:
    """Published -> drop it from the pre-install loop; `.[dev]` resolves it from the index."""
    source = source_for_dependency(committed_sources, dep)
    return source is not None and source.kind == "pypi"
```

### What you may change

- Adding a source kind means extending the `SourceKind` `Literal`; everything that enumerates
  kinds (the click `Choice`, the docs table) derives from it via `get_args` — do not restate the
  set anywhere.
- The registry schema is additive: a new per-project key is fine, but keep it a flat table of
  string scalars so `write_committed_sources` needs no TOML-writer dependency.
- A new kind must decide two things before anything else: does it carry a `ref` (`REF_KINDS`) and
  does it leave a working tree (`DETACHED_KINDS`). Every call site branches on those, not on the
  kind name.
- What must not change: the layer→artifact mapping above; that CI reads the published bit and
  never the `ref`; and that `Jenkinsfile.local` reads nothing at all. Reintroducing per-kind
  branching in that file would quietly restore the churn this record exists to remove.

---

## The `*.code-workspace` `folders` list is edited line-wise, and disabled entries are commented out

*2026-07-28*

### Context

Disabling a project has to remove it from the editor's folder list. The natural implementation —
parse the workspace file as JSON, drop the element, re-serialise — fails twice over. First, the
file is **JSONC**, not JSON: `settings` carries `//` comments and trailing commas, and
`json.load` raises on the real `acme.code-workspace` at line 121. Second, even a
comment-preserving round trip would reformat a hand-maintained tracked file and, worse, would
lose the entry's *position* — the list is dependency-ordered, not alphabetical, so a removed
entry could not be put back where it came from.

### Decision

Scan out only the `folders` span (a small bracket scanner that skips strings and `//` comments),
then treat it as **lines**. A disabled entry is commented out in place, preceded by a marker line
naming the command that restores it. Re-enabling strips the marker and the `//` prefixes.

Commenting preserves each line verbatim after the marker, so comment → uncomment is
byte-for-byte lossless: flipping a project to `pypi` and back leaves `git diff` clean. Trailing
commas are normalised across the *enabled* entries only, which is itself idempotent — the file
already satisfies the invariant, so a no-op sync writes nothing.

### Consequences

- The mechanism only touches entries for projects named in `project-sources.toml`. `vendorcli`,
  `signals-bridge` and `raidar-ultralytics` are governed by submodule activation, not this,
  and are left exactly as they are.
- A managed project with no entry at all is *inserted*, before any out-of-tree entry (`../logs`)
  so those stay last.
- The scanner is deliberately narrow. It understands the `folders` array and nothing else, which
  is why the rest of the JSONC document survives untouched.

### Example

```jsonc
    "folders": [
        { "name": "aisland", "path": "aisland" },
        // aisland source: 'loggair' is consumed as a package (no checkout); restore it with `aisland source set loggair local`.
        // {
        //     "name": "loggair",
        //     "path": "loggair"
        // },
        { "name": "confluid", "path": "confluid" },
    ],
```

### What you may change

- The marker text is free-form; `_MARKER_RE` is what detection depends on, so change both together.
- Insertion position policy (currently "before out-of-tree paths") is a judgement call and safe to revisit.
- What must not change: never replace this with a JSON parse + re-serialise. It would strip every
  comment from a tracked, hand-maintained file and destroy the lossless round trip.
