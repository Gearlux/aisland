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
| `Jenkinsfile.local` | **nothing** | installs nothing — it tests the `.venv`, where the effective source was already applied |
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

- **`Jenkinsfile.local` needs no source configuration at all**, because it installs nothing: the
  effective source of every dep was applied to the `.venv` by `aisland setup`, and the local
  pipeline tests that venv as it is (see [the local pipeline tests the venv, it does not
  install](#the-local-pipeline-tests-the-venv-it-does-not-install)). A per-machine override is
  therefore honoured there for free, and no source change of any kind can rewrite the file.
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
# of it, for the runner files. `Jenkinsfile.local` calls none of this: it installs nothing, so
# the file cannot depend on configuration at all.
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
  branching — or any install — in that file would quietly restore the churn this record exists
  to remove.
- The `local` kind's editable install has ONE spelling, `selection.editable_target` (the `dev`
  extra plus the selected extras on both axes), shared with `setup_workspace` (2026-08-22). Do
  not inline a bare `uv pip install -e <dir>` at a new call site: that is how `aisland source set
  … local` dropped every extra `setup` had just installed, and the helper exists so the two
  installs cannot disagree again.

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
`matrainer[torch,lightning,keras,tensorflow]`. That is what makes "install only what I work with"
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
  `matrainer[keras]>=0.1.0`), and installing the consumer first resolves against the sibling's
  already-installed metadata — uv then warns `does not have an extra named 'keras'` and installs
  without it. Found on the first real run of the function, not in review.
- **Deselecting does not uninstall.** uv has no notion of "the packages that belonged to this
  extra" — they are ordinary dependencies, possibly shared — so dropping `keras` leaves the wheel
  behind. Accepted: a stale wheel costs disk, not correctness.
- **`--non-interactive` selects everything** when nothing is recorded. A headless setup must
  neither hang on the prompt nor hand back a workspace that cannot train.
- **Scope is deliberately limited to projects that OFFER a choice** (decided 2026-07-30 after
  reading each project, not counting its imports). `recordstream`, `matrainer` and `sonair` declare
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
==> Installing matrainer (/w/matrainer[dev,torch,lightning,keras])...
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
NON_LIQUIFAI_CLIS = (NonLiquifaiCli("aisland", "click"), NonLiquifaiCli("hydraide", "click"), NonLiquifaiCli("streamstudio", "argcomplete"))
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

---

## Reclaiming a checkout's disk is a MOVE into a dated store, and it is per-project

*2026-08-04*

### Context

A workspace checkout accumulates gigabytes git never sees: `.venv/`, `build/`, `.mypy_cache/`,
`lightning_logs/`, `runs/`, downloaded checkpoints. A real measurement while writing this:
`sonair` alone carried **1.4 GiB across 48 ignored entries**, `aisland` another 1.4 GiB — all
invisible to `git status`.

The tool that already exists for this is `git clean -Xdf`, and it was rejected for one
reason: in *this* workspace an ignored path can be load-bearing. `aisland/bin/` holds
provisioned binaries (node, process-compose), `aisland/data/` holds every service's state,
the workspace `.env` is what makes `$DATA_ROOT` resolve in every YAML config. Deleting them
is recoverable only by re-provisioning, and the mistake is discovered *after* the delete.

The second half of the context is that the destination is on the same filesystem as the
source. That makes the choice between "delete" and "move" nearly free: a rename costs the
same for a 2 GiB `.venv` as for an empty directory.

### Decision

`aisland clean <project>` **renames** every git-ignored path into
`aisland/data/backups/<project>/<YYYYMMDD-HHMMSS>/files/`, mirroring its path relative to the
project, and writes a `manifest.json` beside the payload. Space is reclaimed by the
**retention sweep** that runs on *every* invocation: backups older than `--keep-days`
(default 7) are deleted, across all projects, *before* the move — so a run on a full disk
frees the expiring space first.

Scope is **one project per invocation**, and there is deliberately no `--all`. The blast
radius of "every ignored file in the workspace" is the shared `.venv`, every service's data
directory and the `.env`, from a command whose name sounds routine. The workspace **root**
repository is refused outright for the same reason.

