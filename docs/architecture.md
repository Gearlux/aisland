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
dependency. Two failures followed from reading it: switching confluid to `local` to fix bugs made
every consumer's committed CI install confluid from GitHub `main` (and switching back rewrote
them again — sixteen projects, twice, in one afternoon), and liquifai, released on PyPI but
developed `local`, was built from GitHub `main` by every consumer. The user's ruling: CI "should
always point to released packages, and to GitHub for non-released ones". So the scaffolder asks
`sources.released_on_pypi(dep)` — a FINAL, non-yanked version whose metadata points at
`github.com/Gearlux/` — and `aisland source set` / `unset` regenerate nothing at all. The earlier
objection to a scaffold-time probe (an offline run would flip every project to git URLs) is met
by refusing instead of guessing: an unreachable PyPI raises `ReleaseProbeError` and the project's
files are not written. Rejected alternative: a hand-kept "released" list in `config.toml` —
offline, but it has to be remembered at every first release, and forgetting it silently keeps a
released dependency on GitHub.

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
- **No source change re-scaffolds anything** (2026-09-26). CI reads no source, so `source set`
  and `unset` have nothing to regenerate; what changes a CI file is a dependency's FIRST RELEASE,
  picked up by the next `aisland jenkins scaffold`.
- **Generating CI needs PyPI** (2026-09-26). `released_on_pypi` is asked once per distribution
  per process (cached) and raises when the index cannot answer — committed file content must never
  depend on whether the network happened to be up, so the run stops rather than guessing.
- **A consumer builds against the RELEASE of a released dependency, never its `main`.** Using
  something a released dependency has only on `main` fails that consumer's CI until the release —
  the accepted cost of the ruling (measured on day one: streamstudio requires `liquifai>=0.3.0`,
  and 0.3.0 exists only on liquifai's `dev/main`).
- Discovery still does the heavy lifting: a `pypi`-sourced project's directory is empty, so
  `discover_projects()` drops it from the editable-install loop with no special-casing.

### Example

```bash
$ aisland source set loggair github --ref dev/main   # from local:main — no CI touched
==> loggair: committed source set to github:dev/main in config.toml
==> loggair: github @ dev/main (git+https://github.com/Gearlux/loggair.git@dev/main)
==> Wrote acme.code-workspace (28 folders)

$ aisland source set loggair pypi                    # no CI touched either
==> loggair: committed source set to pypi in config.toml
```

```python
# The scaffolder reads NO source — it asks PyPI, per internal dependency, for the runner files.
# `Jenkinsfile.local` calls none of this: it installs nothing, so it depends on nothing.
def _dep_is_released(dep: str) -> bool:
    return released_on_pypi(dep)   # released -> `.[dev]` resolves it; else git+…@main

>>> [(d, released_on_pypi(d)) for d in ("liquifai", "recordstream", "purifai")]
[('liquifai', True), ('recordstream', False), ('purifai', False)]   # 0.1.0a1 only / a stranger's name
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
- What must not change: the layer→artifact mapping above; that CI reads no source and no `ref`
  (only whether PyPI has a release); that an unreachable PyPI stops the scaffold rather than
  guessing; and that `Jenkinsfile.local` reads nothing at all. Reintroducing per-kind
  branching — or any install — in that file would quietly restore the churn this record exists
  to remove.
- The `local` kind's editable install has ONE spelling, `selection.editable_target` (the `dev`
  extra plus the selected extras on both axes), shared with `setup_workspace` (2026-08-22). Do
  not inline a bare `uv pip install -e <dir>` at a new call site: that is how `aisland source set
  … local` dropped every extra `setup` had just installed, and the helper exists so the two
  installs cannot disagree again.

---

## The VS Code workspace file is generated per machine, never tracked

*2026-10-18*

### Context

`acme.code-workspace` was a tracked, hand-maintained file. aisland only commented the folder
entries of the `[sources]` projects out and back in, line by line, and only from the COMMITTED
layer — a tracked file must not differ per machine. Two failures followed. A per-machine override
never reached the editor: `aisland source set loggair local --machine-only` installed the checkout
but left the editor's list alone, so loggair stayed hidden on the one machine developing it. And everything else stayed manual: seven projects added between 2026-09-28 and 2026-10-04 each
took a hand edit of two lists (`folders` and a `python.analysis.extraPaths` that pyright ignores
while `pyrightconfig.json` exists), and inactive optional submodules showed as empty folders.

### Decision

Generate the whole file on every `aisland setup` and every `aisland source set|unset|apply`, and
gitignore it, like `pyrightconfig.json` and `.mcp.json`:

- **Folders** are what this machine has checked out: the workspace, then every discovered project
  in dependency order (sorted by folder name first, so the order the disk lists folders in never
  reorders it), then loggair's log folder when it exists. An empty directory — a project detached
  to `pypi`/`github`, an inactive optional submodule — is not a project, so it is not listed.
- **Loggair's log folder** is asked from loggair, never written into aisland: `python -P -m
  loggair --json` from the workspace root with `.env` over the environment, exactly as a run in
  the workspace resolves it (`LOGGAIR_DIR`, a `log_dir` in a loggair config file, or `./logs`).
  Out of process, so aisland keeps no dependency on loggair.
- **Settings and extensions** come from the tracked template `aisland/aisland/code-workspace.json` (package data, so a wheel carries it), strict
  JSON with no `folders`, so the generator is `json.dumps` plus the provenance header (VS Code
  reads JSONC, so the header lives in the file).
- The file is named after the workspace (`get_workspace_name`), the name the template's
  `${workspaceFolder:<name>}` references use, and the first folder carries that name.
- An unchanged file is not rewritten, so VS Code does not reload the workspace for nothing.

### Consequences

- A source override shows in the editor, and no source change dirties a tracked file.
- A new submodule appears on the next `aisland setup`; nothing is edited by hand.
- A setting changed in VS Code at *Workspace* scope is written into the file and lost at the next
  setup. Shared settings belong in the template; personal ones in VS Code's *User* settings.
- Pulling the commit that untracked the file deletes it on another machine until `aisland setup`
  runs there.

### Example

```jsonc
// =========================================================================
// AUTO-GENERATED FILE — DO NOT EDIT BY HAND
// Generated by: aisland setup
// Source template: aisland/aisland/code-workspace.json (aisland.manager.write_code_workspace)
// To regenerate:  aisland setup
// =========================================================================
{
    "folders": [
        { "name": "acme", "path": "." },
        { "name": "loggair", "path": "loggair" },
        { "name": "confluid", "path": "confluid" },
        { "name": "logs", "path": "../logs" }
    ],
    "settings": { "python.defaultInterpreterPath": "${workspaceFolder:acme}/.venv/bin/python" },
    "extensions": { "recommendations": ["ms-python.python"] }
}
```

### What you may change

- The template's settings and recommendations, freely; re-run `aisland setup`.
- The folder order rule (currently dependency order, ties by folder name) is a judgement call.
- What must not change: the file stays generated and gitignored, its folders follow the checkouts
  on this machine, and the log folder is loggair's answer, never a path typed into aisland.

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
extra" — is wrong twice over: sonair declares eight engine extras (`lightning`, `torch`, `keras`,
`tensorflow`, `fastai`, `huggingface`, `jax`, `mlx`) that are alternatives to one another, and no
machine works with all of them, and `dev` / `http` / `viz` / `notebook` / `all` sit in the same
table as `torch` and `keras`, so no name-based rule can tell a training engine from a
documentation extra.

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
  reading each project, not counting its imports). `recordstream`, `matrainer`, `sonair`,
  `deltaid`, `distilaide` and `raidar` declare frameworks because a genuine alternative exists:
  each keeps its core install free of torch and puts every engine behind an extra. Another
  keeps `torch` as a plain hard dependency and is NOT declarable:

  | project | why it requires torch |
  | --- | --- |
  | `raidar-ultralytics` | the ultralytics detection trainer; `__init__.py` imports its torch/ultralytics classes eagerly. |

  Moving their deps behind a `[torch]` extra was considered and REJECTED: `pip install
  raidar-ultralytics` would then yield a package whose `__init__` raises `ImportError`, which is
  dishonest packaging for a project that genuinely needs torch. The consequence to accept is that selecting `keras` on
  a FULL workspace still installs torch — the selection narrows multi-backend projects, and does
  not promise a framework-free venv. (`torpaido` was a second case and is fixed rather than
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
NON_LIQUIFAI_CLIS = (NonLiquifaiCli("aisland", "click"), NonLiquifaiCli("hydraide", "click"))
```

**What you may change.** Add a CLI as a row in `NON_LIQUIFAI_CLIS`. A fourth
completion *mechanism* means a new `CliKind` and its renderer. Do not move any of
this back into `setup.sh`, and do not let it grow a second copy there — the
duplicate would rot exactly as the original did.

---

## The completion block leaves a non-interactive shell before it runs anything (2026-10-20)

**Context.** The generated block does not *describe* completion, it *fetches* it: each click
CLI contributes `eval "$(_X_COMPLETE=bash_source X 2>/dev/null)" || true`, which RUNS that CLI
at source time. That is fine in the shell a person types in. It is not fine in any other shell
that happens to read the file.

Click, asked for a bash completion script, first checks the shell's version by spawning
`bash --norc -c 'echo "${BASH_VERSION}"'`. A non-interactive bash reads `$BASH_ENV` even with
`--norc`. So on a machine whose environment pointed `BASH_ENV` at the workspace's
`project.bashrc` — which sources this file — one `aisland` became: emit completion → probe the
shell → the probe sources `project.bashrc` → which runs `aisland` → emit completion → probe …
Every PPID the previous PID, without end.

Measured on 2026-10-20: it filled the machine's process table (2931 processes against a per-user
limit of 2666) in about five minutes. Nothing on the machine could fork — `git`, `ps`, `pkill`
and a bare `echo` in a new shell all failed — so it could not even be diagnosed or killed from
inside, and the machine was rebooted twice. It is also SELF-SUSTAINING: killed in slices it
refills from the survivors, with no parent process left to stop.

