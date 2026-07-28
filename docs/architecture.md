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

A source is `git` (checked out at a `ref`, installed editable) or `pypi` (submodule deinit'd,
directory empty, published wheel installed) — a closed `Literal`, so "GitHub main" and "GitHub
dev/main" are one kind at two refs rather than two modes. Configuration is **two layers**,
mirroring the existing `.gitmodules active` + `.git/config submodule.<name>.active` split:

| layer | storage | committed |
|---|---|---|
| default | `<workspace>/project-sources.toml` | yes |
| override | `git config --local aisland.source.<project>` | no |

The layers are **not** interchangeable. Each artifact reads exactly one:

| artifact | reads | why |
|---|---|---|
| `Jenkinsfile`, `Jenkinsfile.local`, `ci.yml` | committed only | tracked — an override would churn them per machine |
| `*.code-workspace` `folders` | committed only | tracked, hand-maintained |
| submodule checkout, shared `.venv` | effective (override ?? committed) | untracked machine state — the whole point of an override |

A project with no entry in either layer is *unmanaged*, and `DEFAULT_REF = "main"` makes an
unmanaged project and an explicit `git:main` produce byte-identical build files — so introducing
the mechanism changed no behaviour anywhere until a source was actually set.

### Consequences

- **The `[ -d ]` guard is load-bearing, not defensive polish.** An override can empty a directory
  that the *committed* `Jenkinsfile.local` still names, so the generated editable-install line
  must probe for the directory and fall back to the committed git ref. Without it, a per-machine
  `pypi` override silently breaks the local Jenkins pipeline.
- **Order of operations in a `pypi` switch is fixed by information loss.** The distribution name
  (`aisland` → `aisland-manager`) can only be read from `pyproject.toml`, which the deinit
  removes — so it is resolved first and then *persisted* into the committed registry.
- **Provenance, not existence, is what gets validated.** PyPI names are global, so a workspace
  project can share a name with an unrelated package; checking only that the name resolves would
  install a stranger's code into the shared venv. The workspace convention that every pyproject
  declares a `github.com/Gearlux/…` repository URL makes the published metadata a usable marker,
  so `_pypi_provenance` requires it. An unreachable index is *inconclusive*, not a rejection —
  an offline machine must not be blocked by a check it cannot perform.
- **Nothing is ever forced, and a failed apply rolls back.** `git switch` and `git submodule
  deinit` run without `-f`; a dirty submodule refuses, is reported, and the venv is left
  untouched — matching `reconcile_submodules`' protective deinit rather than inventing a second
  policy. `set_project_source` then restores the previous registry and override, because a
  registry claiming a state the working tree does not have would be re-attempted by every
  subsequent `aisland setup`.
- **Two mechanisms could fight over one submodule**, so they don't: `reconcile_submodules` folds
  `sources.pypi_sourced_projects()` into its active set, otherwise the next `aisland setup` would
  re-init a directory the source says is empty.
- **A source change re-scaffolds dependents with `--force`**, which also absorbs any unrelated
  template drift those tracked files had accumulated. That is a real, visible side effect;
  `--no-scaffold` exists for when it is unwanted.
- Discovery still does the heavy lifting: a `pypi`-sourced project's directory is empty, so
  `discover_projects()` drops it from the editable-install loop with no special-casing.

### Example

```bash
$ aisland source set loggair pypi
==> loggair: committed source set to pypi in project-sources.toml
==> loggair: pypi (loggair)
==> Updated acme.code-workspace folders (22 enabled)
==> Regenerating CI files for 17 dependent project(s): annotaide, chainwind, confluid, …
```

```python
# The scaffolder reads the COMMITTED layer only — never `effective_source`.
committed_sources = read_committed_sources(workspace_root)

def _dep_is_pypi(dep: str) -> bool:
    source = source_for_dependency(committed_sources, dep)
    return source is not None and source.kind == "pypi"
```

### What you may change

- Adding a source kind means extending the `SourceKind` `Literal`; everything that enumerates
  kinds (the click `Choice`, the docs table) derives from it via `get_args` — do not restate the
  set anywhere.
- The registry schema is additive: a new per-project key is fine, but keep it a flat table of
  string scalars so `write_committed_sources` needs no TOML-writer dependency.
- What must not change: the layer→artifact mapping above, and the `[ -d ]` guard that makes it
  safe.

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
        // aisland source: 'loggair' is consumed from PyPI; restore it with `aisland source set loggair git`.
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