Discovery is `git status --porcelain --ignored -z`. Both flags carry weight: `--ignored`
collapses a fully-ignored directory into ONE entry, so a 30k-file virtualenv is one rename
rather than 30k; `-z` makes paths NUL-separated and never quoted, so a filename containing a
space or a newline round-trips exactly.

Three invariants protect the destructive half:

| Invariant | What it prevents |
|---|---|
| The project directory must BE its repository's root (`rev-parse --show-toplevel` equality) | Every project is a submodule (a `.git` **file**); `git status` run merely *inside* a repo reports THAT repo's ignored set, so a name resolving to a plain subdirectory would clean the enclosing tree |
| An ignored entry containing the backup store is split into its children (`_split_around`) | `aisland clean aisland`: its `.gitignore` ignores `data/` wholesale and the store is `data/backups`, so the collapsed entry git reports **contains the destination** |
| The sweep deletes only directories whose name parses as `STAMP_FORMAT` | Anything a human dropped under the backup root; the name is also why an interrupted run (no manifest) still expires, and why an mtime — which any write underneath changes — is not the clock |

The store gets its own command group — `aisland backups list|restore` — rather than
subcommands of `clean`, because the two act on different objects: `clean` on a project's
working tree, `backups` on the store. It also keeps the destructive verb one word long.

Restore **moves** as well, and consumes the backup as it goes (the manifest is rewritten,
an emptied backup is removed). Two of its rules are the interesting ones:

- A path the project has a **different** copy of is skipped, never clobbered, and the command
  exits 1. The usual case is a cache or `.venv` that rebuilt itself since the clean — almost
  always the copy to keep — and a script must be able to see that the restore was partial.
  `--force` replaces instead, after asking.
- A path the project has an **identical** copy of is not a collision: it is reported
  `already`, the redundant backup copy is dropped, and the exit code stays 0. Because that
  decision deletes data, the comparison is a real one and not a quick check — size and file
  count, then the path set, then byte-for-byte; any `OSError` means "not the same". The
  rsync-style size+mtime check was rejected outright: the case this exists for is a path the
  operator already put back with `cp -R`, which restamps every file it writes.
- The **manifest**, not the payload's top level, is the restore granularity, because it
  records the paths that actually moved. A backed-up `data/jenkins` merges into a `data/`
  the project has since re-created; a top-level walk would try to restore `data` itself and
  collide with it. A manifest-less backup falls back to the payload's own top level.

### Consequences

- A clean is undoable for `--keep-days` days, by `aisland backups restore` or by copying
  `files/` back. That is the whole reason the payload mirrors the project tree instead of
  being an archive: recovery also works as a plain `cp -R`, needs no tool, and survives this
  code being the thing that broke — which is why `clean` prints both forms.
- Disk usage temporarily *doubles* for the moved content — it is still on disk until it
  expires. `--keep-days 0` is the escape hatch on a genuinely full volume.
- The manifest's `bytes` is what the sweep later reports as freed. Re-walking an expiring
  tree just to print that line would cost more than the deletion it describes.
- Cleaning `aisland` takes away the provisioned `bin/` and every service's `data/`; that is
  honest behaviour for "delete what git ignores", not a bug — it is documented rather than
  special-cased, because special-casing one project's ignored paths would make the tool's
  contract unpredictable.
- `aisland reset` and this command do not overlap: reset wipes each *service's* subdirectory,
  never the whole `data/` tree, so backups survive it.

### Example

```bash
aisland clean sonair --dry-run     # the plan + which backups expire; nothing changes
aisland clean sonair               # confirm, sweep, move
aisland backups restore sonair     # put it back
aisland backups prune              # the sweep on its own, for a workspace nobody is cleaning
```

```text
Moved 48 entries (1.4 GiB) into .../aisland/data/backups/sonair/20260804-164945
Restore with:  cp -R .../backups/sonair/20260804-164945/files/. /Users/gert/acme/sonair/
```