**Decision.** The block's first line after its provenance header is

```bash
case $- in *i*) ;; *) return 0 ;; esac
```

Completion means nothing without a prompt, so a shell without one leaves before reaching
anything that runs. The guard is rendered for every shell, not just bash: zsh emits no version
guard but runs its CLI at source time just the same.

This is deliberately not a re-entry counter or an "am I already loading?" marker. Those make the
recursion shallower; this makes it impossible, and it does not matter which of the many things
that can set `BASH_ENV` set it.

**Consequences.**

- A non-interactive shell that sources the fragment now costs nothing at all, where before it
  paid for ~20 function definitions and two CLI executions. Every `bash -c` in the workspace
  that inherits such an environment gets that back.
- An interactive shell is unchanged: every CLI still registers.
- A chain already running does not stop when the file is fixed — those shells sourced the old
  copy. One clear is needed after the fix lands, and it must be a loop, not a single signal.
- The clear anchors the pattern: the bomb's invocations are the bare, argument-less ones, while
  `aisland tunnel …` is a person at a prompt. An unanchored `pkill -9 -f 'venv/bin/aisland'`
  killed a user's running command on the day this was found.

**Example.**

```text
measured, one `aisland`, capped subshell, 8 s           peak processes spawned
  as it was                                                   51   (hit the cap)
  with the guard                                               0

interactive shell, guard in place — completion still registers:
  $ bash -ic "source .project.bashrc.completion; complete -p annotaide"
  complete -o default -F _annotaide_completion annotaide
```

```bash
# clearing a chain that is already running (anchored: spares `aisland <subcommand>`)
end=$((SECONDS+40)); while [ $SECONDS -lt $end ]; do
    pkill -9 -f BASH_VERSION; pkill -9 -f 'venv/bin/aisland$'
done
```

**What you may change.** The guard's spelling, and whether a future shell needs a different
test for "interactive". What must hold: nothing in the generated block runs a program before the
guard. A line added above it re-arms exactly this. Pin:
`tests/test_completion.py::test_the_block_does_nothing_in_a_NON_interactive_shell`.

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
depended only on one sibling; that sibling's *dev* extra names `matrainer` and `annotaide`; matrainer's *torpaido* extra names `torpaido` —
and CI installed all three, half-broken (`--no-deps`, their own deps never resolved), for a
project that imports none of them. The same walk let annotaide's and that sibling's mutual
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

Twelve projects' pre-install lists shrank to their real dependency cones (the one-sibling
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
  package namespace (`raidar-ultralytics` installs `raidar/ultralytics/`), where a package lookup
  would resolve a sibling's checkout. It runs `python3 -P` — Jenkins runs inside the checkout,
  and without `-P` the working directory is first on `sys.path`, so a stale `<name>.egg-info`
  left there shadows the venv's record and a correctly-installed project fails (measured on
  such a child project: exit 1 without `-P`, exit 0 with it).
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

## A long stage of the local pipeline has a time limit, and past it the stage fails

*2026-10-15*

### Context

The local orchestrator builds every project at 01:00 and waits for each build (`build job:`,
`wait: true`); nothing in the orchestrator or the project pipelines bounded that wait. On
2026-10-14 the Slow Tests stage of one project's `.local` #13 ran 518.6 min (41 392 tests whose
own clocks summed to 11 min; pytest's clock said 8:38:30) and passed; on 2026-10-15 the Unit Tests
stage of `annotaide.local` #37 waited 108.9 min on a test that waits for a SIGINT Jenkins' background
shell ignores, until a hand `kill` ended pytest (`Terminated: 15`, exit 143). Both held
`acme.local` #36 "building" from 01:00 into the afternoon, and the morning report could only say
"still building". The stage durations of the six 01:00 runs before (read from each build's
pipeline-graph record) were small and steady: Unit Tests at most 6.0 min (streamstudio), Slow Tests
at most 29.6 min, Verify Examples 0.5 min, Verify Notebooks 1.4 min, Initialize and
the quality gates 0.2 min. The 35-48 min builds of some projects were executor queueing
inside the build, not stage time.

