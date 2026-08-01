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
| default | the `[sources.<project>]` tables of `<workspace>/config.toml` | yes |
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
==> loggair: committed source set to github:dev/main in config.toml
==> loggair: github @ dev/main (git+https://github.com/Gearlux/loggair.git@dev/main)
==> Updated acme.code-workspace folders (20 enabled)

$ aisland source set loggair pypi                    # crosses the boundary — CI regenerates
==> loggair: committed source set to pypi in config.toml
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

- The mechanism only touches entries for projects named in `config.toml`'s `[sources]`. `vendorcli`,
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

---

## Framework selection is a second two-layer registry, and CI reads only the committed layer (2026-07-30)

### Context

The workspace's packages were made framework-neutral: each keeps a numpy/config core and puts
its ML framework behind an extra — `recordstream[torch]`, `sonair[lightning]` / `[keras]`,
`marainer[torch,lightning,keras,tensorflow]`. That is what makes "install only what I work with"
*expressible*, but nothing yet *decided* it. `aisland setup`'s install line was:

```python
target = f"{project_dir}[dev]" if project["has_dev"] else str(project_dir)
```

so immediately after the extras split, a fresh setup produced a sonair with no training backend
at all. The decision had to live somewhere, and the obvious somewhere — "just install every
extra" — is wrong twice over: sonair declares `jax` / `mlx` / `tensorflow` extras that exist only
so its CLI templates can print a real install command, and `dev` / `http` / `viz` / `notebook` /
`all` sit in the same table as `torch` and `keras`, so no name-based rule can tell a training
engine from a documentation extra.

### Decision

**Split it in two: a project declares, the workspace selects.**

1. **Declaration** lives in the project's own `pyproject.toml`, because only the project knows
   which of its extras are frameworks and which are implemented:

   ```toml
   [tool.aisland]
   frameworks = ["lightning", "keras"]
   ```

   A project with no declaration is *inert* — it installs exactly as it always did, which is what
   keeps this change invisible for the fifteen-odd projects that ship no framework extra.

2. **Selection** is a list of framework NAMES, and each project installs what it declares ∩ what
   is selected. The names line up across projects deliberately (`keras` is `keras` everywhere),
   so one selection drives everything with no per-project mapping table.

3. **Two configuration layers, mirroring `aisland/sources.py`**: the committed `frameworks`
   key of `config.toml` and a per-machine `git config --local aisland.frameworks`. The
   committed layer is the shared default AND the only layer the CI scaffolder reads.

4. **`None` is not `[]`.** An absent selection means *nobody has chosen* and `aisland setup`
   prompts, recording the answer; a recorded empty selection means *no frameworks, I mean it* and
   is honoured silently.

### Consequences

- **A local narrowing can never reach a runner.** This is the invariant, not a nicety: if one
  developer's "I only use torch" reached the generated `Jenkinsfile` / `ci.yml`, every runner
  would stop installing Keras and sonair's Keras suite would pass by not running. The failure
  presents as a green CI that no longer checks anything, so `scaffold_pipelines` computes
  `ci_extras` from `read_committed_frameworks` and never from `effective_frameworks`.
- **CI installs the union, and that is affordable.** Measured 2026-07-30: torch 2.13 +
  tensorflow 2.20 + keras 3.15 + pytorch-lightning 2.6.5 co-resolve on one numpy. Committing
  everything and letting each machine narrow is therefore a real option, not a hope.
- **Reinstall order is load-bearing.** `apply_frameworks` iterates `get_install_order`, not
  `discover_projects`: a consumer's extra can name a sibling's (`sonair[keras]` requires
  `marainer[keras]>=0.1.0`), and installing the consumer first resolves against the sibling's
  already-installed metadata — uv then warns `does not have an extra named 'keras'` and installs
  without it. Found on the first real run of the function, not in review.
- **Deselecting does not uninstall.** uv has no notion of "the packages that belonged to this
  extra" — they are ordinary dependencies, possibly shared — so dropping `keras` leaves the wheel
  behind. Accepted: a stale wheel costs disk, not correctness.
- **`--non-interactive` selects everything** when nothing is recorded. A headless setup must
  neither hang on the prompt nor hand back a workspace that cannot train.
- **Scope is deliberately limited to projects that OFFER a choice** (decided 2026-07-30 after
  reading each project, not counting its imports). `recordstream`, `marainer` and `sonair` declare
  frameworks because a genuine alternative exists. Four others keep `torch` as a plain hard
  dependency and are NOT declarable:

  | project | why it requires torch |
  | --- | --- |
  | `signals` | `schema.py` — "THE builder for the record model" — enforces `torch.complex64` IQ. Torch is the canonical representation, not a peripheral op. |
  | `deltaid` | its own docstring: "a standard PyTorch-Lightning semantic segmenter". |
  | `raidar`, `raidar-ultralytics` | torch/ultralytics detection trainers; `__init__.py` imports them eagerly. |

  Moving their deps behind a `[torch]` extra was considered and REJECTED: `pip install raidar`
  would then yield a package whose `__init__` raises `ImportError`, which is dishonest packaging
  for a project that genuinely needs torch. The consequence to accept is that selecting `keras` on
  a FULL workspace still installs torch — the selection narrows multi-backend projects, and does
  not promise a framework-free venv. (`torpaido` was a third case and is fixed rather than
  excepted: it declared `torch` while referencing it *zero* times, so the dependency was simply
  dropped.) A future mechanism for "skip this project unless its framework is selected" is the
  path if a truly torch-free venv is ever wanted; it is not this one.

### Example

```python
# the whole rule, at the one place setup installs a project
extras = extras_for(project["name"],
                    declared_frameworks(project_dir / "pyproject.toml"),  # ["lightning","keras"]
                    selected_frameworks)                                  # ["torch","keras"]
target = install_target(project_dir, project["has_dev"], extras)          # /w/sonair[dev,keras]
```

```bash
$ aisland framework set torch lightning keras
==> Recorded as `frameworks` in config.toml: torch, lightning, keras
==> Installing recordstream (/w/recordstream[dev,torch])...
==> Installing marainer (/w/marainer[dev,torch,lightning,keras])...
==> Installing sonair (/w/sonair[dev,lightning,keras])...
```

### What you may change (and where it's documented)

- **A new framework**: add the extra to the project, name it in that project's
  `[tool.aisland] frameworks`, and it appears in `aisland framework list` automatically. No
  aisland edit — the vocabulary is derived from the declarations.
- **Which frameworks CI installs**: `aisland framework set …` (committed layer), then re-scaffold.
  Do NOT hand-edit a generated `Jenkinsfile` / `ci.yml` to change extras — the next scaffold
  reverts it.
- **A per-machine deviation**: `aisland framework set … --machine-only`. What must not change is
  that this layer stays out of every tracked file.
- **The prompt's default** (currently blank = all) is a judgement call and safe to revisit; the
  unset-vs-empty distinction underneath it is not.
- **Usage** lives in the README's "ML Frameworks" section; per-project declarations are commented
  in each `pyproject.toml`.

## Completion install is a command, not a section of the bootstrap script (2026-07-31)

**Context.** Shell completion for the workspace's CLIs was installed by ~70 lines
of inline shell in `setup.sh` — sections 5 (liquifai apps), 5a (prune stale
blocks) and 5b (click + argparse apps). It worked. It was also, structurally,
write-only: the only way to run it was to run `setup.sh`, which also provisions
the venv, runs `aisland setup`, and rewrites the git config.

Nobody runs a full provisioning script to refresh a tab-completion. So when 5b
was added on 2026-07-27 to close a real gap (`aisland` and `streamstudio` had no
completion at all), it did not reach this workspace's own machine. The generated
`.project.bashrc.completion` kept its 2026-07-02 content, `aisland <TAB>` kept
doing nothing, and there was no signal anywhere — every *other* app completed
fine, because their blocks had been written back when the file was generated.
The bug was found a month later by a user asking why completion did not work.

Two properties of the old shape caused that, and neither is about shell:

1. **Cost of running was unrelated to cost of the work.** Refreshing an rc
   fragment is a sub-second job behind a multi-minute, side-effectful one.
2. **Nothing could test it.** Logic living in a bootstrap script that provisions
   a machine is logic no unit test will ever execute.

**Decision.** The install is `aisland/aisland/completion.py`, exposed as
`aisland completion install`. `setup.sh` calls it in one line. The three CLI
families stay exactly as they were — that taxonomy was right — but they are now
data (`NON_LIQUIFAI_CLIS`) plus rendering, in a module aisland's own tests
import. liquifai's installer is called through its Python API rather than the
`liquifai-install-completions` console script, so a PATH miss cannot silently
skip a whole family.

Two rules the implementation encodes, each protecting against a failure that
destroys the file rather than merely failing:

- **The pruner stands down when discovery finds nothing.** Dropping blocks for
  programs that no longer exist is required (liquifai merges by marker and leaves
  foreign blocks forever). But "no liquifai app found" means discovery broke, not
  that every app was uninstalled — pruning on that signal empties the file. The
  shell version guarded this with `command -v liquifai`; here it is an explicit
  `if installed` on the discovery result.
- **Program lookup is venv-first** (`sys.prefix/bin`, then `PATH`). A `PATH`-only
  check makes every app look missing when the command runs from an unactivated
  venv — which is precisely when someone would run it to repair their setup.

**Consequences.**

- Adding a project is `aisland completion install`, not a re-provision.
- `--target-rc` makes the destination a choice: the workspace fragment that
  `project.bashrc` sources (default), or the user's own `~/.bashrc`.
- The command reports what it installed, what it pruned, and whether anything
  sources the file yet — the signal whose absence hid the original bug. It also
  smoke-checks that the click app actually emits a script, since the installed
  block is an `eval` of that output and an app emitting nothing installs an inert
  block.
- The cost is liquifai's app discovery, which probes every candidate binary that
  has not declared the `liquifai.apps` entry point (~36s on this workspace). That
  is per-invocation now rather than per-provision; the fix is for apps to declare
  the entry point, not for aisland to cache a guess.

**Example.**

```bash
aisland completion install                       # workspace fragment (default)
aisland completion install --target-rc ~/.bashrc # or your own rc
```

```python
# setup.sh's whole share of the job:
#   "$VENV_DIR/bin/aisland" completion install
NON_LIQUIFAI_CLIS = (NonLiquifaiCli("aisland", "click"), NonLiquifaiCli("streamstudio", "argcomplete"))
```

**What you may change.** Add a CLI as a row in `NON_LIQUIFAI_CLIS`. A fourth
completion *mechanism* means a new `CliKind` and its renderer. Do not move any of
this back into `setup.sh`, and do not let it grow a second copy there — the
duplicate would rot exactly as the original did.

---

## The workspace's committed configuration is ONE file, and `uv.toml` cannot join it

*2026-08-01*

### Context

Three mechanisms grew their own workspace-root file, each with its own provenance header,
its own reader and its own writer: `sources.toml` (`aisland source`), `frameworks.toml` and
`trackers.toml` (`aisland framework` / `aisland tracker`). They are the same KIND of thing —
a committed, shared decision whose per-machine deviation lives in `.git/config` and never
reaches a tracked file — and the resemblance was already load-bearing: `selection.py` was
written by generalising `sources.py`, and every rule in one record repeats in the other.

Four `*.toml` at the workspace root also read as four things to configure, when only three
of them were configuration at all. The fourth, `uv.toml`, is *generated* per machine and
gitignored.

### Decision

`aisland/config.py` owns ONE committed file, `<workspace>/config.toml`, holding a top-level
key per concern: `frameworks`, `trackers`, and a `[sources.<project>]` table per managed
project. It owns the FILE — layout, comments, reading, rewriting — and none of the DATA:
`selection.py` and `sources.py` convert their own types to plain TOML values and call
`config.update`, which is why `config.py` imports nothing from aisland and cannot be part of
an import cycle with its two callers.

Four properties hold it together:

1. **Every write is whole-file, keyed.** `update(root, key, value)` loads the document,
   replaces one top-level key and re-renders, so `aisland tracker set` cannot disturb the
   `[sources.*]` tables. The cost, stated in the file's own header: a hand-edited file is
   normalised (and its comments dropped) by the next command that writes.
2. **Scalars are emitted before tables.** TOML binds a bare key to the most recent table
   header, so `frameworks = [...]` written after `[sources.confluid]` would silently become
   `sources.confluid.frameworks`. The renderer partitions on this, not on taste.
3. **An absent KEY is what *unset* means** — the distinction `resolve_for_setup` prompts on.
   It used to be an absent FILE, which three concerns can no longer share. An empty document
   still removes the file, so a workspace that has configured nothing carries no stub.
4. **The legacy files are still read**, per key, and deleted by the first write (or by
   `aisland config migrate`, which `aisland setup` calls). Without the fallback a checkout
   predating the merge reads as "nothing is configured", which for sources means every
   project silently unmanaged — a failure with no symptom until CI resolves the wrong thing.

**`uv.toml` stays out, permanently.** uv reads only its own settings file and rejects any
foreign table — measured against uv 0.11.8: `UV_CONFIG_FILE=config.toml uv pip install …`
fails with ``error: Failed to parse: `config.toml`. The `sources` field is not allowed in a
`uv.toml` file.`` It is also generated from the activated project set and gitignored (a
private index must reach it only when its project is active), so it is not the same kind of
artifact in the first place.

### Consequences

- One provenance header, one `aisland config show`, one file in a review diff.
- `Axis.file` is gone; `Axis.plural` now names the `[tool.aisland]` declaration key, the
  `config.toml` key AND the `.git/config` suffix — one word driving three things instead of
  two, which is the drift this merge removes rather than adds.
- A new axis needs no new file: `write_committed` already writes a key. It gets no comment
  block until one is added to `_SECTION_DOC`, which is the only per-axis knowledge in
  `config.py`.
- The renderer is a general TOML emitter for the scalar/list/table subset, so an unknown
  hand-added section survives a write instead of being dropped.
- The legacy fallback is dead weight once every machine has migrated. It is deliberately
  cheap to delete (one dict, one loop) and is tracked in `TASKS.md`.

### Example

```toml
# config.toml — one file, three sections, each with its own command
frameworks = ["torch", "lightning", "keras"]
trackers   = ["mlflow", "tensorboard", "aim"]

[sources.loggair]
source = "pypi"
distribution = "loggair"
```

```python
# selection.py and sources.py never format anything — they hand over plain values
update_config(workspace_root, axis.plural, list(names))          # frameworks = [...]
update_config(workspace_root, SOURCES_KEY, table or None)        # [sources.*], or drop it
```

### What you may change

Add a section by writing its key through `config.update` and (optionally) a comment block in
`_SECTION_DOC`. Do NOT teach `config.py` about `ProjectSource` or `Axis` — the one-way
dependency is what keeps it cycle-free. Do NOT move the uv overrides in here; uv cannot read
this file, and `manager._ROOT_UV_BASE` -> the generated root `uv.toml` is a different
artifact with a different lifetime. Deleting the legacy fallback is safe once no machine
carries a pre-2026-08-01 checkout.