```python
# The split that makes `aisland clean aisland` safe: the store is held back, its siblings move.
def _split_around(entry: Path, protected: Path) -> Iterator[Path]:
    if entry == protected:
        return
    if entry.is_dir() and protected.is_relative_to(entry):
        for child in sorted(entry.iterdir()):
            yield from _split_around(child, protected)
        return
    yield entry
```

### What you may change

The retention window, the manifest's fields, and the output format are all free. What must
NOT change: the move-instead-of-delete semantics on both sides, the per-project scope (no
`--all`), the toplevel-equality guard, restore's skip-don't-clobber default, and the sweep's
rule that it only ever deletes what it created. Moving the backup store out of
`aisland/data/` would also mean revisiting `_split_around` — it exists precisely because the
store lives inside a project that can itself be cleaned. Adding a field to the manifest is
cheap; *relying* on one being present is not, since an interrupted clean leaves none and both
the sweep and restore are built to work without it.

---

## A containerised service pins its image by digest and is told what the VM shares

*2026-08-17*

### Context

Two aisland services now run as Docker containers rather than supervised processes (a
tracking server assembled from upstream's compose file, and a SigMF viewer that is one
image), and on this workspace's macOS machines Docker is Colima — a Linux VM. That changes
what a bind mount means: `docker run -v <host>:<target>` binds a path *inside the VM*, and the
VM sees only what Colima shares with it. Measured on colima 0.10.3: the default share is
`$HOME` and nothing else, so a mount from `$DATA_ROOT=/Volumes/…` yields an EMPTY directory
that Docker creates inside the VM — no error at any layer, the container simply lists
nothing. Passing `--mount` to colima to add a share *replaces* the default (`colima start
--mount /Volumes/Store:r` left the VM with no `$HOME`), and colima persists the flags into its
profile, so a wrong flag survives every later restart.

Separately, an image tag is not a pin: on ghcr the viewer's `:latest` is a 2023 build and
`:pre` moves with every merge, so `docker run image:tag` runs something different next month
with no signal in the workspace.

### Decision

- **The mount contract lives in ONE place — `aisland docker start --mount DIR[:w]`.** It
  re-states `$HOME:w` ahead of every requested share, resolves symlinks (the VM resolves a
  shared symlink against *its* tree), refuses a directory that does not exist (colima would
  share an empty one), and refuses to change shares under a running engine — a VM restart
  stops every container, so it prints `aisland docker restart --mount …` instead. The
  profile file is the record of what the VM shares (`configured_mounts()`), and liveness is
  the exit code of `colima status`, never its text.
- **A containerised service diagnoses the empty-share case at start** by comparing what the
  host and the container see (the viewer counts sidecars on both sides) and naming that fix.
- **An image is pinned as `<image>:<version>@<digest>`** — the tag documents, the digest is
  what Docker verifies — and the two constants are bumped together; a checksum-pinned upstream
  compose file is the same rule applied to a config artifact.
- **Docker owns these lifecycles, not the supervisor**, and their `info()` says so rather than
  printing the base class's process-compose line.

### Consequences

- The empty-mount failure has a name and a remedy at the point it happens, instead of an
  empty table and a forensic session; the remedy is one command that cannot drop `$HOME`.
- Any new containerised service inherits the contract by calling `aisland docker start
  --mount` in its docs and comparing host/container counts in its start — no per-service
  colima logic.
- A version bump is a two-constant edit; forgetting the digest fails at start (Docker refuses
  a mismatched digest) rather than running an unverified image.
- Accepted cost: the check is a `docker exec` after the API answers, i.e. a start takes the
  readiness wait before it can diagnose the share; and a machine on Docker Desktop (which
  shares `/Volumes` by default) never needs `--mount` but still gets the same message if some
  other reason empties the mount — the message names Colima as the *usual* cause.

### Example

```bash
aisland docker start --mount "$DATA_ROOT:r"        # colima start --mount $HOME:w --mount /Volumes/Store:r
aisland iqengine start --recordings "$DATA_ROOT/sigmf"
```

```text
# Before the share existed (measured):
The container sees NO sidecars under /Volumes/Store/sigmf while the host has 77.
  The Docker VM does not share that directory (Colima shares only $HOME by default),
  so the bind mount came up empty. Share it, then start again:
      aisland docker restart --mount /Volumes/Store/sigmf:r
      aisland iqengine start
```