A second finding from #37: pytest killed before it writes `--junitxml` leaves the previous build's
`test-report.xml` in the checkout (the local job's workspace), and the stage's `junit` post step
recorded #36's 2 859 passed cases for #37.

### Decision

- Each stage of `Jenkinsfile.local` that runs the project's own code runs inside a time limit:
  `STAGE_LIMITS` in `services/jenkins.py` (Unit Tests 15, Slow Tests 60, Verify Examples 15, Verify
  Notebooks 15 min — about twice the longest healthy run measured). One table, every project the
  same; Initialize and the quality gates run tools, not the project's code, and get none. The wait
  for an executor is not counted: the node is held before the first stage.
- Past its limit the stage FAILS, naming itself and the limit (`TIMED_OUT_MESSAGE`), rather than
  ABORTING with Jenkins' own wording. Measured on two probe jobs with a 1 min limit around a shell
  ignoring SIGINT: a per-stage `timeout` caught and turned into `error(...)` ends the stage FAILED
  at 80 s, the build FAILURE, the stage's post block run, the next stages skipped, the console
  ending with the one line; the alternative, one whole-build `options { timeout }`, ends ABORTED
  with `Timeout has been exceeded`, names no stage and no limit, skips the pipeline's `failure`
  block, and would have the morning report parse Jenkins' wording and the orchestrator's
  `catchError` meet a result it was never measured with. FAILURE is the path every failing project
  already takes through the orchestrator (`<job> completed: FAILURE`) and through the morning report.
- The helper `limited(stage, minutes, body)` is defined in the generated file above `pipeline {`
  (`_LIMITED_HELPER`) and rethrows a `FlowInterruptedException` that arrives before the limit —
  Abort clicked by hand stays an abort. It needs no sandbox approval: `System.currentTimeMillis`
  and `Closure.call` are in script-security's generic whitelist, and a catch clause's class name
  is never checked.
- Each pytest step first removes the files it writes (`rm -f test-report.xml coverage.xml`,
  `rm -f slow-test-report.xml`), in both Jenkins templates: both workspaces persist between builds.
  A timed-out stage then has no report, the `junit` step records nothing, and the morning report's
  console path finds the timed-out line (`nightly._TIMED_OUT`) and quotes it.
- The push pipelines (`Jenkinsfile`, `ci.yml`) get no limit here: a runner's limits are measured
  on the runner (`TASKS.md`), and GitHub bounds a job at six hours anyway.

Rejected: bounding the orchestrator's `build job:` wait instead. The project build would keep
running, holding an executor for the rest of the night, and its own record would say nothing.

### Consequences

- The hung process may outlive the stage: in both probes Jenkins printed
  `After 20s process did not stop` and went on, and the `sleep` was alive minutes later. The
  orchestrator is unblocked; the process is the test's own bug to fix.
- A project whose healthy run grows past its limit fails its nightly build with the line above
  until the number in `STAGE_LIMITS` is raised and every project re-scaffolded — the morning
  report names it, so it is never silent.
- `aisland tests nightly` says `annotaide (timed out after 15 min)` for such a build, and its
  markdown quotes the line under the project's heading.

### Example

```groovy
// <project>/Jenkinsfile.local — the helper, and one stage inside its limit
def limited(String stage, int minutes, Closure body) {
    long started = System.currentTimeMillis()
    try {
        timeout(time: minutes, unit: 'MINUTES') {
            body()
        }
    } catch (org.jenkinsci.plugins.workflow.steps.FlowInterruptedException interrupted) {
        if (System.currentTimeMillis() - started < minutes * 60000L) {
            throw interrupted
        }
        error("${stage} timed out after ${minutes} min - its limit in Jenkinsfile.local (STAGE_LIMITS in aisland/services/jenkins.py)")
    }
}
...
        stage('Slow Tests') {
            steps {
                limited('Slow Tests', 60) {
                    sh 'rm -f slow-test-report.xml'
                    script { ... the pytest call ... }
                }
            }
```

```text
Timeout set to expire in 1 hr 0 min
+ /Users/you/acme/.venv/bin/pytest tests -m slow --junitxml=slow-test-report.xml
...
Cancelling nested steps due to timeout
Sending interrupt signal to process
After 20s process did not stop
[Pipeline] error
...
ERROR: Slow Tests timed out after 60 min - its limit in Jenkinsfile.local (STAGE_LIMITS in aisland/services/jenkins.py)
Finished: FAILURE
```

### What you may change

- The numbers in `STAGE_LIMITS`, and which stages carry one, as long as the table stays the one
  source (`tests/test_jenkins_scaffold.py::TestALongStageHasATimeLimit` reads it) and the
  generated files are regenerated with it.
- The wording of `TIMED_OUT_MESSAGE`, together with `nightly._TIMED_OUT`
  (`tests/test_nightly.py::test_the_line_the_pipeline_writes_is_the_line_the_report_reads`).
- What must not change: a timed-out stage FAILS (an abort would leave the orchestrator's and the
  report's paths unmeasured), an abort before the limit is rethrown, and every pytest step removes
  its report files first.

## A recurring environment skew becomes a doctor check, not a note

*2026-07-27; widened to the Docker layer 2026-08-31 and to Jenkins' Java 2026-10-07*

### Context

The workspace `.venv` is shared and built incrementally, so one install can leave a package
pair inconsistent: both OpenCV wheels installed (they own the same `cv2/` files, so their native
libraries mix), `huggingface_hub` left below what `transformers` imports, an MCP SDK whose server
module was renamed between majors. The Docker layer drifts the same way: a `credsStore` entry
naming a credential helper that was uninstalled, and a Colima VM whose trust store lacks the CA
of a TLS-intercepting proxy the host already trusts. Each failure surfaces far from its cause — a
`dlopen` error during pytest collection, an `ImportError` three libraries deep, every
`docker pull` dying on credentials it does not need — and each had been written up as a note in
an `AGENTS.md`. A note does not run, so every recurrence was diagnosed again from the start.

### Decision

`aisland doctor` runs the tuple `CHECKS` of `check_*` functions. Each returns
`CheckResult(name, ok, detail, remedy)`; the command exits 1 when any check failed, so a CI step
can gate on it.

- **A check is specific.** It names one known skew and carries the copy-pasteable command that
  fixes it; the test suite asserts every failing result has a remedy.
- **Imports run in a subprocess** (`_import_probe`). A broken native bundle can abort the
  interpreter, and a doctor that dies while diagnosing is worse than none.
- **A check mirrors an invariant enforced elsewhere, and names it.** The OpenCV check mirrors the
  `opencv-python ; sys_platform == 'never'` override in `manager._ROOT_UV_BASE`, so the doctor
  cannot grow a second policy that drifts from the first.
- **A probe that cannot run passes with a note**: no Colima installed, a stopped VM (the doctor
  never boots one), an unparseable config, a network error. This is the rule
  `supervisor._preflight_full_disk_access` already follows: an inconclusive probe must never cry
  wolf, or the operator learns to ignore the tool.
- **VM state is `DockerService.is_running()`** (the exit code of `colima status`), never a second
  parse of colima's output.

Rejected: a generic venv linter. `pip check` already lists lines that are expected here — a
library pinning one exact OpenCV build, libraries asking for the GUI wheel the workspace drops on
purpose — and a doctor that reported them would fail on every healthy machine.

### Consequences

- A recurrence costs one command and one fix line instead of a forensic session.
- A laptop that does not run Docker passes the preflight.
- The `colima-tls` remedy stays manual (user ruling 2026-08-31). Installing the network's CA
  automatically at `aisland docker start` would silently trust whatever intercepts TLS on any
  network the machine joins, to save a rare one-command fix. The CA install does not survive
  `colima delete`, which is why the check exists at all.
- Each check costs a subprocess or a file read, so the whole run stays a preflight rather than a
  build step.

### Example

```python
# aisland/doctor.py — a new check is one function plus one tuple entry
CHECKS: Tuple[Callable[[], CheckResult], ...] = (
    check_opencv_single_variant,
    check_huggingface_hub_floor,
    check_mcp_server_api,
    check_docker_credsstore,
    check_colima_vm_trust,
    check_jenkins_java,      # see "Jenkins runs on the Java its WAR accepts, by full path"
)
```

```text
# the opencv check's report when both OpenCV wheels are installed (exit 1)
  ✗ opencv           opencv-python=4.12.0.88 is installed; the workspace override drops it and keeps opencv-python-headless=4.13.0.92 — both own cv2/, so .dylibs will mix
      fix: uv pip uninstall opencv-python && uv pip install --force-reinstall opencv-python-headless   # use `uv pip`, not `pip` — pip does not read uv.toml's overrides
```

### What you may change

- Add a check: a `check_*` returning `CheckResult`, appended to `CHECKS`, with tests for its
  failing, passing and inconclusive branches in `tests/test_doctor.py`.
- The report's wording and order.
- What must not change: a failing result carries a remedy; imports stay out of process; an
  inconclusive probe passes; the doctor only reads — it never changes the environment and never
  boots a VM.

---

## Every pytest run of a workspace project is recorded, and the split is read from the record

*2026-10-05*

### Context

The workspace splits each project's tests into a fast run (plain `pytest`, within five minutes)
and a slow run (`pytest -m slow`, CI's), and keeps in the fast run only the integration tests
that break (user ruling 2026-10-05, aisland rules → "Two Test Runs"). Both halves of that rule
are measurements — how long a test takes, how often it fails — so the rule needs a record. The
local Jenkins already keeps one (each build's `junitResult.xml`), but it measured the wrong
thing: code reaches CI only after it passes on the developer's machine, and of the 351 builds it
held on 2026-10-05, 11 had a failing test. The breaks that decide what belongs in the fast run
happen in the runs nobody kept: the ones agents and developers make while they work.

### Decision

`aisland/pytest_stats.py` is both halves of the record.

- **A pytest plugin, installed by entry point** (`pytest11` → `aisland_test_stats`), so every run
  in the workspace `.venv` loads it with no per-project wiring. It records a run only when
  pytest's rootdir is `<workspace>/<project>` (a `.gitmodules` in the parent) — a test's own tmp
  project driven by `pytester` is not a run of the project — and never inside a Jenkins build
  (`JENKINS_URL` set): the local Jenkins builds in the project folders themselves
  (`customWorkspace`) with the workspace `.venv`, so the plugin loads there too, but Jenkins
  keeps its own JUnit record and the report reads that one. Each run appends ONE write to
  `aisland/data/test-stats/<project>/<date>.jsonl`: a line for the run (which leg — `fast`,
  `slow`, `all`, `other` — from the `-m` expression; whether it was the whole leg; wall seconds;
  exit status; commit) and a line per test (seconds over setup, call and teardown; the worst
  phase's outcome; its `slow` mark). Day files past 90 days are removed as a run is written.
- **The plugin never costs a run its result.** It imports only the standard library (it loads in
  every pytest start, so not `aisland.manager` with click and yaml; it resolves the data folder
  the same way, pinned by a test), and a record it cannot write is one printed line.
- **The report, `aisland tests report <project>`,** merges the local lines with the Jenkins
  builds' JUnit records — a nodeid lands on the same test as CI's `classname::name` through
  `junit_address`, the mapping pytest's own JUnit writer uses — and answers the rule's
  questions: the median of the complete whole fast runs against the 300-second budget, the
  slowest fast tests, the tests that failed, the never-failed fast tests that would cover an
  overrun (slowest first), and the slow tests that failed. `--json` prints the same
  `StatsReport` as data.
- **The report proposes, it does not move.** A test changes leg by an edit to its
  `@pytest.mark.slow` (or a reference file to a `FAST_SAMPLE`) that a person reviews; the plugin
  never marks a test at collection time from the record. Rejected: deselecting tests dynamically
  from their history — the leg a test runs in would change between two runs of the same commit,
  and nobody could read from the code which tests the fast run holds.
- **Rejected: CI only.** It would leave the "breaks" column almost empty (above). **Rejected: a
  committed statistics file** — every test run in every session would dirty the tree.

### Consequences

- A run in a workspace project costs one `git rev-parse` and one file append at its end.
- The records are per machine and git-ignored (`data/`): the report on one machine knows that
  machine's runs plus its Jenkins.
- A partial run (a file, `-k`, `-x`, `--lf`, `--sw`, `--deselect`) still records its tests' times
  and outcomes, but its wall time never counts as the fast run's.
- An xdist worker records nothing; its controller records the run.
- The CI half fills because the local orchestrator builds every project at 01:00 each night
  (`NIGHTLY_CRON`, update 2026-10-06): development and `/ship` run only the fast run, so without
  a schedule nothing ran the slow runs at all. The timer sits in the job's properties (armed when
  the job is seeded) and in the pipeline's `triggers` (re-applied at each run). A machine asleep
  at 01:00 misses that night. Only that run asks for the slow tests: a manual build is the fast
  run (user ruling 2026-10-06). The project jobs learn which run they belong to from a
  `SLOW_TESTS` parameter the orchestrator passes — true when its build cause is the timer —
  because a downstream job cannot see its upstream's cause; rejected: a second, nightly-only
  orchestrator job (two lists of projects to keep in step).
- A night's failure is reported by Jenkins, and the tests are moved by Claude (user choice
  2026-10-06). `aisland tests nightly` (`aisland/nightly.py`), run by the
  `<workspace>.nightly-check` job at 09:30 on weekdays, writes `aisland/data/nightly/<date>.md`,
  notifies and turns red when a night failed or had no run — it runs whether or not the Claude
  app is open, and its result shows in Jenkins beside the night it judges. Moving a test between
  the runs needs judgment (a timeout under load is no caught break), so the Claude app's
  scheduled task does that at 09:45 from the report. Rejected: the Claude task alone (it runs only
  while the app is open, and Jenkins showed nothing); Jenkins alone (no judgment for the moves).
- One run is disabled with `-p no:aisland_test_stats`.

### Example

```console
$ aisland tests report streamstudio
streamstudio - the last 30 days: 4 local runs, 3 Jenkins builds
Fast run: 6m 10s (median of 2 whole runs; last 6m 02s) - budget 5m 00s, 1m 10s over

Move to the slow run (never failed, slowest first) to fit 5m 00s:
      40.2 s  tests/test_app.py::TestPaletteAndForm::test_the_form_gives_every_node_its_parameter_schema
      36.8 s  tests/test_app.py::TestPaletteAndForm::test_the_palette_lists_at_least_recordstreams_own_classes
```

```python
# What an MCP tool wrapping the report returns: the same object as data.
from aisland.pytest_stats import build_report

build_report("streamstudio", days=30).to_dict()
# {"project": "streamstudio", "fast_run_seconds": 370.0, "to_slow": [{"test": "...", ...}], ...}
```

### What you may change

- The budget (`FAST_RUN_BUDGET_SECONDS`), the retention and the default window — the budget only
  with the user, since it is their ruling.
- What the report lists and how it words it (`render_report`), with its tests in
  `tests/test_pytest_stats.py`.
- What must not change: the plugin records only workspace projects' own runs, never fails a run,
  and never moves a test between legs by itself; the leg of a test is the code's
  `@pytest.mark.slow`.

---

## `pull-all` settles a submodule pointer only to a commit holding both sides

*2026-10-06*

### Context

The root records one commit per submodule, and several machines and sessions commit in the same
submodules and record them. `git pull` cannot bring that together. It moves each submodule only to
the commit the root records, so a commit pushed to a submodule that nobody recorded is never
pulled. And when both sides of a root merge recorded a new commit of the same submodule, the merge
stops on `CONFLICT (submodule)` even when the submodule merges cleanly; git prints the
submodule's merge commit as "a possible merge resolution" and leaves it to a person. The earlier
one-line alias pulled each submodule and then the root, so it walked into that conflict and,
re-run, stopped at once on `Pulling is not possible because you have unmerged files`.

### Decision

`git pull-all` is a script, `git_pull_all.sh`, that the alias runs from the workspace root
wherever it is typed. It fetches everything first (in parallel, before anything merges), merges
each submodule on the root's branch, then the root, then records pointers. Three rules:

- **A pointer conflict is settled to the submodule's checked-out commit only when that commit
  contains both sides** (`merge-base --is-ancestor` with stage 2 and stage 3). That is exactly the
  commit git suggests; any other case stays a conflict for a person.
- **A pointer is recorded only for what came from origin.** A submodule is recorded when origin has
  commits the root's pointer lacks and the checkout contains them, and the root holds everything
  its own origin has. A pointer never moves backwards. A submodule whose only unrecorded commits are
  local is left for whoever made them.
- **What it cannot do is listed, and a rerun continues.** A submodule left mid-merge holds back the
  root (its pointer depends on it). A root merge left on pointer conflicts, by this script or by a
  plain `git pull`, is settled and committed by the next run.

`--autostash` sets aside only the files the merge needs and puts them back. A put-back that clashes
renames the kept stash entry (`… (applied with conflicts)`), so no later run applies it twice.

Merges are `git fetch` + `git merge`, never `git pull`: a `pull` honours each machine's
`pull.rebase`/`pull.ff` (unset, a diverged pull refuses with "Need to specify how to reconcile
divergent branches"), and under `submodule.recurse` it checks the recorded pointers out into the
submodules. Measured: a submodule on `dev/main` with a local commit went to
`branch='' 05b1514 wfA`, detached at the recorded commit, its local commit on no branch.

Rejected: resolving pointer conflicts to "ours" or "theirs". Either can drop the other machine's
submodule commits from the record. Also rejected: a Python `aisland` command. The alias must work
before the `.venv` exists, and from a submodule's shell, where `aisland` is not on `PATH`.

### Consequences

- Two machines that both recorded a submodule pull each other without a manual step. Only real
  content conflicts reach a person, and the `/pull-all` workflow resolves those too.
- `pull-all` makes commits: the root's merge and one pointer-record commit. The root is then ahead
  of origin until pushed, and `push-all` pushes the submodules before the root, so the root on origin
  never records a submodule commit origin does not have.
- The script runs under macOS's `/bin/bash` 3.2: no `wait -n`, associative arrays or `mapfile`. The
  tests run it there.

### Example

```console
$ git pull-all                       # both machines recorded navigaitor
fetching the root and 25 submodules…
navigaitor             merged origin/dev/main (1 commit)
root                   pointer conflict navigaitor → 6cb3d7f (holds both sides)
root                   merged origin/dev/main (1 commit)
```

```bash
# the settle rule, as git_pull_all.sh applies it to each conflicted gitlink
ours=$(git rev-parse :2:navigaitor); theirs=$(git rev-parse :3:navigaitor)
head=$(git -C navigaitor rev-parse HEAD)
git -C navigaitor merge-base --is-ancestor "$ours" "$head" &&
    git -C navigaitor merge-base --is-ancestor "$theirs" "$head" &&
    git add navigaitor          # settled; otherwise it stays listed as CONFLICT
```

### What you may change

- The output wording, the fetch parallelism (`FETCH_JOBS`), and what is listed under `needs you:`,
  with `tests/test_git_pull_push_all.py`.
- What must not change: a pointer is settled only to a commit containing both sides and never moved
  backwards; a submodule's local-only unrecorded commits are never recorded by the pull; the merges
  never depend on `pull.*` settings; uncommitted edits are touched only under `--autostash`, and only
  the files the merge needs.

---

## Jenkins runs on the Java its WAR accepts, by full path

*2026-10-07*

### Context

Jenkins runs only on the long-term Java releases its launcher names, and refuses any other
before it starts:

```text
$ java -jar aisland/bin/jenkins.war --version        # java 26 first on PATH
Running with Java 26 from /opt/homebrew/Cellar/openjdk/26.0.2.1/…, which is not yet fully supported.
Run the command again with the --enable-future-java flag to enable preview support for future Java versions.
Supported Java versions are: [21, 25]
```

aisland declared Homebrew's `openjdk` as a system dependency and started Jenkins with the bare
`java`. That formula follows the newest Java, and `aisland setup`'s `brew install openjdk`
upgrades it. Homebrew's own caveat tells users to put that keg first on `PATH`. On 2026-10-06
setup moved it to 26.0.2.1. The supervisor ran Jenkins three times, each exited 1, and the only
trace was `Exited exit_code=1 process=jenkins` in the supervisor log. A Java 25 JDK was
installed on the same machine the whole time, and Jenkins never looked at it. The breakage
recurs every six months, whenever Java moves past the releases the downloaded WAR knows.

### Decision

`services/jenkins_java.py` asks the WAR. Every installed JDK is found: `$JAVA_HOME`, `java` on
`PATH`, each Homebrew `openjdk*` keg, `/Library/Java/JavaVirtualMachines/*`, `/usr/lib/jvm/*`. Its
version comes from its `release` file, so finding JDKs starts no JVM, and a JDK reached by several
paths is listed once. The JDKs are probed newest first with `java -jar jenkins.war --version`. The
first one the launcher accepts (exit 0, no "Supported Java versions" line) is the one Jenkins and
its plugin manager run on, by the path it was found under (`/opt/homebrew/opt/openjdk@25/bin/java`;
the `opt` link survives a `brew upgrade`).

- **No Java version is written into aisland.** The releases come from the launcher's refusal
  line, and the Homebrew formula to install is `openjdk@<the newest one named>`.
- **`aisland jenkins start` refuses up front** when no JDK is accepted: it prints the refused JDKs
  and the install command, then exits 1 before the supervisor is touched.
- **macOS provisioning installs the named formula** (`JenkinsService.provision_java`, called by
  `provision_system`). The WAR names its releases only when some JDK can run its launcher, so a
  machine with no such JDK (none at all, or only Java 8) first gets the newest `openjdk@N` to ask
  with. Setup goes on when the WAR cannot be downloaded or no JDK is accepted; it never exits.
- **The bare `openjdk` formula is never a sys-dependency** (a test reads aisland's
  `pyproject.toml`).

Rejected:

- `openjdk@25` written into the sys-dependencies. It is correct today, and it breaks silently
  once a re-downloaded WAR (`war-stable/latest`) drops 25.
- `--enable-future-java`. Jenkins calls it preview support, and plugins are not tested on
  non-LTS Java.
- Reading the supported releases from `executable/Main.class` without a JVM. That is parsing
  bytecode constants: fragile, for a 0.1 s saving.

### Consequences

- Jenkins keeps running whatever `java` the user's shell prefers. The user's `PATH` and the bare
  `openjdk` keg are left alone.
- Each start probes one JDK or more, about 0.1 s each, newest first, stopping at the first
  accepted.
- Setup downloads the WAR (about 95 MB, once) to ask it. That download used to wait for the
  first `aisland jenkins start`.
- On a machine with no JDK that can ask, setup installs one JDK Jenkins may then refuse (the
  newest `openjdk@N`, 27 on 2026-10-07) beside the one it accepts.
- Linux installs no Java; start and the doctor name the release to install.

### Example

```python
from aisland.services.jenkins import JenkinsService

choice = JenkinsService().resolve_java()          # probes the installed JDKs, newest first
choice.describe()   # 'Jenkins 2.555.2 runs on Java 25.0.3 (/opt/homebrew/opt/openjdk@25/bin/java)'
choice.supported    # (21, 25) — named by the launcher when it refused 26.0.2.1
```

```text
# the same machine with Java 25 removed: `aisland jenkins start` exits 1 before starting anything
Error: Jenkins 2.555.2 runs on Java 21 or 25, and none is installed (found 26.0.2.1, 11.0.29).
  Java 26.0.2.1: /opt/homebrew/opt/openjdk/bin/java
  Java 11.0.29: /Library/Java/JavaVirtualMachines/amazon-corretto-11.jdk/Contents/Home/bin/java
Fix: brew install openjdk@25
```

### What you may change

- Where JDKs are looked for (`candidate_javas`, `SYSTEM_JDK_GLOBS`), the message wording, and the
  probe timeout, with `tests/test_jenkins_java.py`.
- What must not change: Jenkins starts by full path on a JDK the WAR accepted; the WAR is the only
  source of the supported releases; the bare `openjdk` formula is not declared; provisioning never
  exits setup.

## A project installed outside the workspace gets its unpublished dependencies as wheels

*2026-10-08*

### Context

A project that is not on PyPI (navigaitor, say) has to be installable on a machine that has
neither the workspace nor access to the workspace's GitHub repositories. Its dependencies are
workspace projects of two kinds: some are on PyPI (confluid, recordstream), some are not (a
sibling not yet released). The second kind has to travel with the project. And because those
wheels are built from this checkout, they can use code that the PyPI releases of the first kind
do not hold yet — measured on 2026-10-08: a sibling's module imported `recordstream.draws`, which
recordstream 0.1.0a2 on PyPI lacks, so an install that resolves fine fails at `import`.

### Decision

`aisland wheels <project>` (`aisland/wheels.py`, user choice 2026-10-08: an aisland command
rather than a root script; only what the project needs; both checks below):

- **What it needs** is the closure of its `[project] dependencies` over the workspace's
  projects: base dependencies, plus an extra only when a requirement names it. Every marker is
  followed, because the target machine's platform is not this one's.
- **On PyPI** means a version, pre-releases included, whose metadata points at
  `github.com/Gearlux/`, with a file that is not yanked. This differs on purpose from
  `sources.released_on_pypi` (final releases only): that one decides what CI installs, while this
  one asks what pip on another machine will find, and pip takes a pre-release when no final
  release exists.
- **Each wheel is built from `git archive HEAD`**, with `SOURCE_DATE_EPOCH` set to the commit's
  time. Building from the folder put a file that only lived in a stale `build/lib` into the
  wheel (measured), and without the timestamp an unchanged project rebuilt into a different
  file, so git saw every wheel change on every run (measured). A dirty project is refused,
  because its wheel would silently lack the change.
- **The PyPI half is checked twice.** First, each PyPI wheel is compared with the wheel its
  checkout builds, file by file plus `Requires-Dist`; a difference is reported as *behind*.
  Second, the project is installed editable in an empty venv from its wheels plus PyPI, and every
  module of the project and of its wheels is imported (`__main__` excepted: importing it runs the
  program). A failed import names the project that owns the missing module, which is the one to
  *release first*. Only the second
  check decides the exit status: being behind is normal between releases, while a failed import
  means the project cannot be used.

Rejected:
- **Direct `file:` references in the project's dependencies.** PEP 508 has no relative path, so
  they would carry this machine's absolute path into a committed file.
- **A local version label per build** (`0.1.0+<n>.g<sha>`) to make reinstalls automatic. It
  would put a second version into the files, against the rule that the version lives once, in
  `pyproject.toml`. A reinstall is one documented command instead.
- **Every workspace project by default.** Wheels the project never imports (aisland,
  navigaitor, torpaido) would be shipped and committed on every rebuild. `--all` builds them on
  request (next record).

### Consequences

- A run needs PyPI (an unreachable PyPI stops it before anything is built, because a guess would
  ship a wheel too many or one too few) and takes about 35 s with uv's cache warm.
- The import check imports every module, because a module no package imports is otherwise
  never checked (measured 2026-10-08: importing only the outermost packages of a project with
  unreleased dependencies found 2 modules missing `recordstream.draws`; importing every module
  found 9). The optional extras' modules, whose
  dependencies the empty venv does not install, are kept from failing the run by one rule: a
  package that fails, or any module missing a workspace project's module, is a break (exit 1);
  a deeper module missing something no workspace project ships is listed under "Not imported"
  and breaks nothing. Only a `ModuleNotFoundError` is that: a `TypeError` from the liquifai
  release on PyPI (0.3.1: `LiquifyApp.command() got an unexpected keyword argument
  'config_files'`) was once filed there, though it is the outdated release's doing — with
  `--include-published` the module imported. Any other error of a deeper module is therefore a
  break once a published project is behind, blamed on one project when it can be: the
  traceback's innermost frame in a behind project's code, else the behind projects whose
  checkout-only code uses a name the message quotes (a bad keyword is raised at the call, so
  the callee has no frame), else all of them. The cost of that rule: a deeper module missing an undeclared third-party
  dependency is only listed, never a failure. A package that fails is reported alone, since the
  modules under it cannot be reached.
- A child project that ships inside its parent package (the `extend_path` shim) is imported by
  its dotted name. Its wheel is detected as a child either by the missing parent `__init__.py`
  or by a parent `__init__.py` that only extends `__path__`.
- The editable install in the empty venv writes the target's own `*.egg-info`, which is
  git-ignored like the workspace's own install of it.

### Example

```text
$ aisland wheels navigaitor            # exit 0
...
Installed navigaitor in an empty venv (its wheels + PyPI) and imported every module of its 1 package:
  17 modules imported

Not imported, missing what no workspace project ships (an optional extra's?):
  navigaitor.server         ModuleNotFoundError: No module named 'sonair'
  navigaitor.visualisation  ModuleNotFoundError: No module named 'matplotlib'

Behind, nothing breaks: confluid, liquifai, recordstream
```

```python
from aisland import wheels
from aisland.manager import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "navigaitor")
report.release_first      # []: a module missing a workspace project's module would name it here
report.to_dict()["ok"]    # True: what an MCP tool would return
```

### What you may change

- The report's wording and layout, with `tests/test_wheels.py::TestReport`.
- How packages to import are found (`wheel_modules`) and how their modules are walked, keeping
  the rule that a module missing what no workspace project ships breaks nothing.
- What must not change: wheels are built from the commit, never from the folder; their
  timestamp is the commit's time; a dirty project is refused; the empty venv never reads the
  workspace's root `uv.toml`; the exit status follows the install and the imports, not *behind*.

## What `aisland wheels` builds is a scope, and the install is one generic script naming each wheel by its file

*2026-10-09*

### Context

Shipping only what PyPI lacks leaves the other machine at the mercy of the PyPI releases: on
2026-10-08 a project's modules could not import because recordstream 0.1.0a2 on PyPI lacked
`recordstream.draws`. The user asked for an option that ships the published dependencies as
wheels too, and one that ships every workspace project. A wheel built from a checkout ahead of
its release carries the SAME version as the release, and the old install line
(`pip install --find-links wheels -e .`) then took PyPI's: measured 2026-10-09 in an empty venv,
`pip install --find-links wheels recordstream` installed PyPI's 0.1.0a2 (`recordstream.draws`
missing), while `uv pip install --find-links wheels recordstream` installed the folder's. Named
by its file (`pip install --find-links wheels wheels/recordstream-0.1.0a2-py3-none-any.whl`), pip
installed the folder's.

### Decision

- **A run has a scope** (`WheelsScope`, user choice 2026-10-09): `unpublished` (the default, the
  previous record), `needed` (`--include-published`: every project the target needs that has a
  checkout here) and `all` (`--all`: every workspace project that has one). A project the scope
  covers without a checkout is not built and is said so; a needed one comes from PyPI (user
  choice: report and go on, rather than stop). A dirty project is refused in every scope,
  including one `--all` builds although the target does not need it (user choice).
- **`wheels.yaml` lists the wheels; its `install:` values are the user's.** Each entry carries the
  wheel, the commit, `needed`, the version PyPI has and `install:`, which defaults to `needed`. A
  rerun reads the file it replaces and keeps every `install:` the user set (user choice); a file
  it cannot read stops the run at its line before anything is built, because rewriting it would
  drop the choices.
- **`install.py` is one generic script** (`aisland/wheels_install.py`, copied under a generated
  header): it reads `wheels.yaml`, chooses (`install: true` by default, `--all`, `--needed`,
  `--only`) and runs ONE `pip install --find-links wheels -e <project> <each chosen wheel's file>`
  in the interpreter running it — the file names are what make the folder's wheels win. It
  installs PyYAML first when it is missing, and passes everything after `--` to pip or uv.
- **The empty-venv check runs that script** (`--installer uv --needed`), so what is checked is what
  the other machine runs, and the target's private index (`uv.toml`) still reaches the install.

Rejected:
- **`pip install wheels/*.whl`.** Under `--all` it would install every workspace project with its
  dependencies (raidar and matrainer pull torch and Lightning).
- **A local version label per build**, again (previous record): naming the files solves the
  same-version choice without a second version in the files.

### Consequences

- The check installs through uv while the user's default is pip. The two choose differently only
  between a found and an indexed wheel of one version, and the script names files, which both
  take: `tests/test_wheels_install.py` pins pip's side with a real offline install against a local
  PEP 503 index (the index wins over `--find-links`; the named file wins over the index).
- The wheels folder of a `--include-published` run holds the workspace's commit of every needed
  project; the project then no longer depends on the PyPI releases being current.
- The user's pip configuration reaches every pip run: an `extra-index-url` in `~/.pip/pip.conf`,
  which `--index-url` does not replace, answered a test's `libx` with another project's package
  needing pandas (checked with `pip config debug`, 2026-10-09). The pip tests run with
  `PIP_CONFIG_FILE=/dev/null` and a package name nobody publishes.

### Example

```bash
aisland wheels navigaitor --include-published
python wheels/install.py --dry-run      # on the other machine, from navigaitor/
# $ python -m pip install --find-links …/wheels -e …/navigaitor …/wheels/confluid-0.4.1-py3-none-any.whl …
```

```python
from aisland import wheels
from aisland.manager import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "navigaitor", scope="needed")
[(b.project, b.on_pypi) for b in report.built][:2]   # [('confluid', '0.4.1'), ('liquifai', '0.3.1')]
report.not_built                                      # []: a needed project without a checkout here would be listed
```

### What you may change

- The script's flags and the YAML's fields, together with `aisland/wheels_install.py`'s
  `read_wheels` (the one reader both sides use) and both test files.
- What must not change: the install names each chosen wheel by its file; the check runs the
  shipped script; a rerun keeps the user's `install:` values; a dirty project is refused in
  every scope unless the run is told `--force` (record "`--force` builds a dirty project from its
  working tree").

## The wheels folder is the machine's: ignored unless `--commit`, carried as one self-installing zip (2026-10-18)

### Context

A `--all` build of one project is 26 wheels and 5.1 MB, rebuilt whenever a dependency moves. The folder sat
in the project's tree with nothing ignoring it, so every `/commit-all` swept the wheels into history, and the
user wanted them out unless asked for. The wheels also have to reach a machine that has neither the
workspace nor aisland (`aisland-manager` is not on PyPI), so carrying them must not need aisland there.

### Decision

User choices 2026-10-18: the opt-in flag commits by itself; both command shapes; a project's
already-tracked wheels untracked.

- **Every run writes `wheels/.gitignore` holding `*`.** The folder ignores itself, the ignore file
  included, so git never sees it. A run that finds files of the folder still tracked prints the
  `git rm -r --cached` that stops tracking them; it never runs it.
- **`--commit` writes `*.zip` instead and commits `wheels/` in the target itself** — only when the
  empty-venv check passed, and only that path (`git commit -- wheels`): another session's staged or
  unstaged work in the project stays out. The message names the build command. The zip is never
  committed.
- **`aisland wheels zip` packs the folder with a generated `__main__.py`** (`aisland/wheels_unzip.py`,
  standard library only), so `python <project>-wheels.zip` unpacks into `./wheels` and runs its
  `install.py` with the rest of the line. It refuses a folder whose `pyproject.toml` names another
  project before unpacking anything. Members are written in name order with one fixed time, so a zip of
  an unchanged folder is the same file.
- **`aisland wheels install ZIP --python PYTHON`** runs that zip in another interpreter; the workspace's
  `.venv` is refused (install.py installs with pip, and the workspace installs through `aisland setup`).
- **`aisland wheels` is a group** (`build`, `zip`, `install`); a first word that names no command is the
  project to build, and `--install ZIP` on the group is the short form of `install`.

Rejected:
- **A flag that only leaves the folder visible to git** (`--track`): the user wanted the command to
  commit.
- **Installing from the zip through aisland only**: the machine that needs the zip has no aisland.

### Consequences

- A project whose wheels were committed before keeps them in history; untracking stops new ones only.
- `--commit` creates a commit without `/commit-all`; it is the one aisland command that commits in a
  project.

### Example

```bash
aisland wheels build navigaitor --all --zip
# … Zip: navigaitor/wheels/navigaitor-wheels.zip (…) — install it from the navigaitor folder with: python navigaitor-wheels.zip
cd ~/src/navigaitor && python ~/Downloads/navigaitor-wheels.zip --dry-run   # the other machine
```

```python
from aisland import wheels
from aisland.manager import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "navigaitor", commit=True, make_zip=True)
report.committed, report.commit_note, report.zip_file
```

### What you may change

- The zip's name, its `__main__.py` options and the report's wording, with `tests/test_wheels_zip.py`.
- What must not change: the folder is ignored unless `--commit`; `--commit` commits only `wheels/`, only
  after a passing check, never the zip; the zip installs without aisland and refuses another project's
  folder; `install` refuses the workspace's `.venv`.

## A mirror is push-only, fast-forward-only, and copies the primary (2026-10-16)

### Context

Some submodules must be visible on two hosts — a GitHub organisation and a Bitbucket server —
while work keeps happening in one place. Git's own answer, several push URLs on `origin`, makes
every `git push` go to both, but a rejection by the second host is one `! [rejected]` line in
the push's output, and that host then falls behind without anything reporting it. Two-way
syncing (pulling from both) would make a second primary, with merges between the hosts.

### Decision

- The primary is the `.gitmodules` remote and nothing changes about it. The mirror is a second
  remote, named `mirror`, that only receives.
- `aisland mirror sync` pushes the PRIMARY's `dev/main`, `main` and tags (read with
  `git ls-remote origin`, the missing objects fetched by id), never the local branches.
- It never forces and never deletes: a branch the mirror has commits on that the primary lacks
  is refused with the commit and the merge command; a differing tag is refused; a mirror-only
  branch or tag is left alone.
- `/push-all` and `/ship` run it as their last step (user choice 2026-10-16, option A), so a
  refused or unreachable mirror is a row in their report, not a lost line of git output.
- Two layers like `aisland source`: committed `[mirrors.<project>] url` and the per-machine
  `aisland.mirror.<project>`. Only submodules (user choice 2026-10-16).
- The remote is marked `remote.mirror.aislandManaged`; setup and `unset` remove only a marked
  one, and `set` refuses to take over an unmarked one.
- Every network call runs with `GIT_TERMINAL_PROMPT=0`.

### Consequences

- The hosts can differ only by the mirror being behind; when someone pushes to the mirror
  directly, the sync stops for that branch until the commit is merged into the primary.
- A plain `git push` in a terminal reaches only the primary; the mirror catches up on the next
  sync.
- A sync costs four network round trips per project (two `ls-remote`, a fetch, a push).

### Example

```bash
aisland mirror set navigaitor git@github.com:Org/navigaitor.git
aisland mirror sync
```
```
==> navigaitor → git@github.com:Org/navigaitor.git
    dev/main  pushed (3 commits)
    main      in sync
    tags      pushed v0.4.0
```

### What you may change

- `MIRRORED_BRANCHES` in `aisland/mirrors.py`, with the README section and `tests/test_mirrors.py`.
- What must not change: no force, no delete, no tag overwrite; the primary's state is what is
  copied; an unmarked `mirror` remote is never touched.

---

## `--force` builds a dirty project from its working tree, and says so (2026-10-20)

### Context

A wheel is built from its project's last commit, and a project with changes no commit holds is
refused, so no one ships a change believing it is in. In a workspace where several sessions work at
once that refusal blocks every build that covers a project someone else is editing: a run over every
project with a checkout stopped on three projects with work in progress, though the person building
wanted exactly that work on the other machine.

### Decision

`aisland wheels build … --force` (user choice 2026-10-20) builds such a project from its files as they
are (`export_worktree`), not from its commit:

- the files are the ones git tracks plus the untracked ones it does not ignore
  (`git ls-files --cached --others --exclude-standard`): an edit and a new module are in, a deleted file
  is out, and an ignored setuptools `build/lib` stays out — the reason wheels were never built from the
  bare folder still holds;
- its commit is written `<sha>+uncommitted` wherever a commit is written (the report's table,
  `wheels.yaml`, the folder's README), and its `git status --porcelain` lines go into the report
  (`BuiltWheel.uncommitted`, a text block "Built with changes no commit holds");
- a clean project is still exported from `git archive HEAD`, so `--force` on a clean workspace builds
  the same files as a run without it;
- under `--force` a dirty target is exported from its working tree too, so the import check reaches its
  new modules (it is installed editable from its folder anyway);
- `--force --commit` is refused before anything is built: a committed wheel must be one its commit
  reproduces.

Rejected: building the last commit and leaving the changes out. It keeps every wheel reproducible, but
the person who forces a build wants the work in progress shipped; leaving it out silently is the
mistake the refusal exists to prevent.

### Consequences

- A forced wheel is not reproducible from any commit; the `+uncommitted` mark is the only record. Its
  timestamp is still the last commit's, so a rerun over unchanged files builds the same file.
- Without `--force` nothing changes: the refusal names the files, and now also the flag.

### Example

```python
from aisland import wheels
from aisland.manager import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "navigaitor", scope="all", force=True)
[(b.project, b.commit, b.uncommitted) for b in report.built if b.uncommitted]
# [('navigaitor-dependency', '1a2b3c4+uncommitted', [' M pkg/module.py', '?? pkg/new.py'])]
```

### What you may change

- The wording of the mark and of the report's block, together with the tests that read them.
- What must not change: a dirty project is built only under `--force`; the working tree's files are the
  ones git lists, never the bare folder; a forced wheel is marked; `--force` never goes with `--commit`.

## From a checkout aisland finds everything from its own place; from a wheel, from where it runs (2026-10-19)

### Context

Every path aisland uses was found from the package's own file: the workspace by walking up from
`aisland/aisland/manager.py`, the state as `aisland/data/` beside the package, the git scripts as
`<root>/aisland/…`, the CI scaffolder as `parents[3]`. That is right for the checkout it was
written in, and meaningless for a wheel. Measured in an empty venv, run from another project's
folder, before this record:

```text
workspace root: …/venv/lib/python3.13
DATA_DIR      : …/venv/lib/python3.13/site-packages/data
template      : …/site-packages/code-workspace.json False
```

The workspace that owns this checkout must keep working exactly as it does (user ruling
2026-10-19), and a wheel must find a workspace of the user's choosing.

### Decision

One standard-library module, `aisland.locations`, decides, and every path asks it:

- **Which installation is this?** `FROM_CHECKOUT` is aisland's `pyproject.toml` beside the package.
  A wheel never carries one, an editable checkout always has it. No setting, no environment
  variable: the answer is a fact about the files.
- **From a checkout**, the old rule, moved verbatim (`checkout_workspace_root`), and the state in the
  checkout's own folder. The cwd and `AISLAND_WORKSPACE` are not read.
- **From a wheel**, `AISLAND_WORKSPACE` when set, else `find_workspace` (the next record: a folder of
  projects is a workspace too); the state in `<root>/.aisland/`.
- **Nothing raises at import.** The module constants (`manager.DATA_DIR`, `supervisor.BIN_DIR`, the
  pytest plugin's `STATS_DIR`) are computed once per process; outside a workspace they fall back to
  the cwd, and the CLI's top-level group refuses every command that works on a workspace before it
  writes anything. Creating a workspace (`aisland workspace init`) and installing wheels elsewhere
  run anywhere.
- **What a wheel cannot find beside itself, it carries.** The editor template, the git scripts and
  `init`'s templates are package data; `project.bashrc` stays at `aisland/project.bashrc` (its place
  there makes the root its parent) and `setup.py` copies it into a wheel, from which `aisland setup`
  writes it to `.aisland/` — where its parent is again the root.
- **The scripts take the root from outside.** `setup_git.sh [root]`; installed elsewhere it writes a
  `pull-all` alias that names `git_pull_all.sh` by its path and passes the root in
  `GIT_PULL_ALL_ROOT`. In the checkout the alias keeps its old form.

Rejected: resolving every path from the cwd in both modes (a checkout run from `/tmp` or another
repository would act on that folder — the ruling forbids the change); turning the path constants
into functions (dozens of call sites and default arguments, for a value that never changes within
one process); a marker file (`aisland.toml`) a workspace must hold (one more file to know about,
when a git repository already marks one).

### Consequences

- A checkout's behaviour is pinned path by path (`tests/test_locations.py`), so a change that lets
  the cwd leak into checkout mode fails there.
- From a wheel, the aliases point into the venv: recreating it needs `aisland git aliases`.
- aisland's own system tools (process-compose, Homebrew's bash) come from its `pyproject.toml`
  `sys-dependencies`, which a wheel workspace does not discover: they are the user's to install.
- The editor template is written for `acme`; its name is replaced by the workspace's, so the
  template must keep naming the workspace only as `${workspaceFolder:acme}` and `acme-bash`.

### Example

```text
$ aisland workspace init acme            # aisland from a wheel
==> Created workspace acme in /Users/you/acme
$ cd acme/models && python -c 'from aisland import manager; print(manager.DATA_DIR)'
/Users/you/acme/.aisland/data
$ git config alias.pull-all
!GIT_PULL_ALL_ROOT="$(git rev-parse --show-superproject-working-tree --show-toplevel | head -n 1)" bash "/Users/you/acme/.venv/lib/python3.13/site-packages/aisland/git/git_pull_all.sh"
```

### What you may change

- The wheel-mode rule (`find_workspace`) and the state folder's name (`STATE_FOLDER`), with their
  tests.
- Which commands run outside a workspace (`cli._WORKSPACE_FREE`).
- Never: the checkout's rule, or anything that makes a checkout read the cwd or `AISLAND_WORKSPACE`.

---

## A folder of projects is a workspace: its subfolders are read as submodules (2026-10-19)

### Context

Every project list aisland kept came from `.gitmodules`. A folder of projects without one — plain
folders, or each project its own git clone — was no workspace at all. Measured with aisland from a
wheel in `acme/{alpha,beta}` (beta needing alpha), no git anywhere:

```text
$ aisland status
Error: No workspace here: …/acme is not inside a git repository. …
$ AISLAND_WORKSPACE=…/acme aisland wheels build beta
Error: no project 'beta' in the workspace …/acme
```

The user asked for such a folder to work "as if that directory contains all subdirectories as
submodules", `git pull-all` and `git push-all` included.

### Decision

- **The project list** is `manager.workspace_folders`: `.gitmodules`' submodules when it exists, else
  every direct, non-hidden subfolder holding a `pyproject.toml`, active, named by its folder. Readers
  of "the projects" (wheels, the test statistics) use it; `submodule_active_map` stays what it says —
  submodules — for what only a submodule has (activation, deinit, a `.gitmodules` remote).
- **Finding the workspace** (`locations.find_workspace`), first hit upwards from where aisland runs:
  `.gitmodules`; the start folder when it holds projects; a folder holding a `pyproject.toml` (a
  project — the workspace is its parent, so a project that is its own clone is never the
  workspace); a `.git` folder (a repository with no project yet). The "holds projects" test looks at
  the start folder only.
- **What needs a submodule is refused, naming the reason**: a `github`/`pypi` source empties the
  folder and has nothing to check it out again from; a wheel is built from a commit, so a project in
  no git repository has none.
- **The git aliases**: without `.gitmodules`, every subfolder that is its own clone, and the root
  when it is a repository, gets `pull-all` and `push-all`, the root written into the alias (from
  inside a clone git cannot find it). `pull-all` merges each clone with origin on its OWN branch —
  independent clones share none, where submodules follow the root's — then the root; `push-all`
  pushes each clone, then the root only when all pushed. The other aliases (`status-all`,
  `switch-all`, `merge-all`, `tag-all`, `safe`) fan out with `git submodule foreach` in a
  superproject; here `git_folder_all.sh <verb>` loops over the same clones, and `safe` takes the
  workspace's venv and runs mypy from its root, which a clone cannot find on its own.

Rejected: generating a `.gitmodules` for the folder (it would claim submodules git does not have,
and the root may not be a repository); every ancestor holding projects counting as a workspace (a
home folder holding one project would become the workspace of every folder under it); building a
wheel from a folder outside git (the wheels record's reason — a working tree carries what a commit
does not).

### Consequences

- A workspace with `.gitmodules` reads exactly as before; a stray non-submodule folder in it is
  still not a project.
- From a folder that is neither a project nor its parent (`acme/docs`), there is no workspace:
  `AISLAND_WORKSPACE` names it.
- The clones' aliases name the scripts and the root by path: moving the workspace or recreating the
  venv needs `aisland git aliases` again.

### Example

```text
$ cd acme/beta && aisland git aliases
==> Configuring Git Aliases (no .gitmodules: the clones alpha, beta)...
$ git pull-all
fetching 2 repositories…
alpha                  merged origin/main (1 commit)
```

### What you may change

- Which subfolders count as projects (`workspace_folders`, `locations.holds_projects` — keep the two
  the same test), with `tests/test_folder_workspace.py`.
- Which aliases a folder workspace gets, with `tests/test_folder_git_aliases.py`.
- Never: a second project list beside `workspace_folders`, or `.gitmodules` overruled by the folders.

## A workspace is written down in a file a machine without aisland reads (2026-10-20)

### Context

Setting a workspace up on another machine meant knowing three things nobody had written down: how to make a venv
aisland works from (the editable aisland of another workspace works on THAT workspace, wherever it is run — the
record above), how to install aisland into it, and every project's address and branch, typed one `--add` at a time.
`aisland workspace init --add` took no branch, so each project came on its default branch.

### Decision

One file, `<name>.workspace.toml`, holds both halves:

- **Comments for the machine that has no aisland yet**: the venv with uv and with pip (`--prompt <name>` names it;
  a venv kept elsewhere is linked in as `.venv`), aisland-manager from PyPI or from a wheel, then
  `aisland workspace init . --from <file>` and `aisland setup`. They are comments because nothing can read the file
  before aisland is installed.
- **`[projects]` for aisland**: one `name = { url = …, branch = … }` per project, `name` the folder it is cloned into.
  `init --from` adds each as a submodule on its branch (`git submodule add -b`), before any `--add`.
- **`export` writes it** from the workspace: `.gitmodules`' submodules, a branch of `.` written as the root's branch
  (a new workspace has no superproject branch to follow yet); without `.gitmodules`, each clone's `origin` and the
  branch it is on, the same reading of a folder workspace as the record above. A project folder that is no clone,
  and a clone without `origin`, are reported as left out.
- **TOML**, because the workspace's one committed config is TOML and the standard library reads it (aisland needs
  Python 3.12).
- **Read before anything is written.** A file aisland refuses leaves the folder as it was; every refusal names the
  file and the line — tomllib's position for a syntax error, `_line_of` for a project aisland refuses.

Rejected: a venv-name setting. `.venv` is read in some twenty places (setup, the services' PATH, the git aliases, the
generated editor and MCP files); a name of one's own is had with `--prompt` or a link, and `init`'s `.gitignore`
writes `.venv` rather than `.venv/` so a link is ignored too. Rejected: a script that runs the three steps — the file
is meant to be edited and read, and the steps differ per machine (uv or pip, PyPI or a wheel).

### Consequences

- The file is the user's after it is written: `export` never writes over it, and `init` never writes it.
- An inactive submodule is exported like any other; a machine without access to it gets the clone's error from
  `init`, naming its address — delete its line first.

### Example

```python
from pathlib import Path
from aisland import workspace

workspace.export_workspace(Path("~/work/acme").expanduser())        # → ~/work/acme/acme.workspace.toml
workspace.read_workspace_file(Path("acme.workspace.toml"))
# [FileProject(name='models', url='git@github.com:acme/models.git', branch='dev/main'), ...]
workspace.init_workspace(Path("."), from_file=Path("acme.workspace.toml"))
```

### What you may change

- The comments' wording (with `tests/test_workspace_init.py::test_an_exported_file_says_how_to_create_the_workspace_from_it`).
- A new project setting, added to `_PROJECT_KEYS`, written by `export` and read by `init` in the same change.
- Never: reading the file after `git init`, a refusal without its line, `export` over an existing file.

## Every workspace gets aisland's agent commands and rules (2026-10-20)

### Context

The workspace commands (`/commit-all`, `/commit-session`, `/list-actions`, `/pull-all`, `/push-all`, `/ship`) and the
rules agents follow lived in ONE workspace's root: its `.agents/workflows/` and its `AGENTS.md`. The commands already
depended only on aisland (`git pull-all`, `aisland mirror sync`, the install order), and most of the rules — evidence
before claims, agree the scope first, commit only on request, sessions coordinate before they edit — had nothing to
do with that workspace's projects. Another aisland workspace started with neither, so its agents behaved
differently, and every improvement had to be copied by hand.

### Decision

aisland ships the shared part in `aisland/harness/` and installs it into every workspace (`harness.install_harness`,
from `aisland setup` and `aisland workspace init`):

- **What:** `commands/*.md` → `.agents/workflows/` (`.claude/commands` links there), `scripts/*.py` →
  `.agents/scripts/`, `AGENTS.md` → `.agents/aisland/AGENTS.md`. Only what holds in any workspace; a test refuses
  any project, private or domain name.
- **How the rules reach an agent:** the workspace's own `AGENTS.md` starts with `@.agents/aisland/AGENTS.md` — Claude
  Code and Gemini CLI follow `@` imports, relative to the importing file — and keeps only its own rules below.
  Rejected: aisland writing the rules into a marked block of the workspace's `AGENTS.md`. Every agent would read it,
  including ones without imports, but the file would be half aisland's: an edit inside the block lost on the next
  setup, and no way to make the rules a link into a checkout.
- **Link or copy:** from a checkout each file is a relative link into the package, so an edit made where an agent
  reads it is an edit of aisland's file, reviewed and committed in aisland. From a wheel a link would point into
  `site-packages`, which a reinstall replaces, so each file is a copy.
- **A copy someone edited is kept** (user choice 2026-10-20): `installed.json` records each copy's fingerprint; a
  copy still equal to it (or to aisland's file) is replaced, anything else is reported and left. Rejected:
  overwriting with a report — it destroys the edit it reports.
- **Never the workspace's own files:** a command aisland does not ship is not touched; each installed file is
  ignored by its own path, not by its folder, so the workspace's own commands stay committable; an existing
  `AGENTS.md` is never edited — setup names the missing import line instead.

### Consequences

- A rule or command changes in one place for every workspace; a workspace on a wheel takes it with the next aisland
  release and `aisland setup`.
- An agent that reads `AGENTS.md` without following imports sees the import line, not the rules.
- Claude Code documents parent-folder discovery for `.claude/skills/` and `CLAUDE.md`, not for `.claude/commands/`:
  a session started inside a project folder reads the rules through the root's `CLAUDE.md`, but may not list the
  commands.
- A command dropped from aisland disappears on the next setup: its link is removed, an unedited copy deleted, an
  edited copy kept and named.

### Example

```python
from pathlib import Path
from aisland import harness

harness.install_harness(Path("~/work/acme").expanduser(), link=False)
# []  — or, after someone edited a copy:
# ["kept your edited .agents/workflows/ship.md — delete it to take aisland's"]
[name for _source, name in harness.shipped_files()]
# ['.agents/workflows/commit-all.md', ..., '.agents/scripts/stage_hunks.py', '.agents/aisland/AGENTS.md']
```

### What you may change

- A rule or a command, in `aisland/harness/` (never an installed copy); a command's citations and the rule's bold
  heading together (`tests/test_harness.py::test_every_section_a_command_cites_is_in_the_rules`).
- A new kind of shipped file: a folder under `harness/`, its pattern in `[tool.setuptools.package-data]`, its target in
  `shipped_files()`.
- Never: editing a workspace's `AGENTS.md`, overwriting an edited copy, ignoring `.agents/workflows/` as a folder.