```python
IQENGINE_VERSION = "202512200500.0.0-pre-202512200500"
IQENGINE_DIGEST = "sha256:1b129f6e5e609c9abbd3ca1f2f3dbef533b31b95173f8845c5610ae202236f42"

def image_ref() -> str:
    return f"{IQENGINE_IMAGE}:{IQENGINE_VERSION}@{IQENGINE_DIGEST}"
```

### What you may change

The set of shares, the read-only default, and the wording are free. What must not change:
`$HOME:w` is always re-stated when any mount is passed to colima; a running engine is never
restarted by `start`; an image reference always carries its digest; and a containerised
service reports the host/container mismatch rather than an empty result. Moving the mount
logic into a service (so each one edits the colima profile) would recreate the drift this
record exists to prevent.

---

## The scaffolder pre-installs the closure of what CI installs — not every reachable extras group

*2026-09-02*

### Context

Gearlux distribution names are deliberately unpublished, so `.[dev]` alone cannot resolve a
sibling dep from PyPI — the generated pipelines pre-install each internal dep from its git URL
with `--no-deps` first, and something must compute *which* deps that loop names. The original
rule collected the transitive closure of "every Gearlux name in `project.dependencies` **or any
extras group** of every reachable pyproject". Extras walk was justified for the scaffolded
project itself (its CI does install `.[dev]`), but applying it transitively collected packages
no install step would ever request: a signal-analysis app (since removed from the workspace)
depended only on `signals`; signals's *dev* extra names `matrainer` and `annotaide`; matrainer's *torpaido* extra names `torpaido` —
and CI installed all three, half-broken (`--no-deps`, their own deps never resolved), for a
project that imports none of them. The same walk let annotaide's and signals's mutual
dev-extra references cycle back to the scaffolded project, so each pipeline pre-installed *its
own* `main` branch over the checkout under test. A third latent defect hid beside these: specs
were name-stripped with `re.split(r"[>=< ]", …)`, which cannot parse `matrainer[torch]>=0.1.0`
— the spec silently matched nothing, masked only because every consumer also declared a bare
`matrainer>=0.1.0`.

### Decision

The closure mirrors the install steps the templates themselves emit, node by node:

- the **root** project contributes its base deps plus the extras its CI installs —
  `.[dev,<selected axis extras>]` and the best-effort `.[notebook]` step;
- a **transitive** dep contributes its base deps plus *only* the extras the incoming
  requirement named (`matrainer[keras]` walks matrainer's `keras` extra) — its other extras
  are never requested by anything CI runs, so they contribute nothing;
- the root's own distribution name is discarded from the result;
- specs are parsed with `packaging.Requirement` (aisland's one new dependency), so
  bracket extras, URL specs, and markers all parse instead of string-splitting.

### Consequences

Twelve projects' pre-install lists shrank to their real dependency cones (the signals-only
app above: five deps → two). A sibling named only inside a dependency's dev extra no longer reaches any
consumer pipeline — if a root's CI genuinely needs it, the root's own extras must say so, which
is also where uv will look. The committed CI files stop churning when an unrelated project
edits its dev extra. What the closure still deliberately keeps: transitive **base** deps
(that is the reason the loop exists — `.[dev]` must resolve them and PyPI cannot), and the
topological install order.

### Example

```toml
# app/pyproject.toml                      # libmid/pyproject.toml
[project]                                 [project]
name = "app"                              name = "libmid"
dependencies = ["libmid>=0.1.0"]          [project.optional-dependencies]
                                          dev = ["libextra>=0.1.0", "pytest"]
```

```groovy
// app/Jenkinsfile — libmid is pre-installed; libextra is NOT (libmid installs bare,
// and app's `.[dev]` resolves only libmid's base deps — nothing requests libmid[dev]):
sh "${VENV_BIN}/uv pip install --no-deps git+https://github.com/Gearlux/libmid.git@main"
sh "${VENV_BIN}/uv pip install -e .[dev]"
```

### What you may change

Which extras count as "installed by CI" must track the templates: if a template ever installs a
new extras group, the root-node walk gains it in the same change (the sets are adjacent in
`scaffold_pipelines`). Do not re-add a transitive extras walk "to be safe" — the pins in
`tests/test_jenkins_scaffold.py::TestTheInternalDepClosureFollowsWhatCIInstalls` assert both
directions (what stays in and what stays out). The topological sort orders by base deps only;
that is sufficient because every pre-install runs `--no-deps` and order is cosmetic.

## A dev dependency that needs more than pip is declared by the dependency itself

*2026-09-10*

### Context

Playwright's package cannot open a page on its own — the browser is a separate download
(`playwright install chromium`). A consuming project's browser-driven test therefore has two
environments to run in: the developer venv `aisland setup` builds, and the runners the
scaffolded CI builds. Both install the `dev` extra with pip/uv, and neither would fetch the
browser, so the test would `importorskip` (or skip on launch) in both — a green build that
quietly checks less, the exact failure the committed-selection rules already guard against for
tracker backends.

### Decision

The dependency IS the declaration. `selection.dev_extra_names` reads the `dev` extra's
distribution names (through `packaging.Requirement`, so every spec spelling yields its bare
name); `selection.browser_install_command` turns a `playwright` among them into the venv's
`playwright install chromium`, which `aisland setup` runs right after that project's editable
install; and `services/jenkins.py` emits the equivalent line after `.[dev]` in the two runner
templates — once in `Jenkinsfile` (one venv serves every stage), and in every `ci.yml` job that
runs pytest, with `--with-deps` there because an Ubuntu runner lacks the OS libraries and has
sudo. `Jenkinsfile.local` fetches nothing: it tests the venv setup already provisioned. This mirrors the registered `slow` marker: a project opts in through something it
declares anyway, and a project that does not gets byte-identical output.

Rejected: a generic `[tool.aisland] post-install = ["…"]` list of shell commands. It would have
served this one case at the price of a fourth grammar (arbitrary commands rendered into three
templates and run by setup) whose only known user is the browser download; when a second such
dependency appears, that is the moment to generalise, with two cases to design against.

### Consequences

- A browser-driven test runs wherever the suite runs; the skip in such a test guards only a
  hand-built environment that installed the package and not the browser.
- Chromium only, and one browser name in one constant (`PLAYWRIGHT_BROWSER`) shared by setup
  and the scaffold, so the two cannot disagree.
- A Jenkins agent downloads the browser once per user (Playwright's cache is per user, outside
  the per-build venv); a GitHub runner downloads it per job.

### Example

```toml
# <project>/pyproject.toml — the whole declaration
[project.optional-dependencies]
dev = ["pytest>=7.0.0,<9.0.0", "playwright>=1.40.0,<2.0.0"]
```

```python
from aisland.selection import browser_install_command
browser_install_command(Path("proj/pyproject.toml"), Path(".venv/bin"))
# ['.venv/bin/playwright', 'install', 'chromium']   — None for a project without the dependency
```

### What you may change

- The browser set, if a test ever needs another engine — change the constant and the two
  template lines together, and the pins in `tests/test_jenkins_scaffold.py`.
- Generalising to a declared post-install list — only once a second dependency needs it, and
  as a replacement for this rule, never beside it.

## The local pipeline tests the venv, it does not install

*2026-09-13*

### Context

`aisland jenkins scaffold` emits a `Jenkinsfile.local` per project, seeded into the local Jenkins
as `<project>.local` and run inside the workspace checkout against the shared `.venv`. Its
`Initialize` stage used to install into that venv: every internal dep editable with `--no-deps`,
then the project's `dev` extra (plus selected framework/tracker extras), then its `notebook`
extra, then `jupyter nbconvert`. The venv those installs targeted is the one `aisland setup`
provisions, where every workspace project is already editable-installed from its own checkout —
so the installs changed nothing, but each one re-resolved the full dependency set against the
configured indexes. Measured on 2026-09-13 on a slow VPN link: torpaido's `Initialize` took
1430 s (`Resolved 82 packages in 11m 50s`, then 696 s more for the notebook extra) to reinstall
only torpaido itself, recordstream's 830 s and vendorcli's 574 s, while the quality gates and tests
that followed took about 12 s. The installs also serialised builds on the venv's exclusive lock
(`Waiting to acquire exclusive lock for …/.venv`).

A second queue sat in front of that one. The `<workspace>.local` orchestrator declared
`agent { node { label 'built-in' } }`, holding one of the built-in node's two executors for its
whole run although it only triggers jobs. Each "parallel" phase therefore ran its projects one
at a time: vendorcli waited 875 s for recordstream, and annotaide and purifai showed
`Waiting for next available executor` behind torpaido.

### Decision

- `Jenkinsfile.local` installs nothing. Installing is `aisland setup`'s job; the local pipeline
  runs the gates of a checkout against the venv exactly as setup left it.
- Its `Initialize` stage keeps one check: the project's distribution in the venv must record THIS
  checkout as its source (`direct_url.json`, written for every editable install), else the build
  stops naming `aisland setup`. It reads the distribution rather than looking up the package, for
  two reasons: nothing is imported, and a project may ship its code inside another project's
  package namespace (`signals-bridge` installs `signals/bridge/`), where a package lookup
  would resolve a sibling's checkout. It runs `python3 -P` — Jenkins runs inside the checkout,
  and without `-P` the working directory is first on `sys.path`, so a stale `<name>.egg-info`
  left there shadows the venv's record and a correctly-installed project fails (measured on
  `signals-bridge`: exit 1 without `-P`, exit 0 with it).
- The orchestrator declares `agent none`; `build job:` needs no node.
- The runner files (`Jenkinsfile`, `ci.yml`) are unchanged: a runner has no workspace venv.

Rejected: speeding the installs up (resolving only a private package from the corporate index,
or caching harder). It keeps two owners of the venv and still pays a resolve per build for
nothing.

### Consequences

- A project that gains a dependency fails its local build with `ModuleNotFoundError` until
  `aisland setup` has run. That is the intended signal; the pipeline never installs it.
- `Jenkinsfile.local` carries no dependency list and reads no source or selection configuration,
  so neither `aisland source` nor `aisland framework`/`tracker` changes can rewrite it.
- With installs gone nothing holds the venv lock, and with the orchestrator off the executors a
  phase's builds run in parallel, bounded by the node's executor count.

### Example

```groovy
// <project>/Jenkinsfile.local — the whole Initialize stage
stage('Initialize') {
    steps {
        echo 'Validating Environment...'
        sh "${VENV_BIN}/python3 --version"
        sh """${VENV_BIN}/python3 -P - 'torpaido' "${env.WORKSPACE_ROOT}/torpaido" <<'PY'
import importlib.metadata, json, pathlib, sys
dist, checkout = sys.argv[1], pathlib.Path(sys.argv[2]).resolve()
try:
    raw = importlib.metadata.distribution(dist).read_text("direct_url.json")
    found = json.loads(raw)["url"] if raw else "an install with no source URL"
except importlib.metadata.PackageNotFoundError:
    found = "not installed"
if found != checkout.as_uri():
    sys.exit(f"{dist} is not installed from {checkout} (found: {found}) - run `aisland setup`")
print(f"{dist} -> {checkout}")
PY"""
    }
}
```

```text
torpaido -> /Users/you/acme/torpaido                                                                              # exit 0
torpaido is not installed from /Users/you/acme/torpaido (found: file:///Users/you/other/torpaido) - run `aisland setup`   # exit 1
```

### What you may change

- The check itself (what counts as "this checkout"), as long as it imports nothing, keys on the
  distribution rather than a package name, and does not let the working directory decide the
  answer — the pins in `tests/test_jenkins_scaffold.py::TestTheLocalPipelineInstallsNothing` run
  the emitted shell from inside a checkout carrying a stale `egg-info` for exactly that reason.
- The orchestrator's phase layout. What must not change: it holds no executor
  (`tests/test_orchestrator_executor.py`), and `Jenkinsfile.local` gains no install step.
