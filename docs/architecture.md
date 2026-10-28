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
  (`acme` → `acme-tools`) can only be read from `pyproject.toml`, which the deinit
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

>>> [(d, released_on_pypi(d, "Gearlux")) for d in ("liquifai", "recordstream", "purifai")]
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
// To regenerate:  aisland workspace code-workspace
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

## aisland's tools are its own MCP server, a chosen set (2026-10-21)

### Context

An AI client working in the workspace should check the environment, read test statistics, start Jenkins or MLflow
and set the editor's colours without parsing terminal text. navigaitor is the workspace's MCP server for the ML
libraries (confluid, liquifai, matrainer); the user ruled that aisland's commands never become navigaitor tools —
aisland runs where navigaitor is not installed, and it installs the projects navigaitor needs. liquifai, which turns
one signature into a command and a tool, is out for the same reason: `aisland setup` installs it.

### Decision

`aisland/server.py` is a stdio MCP server, `aisland-mcp`, behind the `[mcp]` extra. Its tools are a list chosen with
the user, not the CLI generated: seven that read and six that change this machine without destroying anything
(the editor's theme and colours, starting and stopping Jenkins and MLflow). Each returns one JSON object: the data
its command prints with `--json`, from the same function (`doctor.run_checks`, `pytest_stats.build_report`,
`sources.describe`, `tunnel.store_payload`, `editor.*`). A service tool runs the `aisland` command as a subprocess
(this interpreter, `-P`, the workspace root), so it starts a service exactly as a person does. The server drops
the resources/prompts handlers so it advertises tools only; it needs no schema sanitizing (flat arguments), so it
is not a copy of the `_LLMSafeMCPServer` the confluid-based servers share.

### Consequences

- What prompts, runs for minutes or deletes stays a command; an agent cannot `clean` or `reset`.
- `.mcp.json` lists the server through the existing discovery — no registration code.
- A print in a tool lands on stderr: the SDK's stdio transport serves the protocol from a private copy of stdout.
- The base install is unchanged: without the extra, `aisland-mcp` says what to install.

### Example

```text
→ tools/call editor_set_color {"role": "background", "color": "navy"}
← {"theme": "Monokai Pro", "background": "navy", "foreground": "white", "area": "frame"}
→ tools/call service_start {"name": "clearml"}
← error: Error executing tool service_start: … name: Input should be 'jenkins' or 'mlflow' …
```

### What you may change

- Add a tool: a function returning one JSON object, in `_READING` or `_CHANGING`, its command's `--json` the same
  data; `tests/test_server.py::READ_ONLY`/`CHANGING` name it.
- The services a tool controls (`SERVICES`).
- What must not change: no tool that prompts, runs for minutes or deletes; the extra stays optional; aisland's
  tools stay out of navigaitor.

---

## The editor's theme and colours are a per-machine choice laid over the generated workspace file (2026-10-21)

### Context

A window of one workspace should be told apart at a glance, or shown in a theme of its own. VS Code reads
`workbench.colorTheme` and `workbench.colorCustomizations` from a `.code-workspace` file, but aisland rewrites
that file on every `aisland setup` and `aisland source …`, so a choice made in VS Code at *Workspace* scope (or
by the Peacock extension, which writes there) is lost at the next setup. A colour is a personal choice: the user
ruled it must stay on the machine and never reach a committed file.

### Decision

`aisland/editor.py` keeps the choice in `git config --local aisland.editor.{theme,background,foreground,area}`
— the per-machine layer `aisland source --machine-only` already uses — and `manager.write_code_workspace` adds
`editor.workspace_settings(editor.read_choice(root))` over the template's settings on every write.

- The theme is what VS Code stores in `workbench.colorTheme`: a contributed theme's `id`, else its label
  (`settingsId = theme.id || label` in VS Code's `colorThemeData.ts`), a `%key%` label read from the extension's
  `package.nls.json`. The list comes from VS Code's own extension folder (found from the `code` command, else the
  standard install places) and `~/.vscode/extensions` (only what its `extensions.json` lists). A machine with no
  theme keeps the name as typed, unchecked (2026-10-26, below).
- The colours are written unscoped (no `"[Theme]"` key), so VS Code lays them over whichever theme is active.
  A colour is one of CSS Color 4's 148 names (`css_colors.py`, copied from the W3C table) or a hex form VS Code
  reads. `frame` colours what Peacock colours — the title bar, activity bar and status bar; `sidebar` adds the side
  bar with its title and section headers (left out, each keeps the theme's colour, a stripe across the side bar);
  `both` adds the editor, gutter, side bar, panel and terminal (key lists from VS Code's theme colour reference).
- A value hand-edited into `.git/config` that cannot be read is skipped with a warning: setup still writes the
  file.

### Consequences

- The choice survives every rewrite and never dirties a tracked file; another checkout keeps its own.
- A workspace that is no git repository cannot keep one (`theme|color set` say so).
- Nothing chosen, the file is byte-identical to before.

### Example

```text
$ git config --local --get-regexp '^aisland\.editor\.'
aisland.editor.theme Solarized Dark
aisland.editor.background navy
```

```jsonc
"workbench.colorTheme": "Solarized Dark",
"workbench.colorCustomizations": {
    "titleBar.activeBackground": "#000080",
    "titleBar.inactiveBackground": "#000080",
    "activityBar.background": "#000080",
    "statusBar.background": "#000080"
}
```

### What you may change

- The keys each area colours (`editor._FRAME`, `editor._SIDEBAR`, `editor._CODE`), and new areas beside
  `frame`/`sidebar`/`both`.
- The places VS Code is looked for (`editor.INSTALL_FOLDERS`).
- What must not change: the choice stays per machine and out of every committed file, and the colours stay
  unscoped so they hold whatever the theme.

### A machine without themes is a remote one: the name is kept unchecked (2026-10-26)

**Context.** `aisland workspace theme set "Monokai Pro"` typed in a remote window's terminal was refused with "VS
Code is not installed on this machine". The window's theme is applied by the local VS Code: Monokai Pro's
`package.json` says `"extensionKind": ["ui", "workspace"]` and was installed on the Mac only, while the remote (a
tunnel's box) had no `~/.vscode/extensions`, only Python and Jupyter extensions in `~/.vscode-server/extensions`, and
no `theme-*` folder in the tunnel server's `server/extensions`. A desktop VS Code ships its built-in themes (12
`theme-*` folders in VS Code 1.142.0 on macOS). The terminal's socket back to VS Code (`$VSCODE_IPC_HOOK_CLI`)
cannot ask for the local themes: the extension host's CLI server answers `open`, `openExternal`, `status` and
`extensionManagement` only, and the last lists the REMOTE extensions (`_remoteCLI.manageExtensions` uses
`remoteExtensionManagementServer`).

**Decision.** No theme found means VS Code is not on this machine, so a window showing the workspace comes from
another one: `set_theme` keeps the name as typed (`ChosenTheme(checked=False)`), the command says it is unchecked
(`editor.UNCHECKED_THEME`; the MCP tool's `note`), and `theme list` prints `editor.NO_THEMES_HERE`. A machine with
any theme still refuses an unknown name. A VS Code installed where aisland does not look (Insiders, VSCodium, an
unpacked tarball) is read the same way: unchecked, never wrongly refused.

**Consequences.** A typo on a remote machine is kept, and VS Code falls back to its default theme silently. The
detection needs no environment variable, so it holds in an `aisland tunnel connect` shell, which has no `code`.

**Example.**

```text
$ aisland workspace theme set "Monokai Pro"     # on the remote machine
==> Theme: Monokai Pro (this machine only; not checked: VS Code is not on this machine, so the theme comes from the VS Code that opens this workspace remotely)
```

**What you may change.** The wording; checking against a list the local VS Code hands over, should such a channel
appear (it would need an extension of aisland's own on the local machine).

---

## Framework selection is a second two-layer registry, and CI reads only the committed layer (2026-07-30)

*Superseded 2026-10-23 by "What each project installs: selections the projects declare, committed in
`workspace.toml`": the two layers and the committed-only CI rule stand; the axes, `config.toml` and the commands below
are gone.*

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

## TAB on an empty word offers a command's options (2026-10-21)

### Context

Click offers a command's options only once the word being completed starts with `-`:
`aisland wheels build vendorcli <TAB>` offered nothing, though the command takes nothing more but options, and
the same held for every aisland command. The user asked for the options there.

### Decision

- **`cli._Command` completes an empty word as `-`.** Click calls a command's `shell_complete` only when no
  argument is left to fill, so an argument's values still come first, and click's own filtering still leaves
  out a hidden option and one already typed.
- **Every group is a `cli._Group`** (`command_class = _Command`, `group_class = _Group`): a command written with
  `@<group>.command` gets the behaviour with no `cls=`, and a subgroup made with `@<group>.group` is a `_Group`
  too — never the root's `_WorkspaceGroup`, whose workspace refusal is for the root only.
- **A group's own TAB is unchanged**: its commands (the `wheels` group: then the projects). Options mixed in
  would bury the commands.

Rejected: patching `click.Command.shell_complete` for the process — it would change every click CLI that
imports aisland.

### Consequences

- An empty word after a finished command lists `--help` too, as `-<TAB>` always did.

### Example

```text
aisland wheels build vendorcli <TAB>   →  --include-published --all --include-project --zip --commit --force --no-check --json --help
aisland tests report aisland <TAB>  →  --days --json --help
aisland <TAB>                       →  aim backups check clean …   (commands only)
```

```python
class _Command(click.Command):
    def shell_complete(self, ctx, incomplete):
        return super().shell_complete(ctx, incomplete or "-")
```

### What you may change

- Which words a group offers, with `tests/test_cli_option_completion.py`.
- What must not change: every aisland group is a `_Group`, so no command misses the behaviour
  (`::test_every_group_makes_its_commands_and_subgroups_with_the_completing_classes`).

## The workspace's committed configuration is ONE file, and `uv.toml` cannot join it

*2026-08-01* — *superseded 2026-10-23: the one file is `workspace.toml` and `config.toml` is gone ("What each project
installs: …"); the reason `uv.toml` stays separate stands.*

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

### `torch-ext-lock` judges this venv's torch (2026-10-25)

**Context.** The check failed on any `lock` older than two minutes in any Python's folder of torch's extension
cache. torch up to 2.13 serialises a JIT build with `FileBaton`, a lock file whose existence blocks; torch 2.14
holds `filelock`'s OS lock instead and leaves the file behind after every build. With the workspace's venv on
2.14.1 the check failed on every healthy build — first on a `py312` lock another venv (also 2.14.1) had left.

**Decision.** Read this venv's torch from its metadata. From 2.14.1 (the first version measured: a build killed
mid-compile left the next import waiting over 240 s on 2.13.0 and loading in 43 s on 2.14.1) the check passes
saying why; below it, it keeps its old rule, in this Python's own folder only.

**Consequences.** No false alarm with a current torch; an old torch is still caught. A venv of another Python is
not judged by this one's torch.

**Example.**

```
✓ torch-ext-lock   torch 2.14.1 holds an OS lock while it builds — a killed build leaves no lock that blocks
```

**What you may change.** Lower `_TORCH_OS_LOCK_SINCE` to 2.14.0 once a 2.14.0 build is measured the same way.

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

## A root without an origin remote is not pulled or pushed, and its submodules follow their own branch

*2026-10-21*

### Context

`aisland workspace init` makes the root with `git init` and adds no remote: the root of such a
workspace lives on one machine. `pull-all` fetched it anyway and required every submodule to be on
the root's branch, so a root renamed to a feature branch, with one submodule left on `dev/main`, gave:

```text
needs you:
  root                 not fetched: fatal: 'origin' does not appear to be a git repository
  alpha                on dev/main, the root is on dev/feature — not pulled
  root                 origin has no dev/feature — not pulled
```

and `push-all` stopped on `git push origin <branch>` in the root after pushing the submodules.

### Decision

When the root has no `origin` remote, `pull-all` neither fetches nor merges it, pulls each submodule
on the branch it is on (the root's branch is then a local name, shared with nobody), and still
records what came in from origin under the same rule (only origin's commits, never backwards).
`push-all`, both forms, skips such a root and says so. A root with an origin is unchanged: a
submodule off its branch is still listed, not pulled.

### Consequences

- In an init-made workspace `pull-all` exits 0 when nothing needs a person, and `git status` in the
  root shows no moved submodule after it.
- The same holds for a folder workspace whose root repository has no origin.

### Example

```text
$ git pull-all
fetching 2 submodules (the root has no origin remote)…
beta                   merged origin/dev/feature (2 commits)
root                   recorded submodule pointers: beta
```

### What you may change

- The wording, with `tests/test_git_pull_push_all.py` and `tests/test_folder_git_aliases.py`.
- What must not change: with an origin, a submodule off the root's branch and outside `dev/*` is
  never pulled (`::test_a_submodule_off_the_roots_branch_is_not_pulled`).
- (2026-10-28, user request) A submodule on a `dev/<name>` branch of its own is pulled on THAT
  branch — commits go to any `dev/*` branch, and `/push-all` pushes it, so it must be pulled first
  or its push is rejected. The root, on another branch, records no pointer for it: the root's
  branch is not where that work lives
  (`::test_a_submodule_on_its_own_dev_branch_is_pulled_on_it_and_its_pointer_left`).

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
workspace nor aisland (`aisland` is not on PyPI), so carrying them must not need aisland there.

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

## `--include-project`: the project's own wheel, installed in place of its folder (2026-10-21)

### Context

`aisland wheels build` shipped what a project NEEDS; the project itself was installed editable from its
folder, so the other machine needed a checkout of it. The user asked for an option that builds the named
project's wheel too, and for its zip to install from any folder (option 2 of the choice put to them).

### Decision

- **`--include-project` builds the target with the same rules as every other wheel**: from its commit, a dirty
  one refused unless `--force`. Its own `wheels/` folder is left out of that check — the folder is what this
  command writes, and on a first run it is untracked.
- **The wheel is kept out of `WheelsReport.built`** (`project_wheel` instead), so "needs N projects", the
  tables and the JSON of what it needs mean what they meant; `wheels.yaml` lists it last.
- **`install.py` knows its project by `wheels.yaml`'s own `project:`.** That entry is never a dependency:
  `--all`, `--needed` and `--only` choose among the others, and while its `install:` is true it replaces
  `-e ..`; false gives the editable folder back, `--no-project` drops both. No new field in the file.
- **The check installs and imports that very wheel** — no second build of the target in the scratch folder.
- **The zip's `__main__.py` carries `STANDALONE`**, filled from the folder (`installs_own_wheel`: the own entry
  marked to install, and its file there). A standalone zip runs in a folder with no `pyproject.toml` too; it
  still refuses another project's `pyproject.toml`, and a `wheels/` whose `*.whl` belong to someone else (the
  unpacked `wheels.yaml` names another project, or there is none) — it would delete them.

Rejected:
- **A `needed`/`target` field in `wheels.yaml`**: the file already names its project, and an older
  `install.py` would choke on the field.
- **Unpacking a standalone zip into `./<project>-wheels/`**: the user chose `./wheels`, as everywhere else.

### Consequences

- An old zip (no `STANDALONE` line) keeps working: the template's default is the old behaviour.
- A user who sets the own wheel's `install:` to false must run the zip from the project's folder again; pip
  names the missing project if they do not.

### Example

```bash
aisland wheels build vendorcli --include-project --zip
# … vendorcli's own wheel (--include-project), installed in place of its folder:
# … Zip: vendorcli/wheels/vendorcli-wheels.zip (…) — install it from any folder with: python vendorcli-wheels.zip
mkdir /tmp/anywhere && cd /tmp/anywhere && python ~/Downloads/vendorcli-wheels.zip --dry-run   # the other machine
```

```python
from aisland import wheels
from aisland.manager import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "vendorcli", include_project=True, make_zip=True)
report.project_wheel, report.zip_standalone
```

### What you may change

- The report's wording and the README section, with `tests/test_wheels.py`.
- What must not change: without the flag every generated file is byte-identical; the own wheel is never in
  `built`; a standalone zip never unpacks over another project's folder or wheels.

## `--bundle`: one wheel, one name (2026-10-23)

### Context

The wheels folder installs through `install.py` or the zip, both run with `python`. The user asked for the
wheels packaged into ONE wheel, to install with plain `pip install <file>.whl`, and accepted what that costs
(option 1 of the choice put to them): pip knows the bundle's name only.

### Decision

- **One distribution, `<project>-bundle`.** A wheel carries exactly one `.dist-info`; pip refuses more
  (`multiple .dist-info directories found`, `pip/_internal/utils/wheel.py`), and a wheel inside a wheel is
  not installed. So the bundle merges the files: each wheel's members as they are, its `.data` folder renamed
  to the bundle's, the entry points merged, the top-level names joined, the licence files kept under
  `licenses/<distribution>/`. Nothing else of a dist-info comes over.
- **Beside the separate wheels, never instead of them**, in `wheels/bundle/`: a subfolder, so the zip (the
  folder's files) and the run's `*.whl` cleanup never take it for a separate wheel. Every other generated
  file is byte-identical with and without the flag; a bundle an earlier run wrote is removed by every run.
- **What it holds**: the project's own wheel and the separate wheels it needs (`BuiltWheel.needed`) — the
  scope decides, as for the folder. The project is held to its own wheel's rules (`_refuse_uncommitted` with
  the target), as under `--include-project`.
- **What it requires is read from each `pyproject.toml`**, never from the built METADATA, where an extra is
  only an `extra == "…"` clause inside the environment markers. A requirement on a bundled project is
  replaced by that project's base requirements and those of the extras it names, under its marker; every
  other requirement is kept as written; the project's extras stay the bundle's, without what the base
  already requires. A requirement the bundled version does not satisfy is refused, so a `--no-check` run
  cannot ship one. A project whose `dependencies` are `dynamic` is refused: pyproject does not hold them.
- **Refused, never resolved by a rule**: a wheel that is not pure Python (the bundle is `py3-none-any`), one
  path holding different bytes in two wheels, one entry point name pointing at two objects.
- **Checked alone**: `uv pip install <bundle>` in an empty venv of its own, then every module of the same
  packages imported; its failures are breaks (exit 1, no `--commit`). The empty-venv check of the folder
  runs as before.
- **Reproducible**: members in name order, one fixed time — an unchanged workspace rebuilds the same file.
  METADATA's description carries the generator header and each project's commit.

Rejected:
- **Several `.dist-info` folders in one wheel**: pip refuses it.
- **The bundle under the project's own name**: it would clash with `--include-project`'s wheel in the same
  folder, and pip would show a version that hides what is inside.
- **Requirements from METADATA**: splitting `extra == "…"` out of an arbitrary marker is parsing a language.

### Consequences

- On the other machine `pip show <dependency>` finds nothing, a package's `__version__` read from its
  distribution falls back to `0.0.0.dev0`, a later install of something requiring a bundled project puts
  PyPI's copy over the bundle's files, and uninstalling is all or nothing (README's table).
- Plugins found by entry point group keep working: the groups are merged into the bundle's.

### Example

```bash
aisland wheels build navigaitor --bundle
# … Bundle: navigaitor/wheels/bundle/navigaitor_bundle-<version>-py3-none-any.whl (…) — … in one wheel …
pip install navigaitor_bundle-<version>-py3-none-any.whl   # the other machine
```

```python
from aisland import wheels
from aisland.manager import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "navigaitor", bundle=True)
report.bundle.wheel, report.bundle.projects, report.bundle_import_failures
```

### What you may change

- The report's wording, the README section and the bundle's description, with `tests/test_wheels_bundle.py`.
- What must not change: one `.dist-info`; the separate wheels and every other file unchanged by the flag; a
  conflict refused, never resolved silently; the bundle's requirements read from `pyproject.toml`.

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
- (2026-10-28, user request) It also copies every other branch the local checkout has — the
  local branch list chooses WHICH branches, the primary still says WHAT each holds. `dev/main` and
  `main` stay always copied, so a checkout on `dev/<name>` alone keeps the mirror's `main` current.
  A local branch the primary lacks is reported `not on the primary`, not pushed and not a failure;
  a branch only on the primary is not copied (someone else's branch reaches the mirror from
  their machine's sync). The rows widen to the project's longest branch name.
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

- `MIRRORED_BRANCHES` (the always-copied ones) and `_branches` in `aisland/mirrors.py`, with `docs/sources.md` and
  `tests/test_mirrors.py`.
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

## `--no-check` builds the wheels and checks nothing, and says so (2026-10-20)

### Context

Every build ran both checks: each project the target takes from PyPI downloaded and compared with the
wheel its checkout builds, then the target installed in an empty venv and every module imported. They
are the slow part of a run and need the network, and a rebuild whose wheels the person does not need
checked (the same commits again, a zip for a machine where they will be tried anyway) paid for both.

### Decision

`aisland wheels build … --no-check` (user choice 2026-10-20) skips both checks and nothing else:

- the closure, the PyPI lookup (it still decides what is built), every refusal, the wheels, `wheels.yaml`,
  `install.py`, the README, `.gitignore` and `--zip` are those of a checked run — the generated files byte
  for byte;
- each project taken from PyPI is listed with its version and the note `not compared (--no-check)`; no
  PyPI wheel is downloaded and no checkout wheel built for it; the target's wheel is not built;
- the report's `checked` is false, the text says "Not checked (--no-check)" where the install section was,
  and the exit status is 0 — no check failed because none ran;
- `--no-check --commit` is refused before anything is built: the folder is committed only once the
  install check passed.

Rejected: skipping only the empty-venv check. The comparison is the other half of the cost (a download
and a build per published project), and a person who wants no check wants neither.

### Consequences

- An unchecked folder may not install on the other machine; the report, the JSON's `checked` and the
  refusal of `--commit` are what keep that visible.

### Example

```python
from aisland import wheels
from aisland.manager import get_workspace_root

report = wheels.vendor_wheels(get_workspace_root(), "navigaitor", scope="all", make_zip=True, check=False)
report.checked, report.ok, report.imported
# (False, True, [])
```

### What you may change

- The wording of the report's two lines, together with the tests that read them.
- What must not change: `--no-check` changes no file the run writes; it never goes with `--commit`.

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
  writes it to `.aisland/` — where its parent is again the root. `MANIFEST.in` keeps it in the source
  package too: `python -m build` builds the wheel from the source package, not from the checkout.
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
- From a wheel, the aliases point into the venv: recreating it needs `aisland setup` again, which installs them.
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
  venv needs `aisland setup` again.

### Example

```text
$ cd acme/beta && aisland setup        # among its steps:
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
  a venv kept elsewhere is linked in as `.venv`), aisland from PyPI or from a wheel, then
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
- Claude Code reads `.claude/commands/` of the folder a session starts in only (measured 2026-10-20: started in a
  project folder it listed none of the six), so every project folder gets the same link, kept out of git through its
  repository's `.git/info/exclude` — a line in each project's tracked `.gitignore` would change every project.
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

## A link to a folder of the workspace is a short name, never a project (2026-10-21)

### Context

A workspace may give its long project folders short names by links in its root (`ml -> machine_learning`,
`od -> object_detection/`). Every loop that looked for projects asked `is_dir()`, which follows a
link, so each such folder was a project twice: `aisland setup`'s extras prompt listed `machine_learning, ml`, `mypy.ini`
gained `od` and `od/torchsig` beside the real paths, and the install, pyright and the VS Code folder list did the
same.

### Decision

- `locations.is_alias(folder, workspace)`: a symbolic link whose resolved target lies inside the (resolved)
  workspace is an alias. Every reader of the workspace's projects skips it — `manager.discover_projects` (which
  recurses with the workspace root, so a link inside a project is judged against the workspace),
  `manager.workspace_folders`, `harness._project_folders`, `sources.known_projects`, `workspace.exportable_projects`.
  The folder it names is found under its own name.
- A link to a folder OUTSIDE the workspace is no alias: it is how a checkout kept elsewhere joins the workspace.
- Both sides are resolved, so a workspace entered through a link of its own (`~/acme -> /build/acme`) tells
  its aliases all the same.
- The short name is shown where a person reads it: `locations.alias_names` maps each aliased folder to its link's
  name (non-hidden links in the root only; of two links to one folder the first by name), and
  `write_code_workspace` uses it as the folder's `name`, its `path` still the real folder — a file opened in VS Code
  keeps its real path.

### Consequences

- A link inside the workspace to a folder no reader reaches (`x -> data/proj` with no project at `data/`) is not
  found either: a project lives in a project folder, a link only names one.
- Setup only appends to `mypy.ini`: entries an earlier setup added through a link stay until removed by hand.
- A workspace without such links gets byte-identical files.
- The workspace file carries the short name as a project's `alias` (user ruling 2026-10-21): `export` writes a root
  link to a project, or a folder checked out under a name other than its repository's (the project is then named
  after its repository, `_repository_name`), as `alias`; `init --from` clones the project straight into the alias
  folder — never a link, which stays a choice a user makes by hand. Names and aliases are one set of folders: an
  alias that is another project's name or alias is refused at its line. A link outside the workspace is left out of
  the file with the reason; a file without `alias` reads as before.

### Example

```python
from pathlib import Path
from aisland import locations, manager

root = Path("~/acme").expanduser()        # ml -> machine_learning, od -> object_detection/
locations.is_alias(root / "ml", root)     # True
locations.alias_names(root)[(root / "machine_learning").resolve()]   # 'ml'
manager.write_code_workspace(root)          # folders: {"name": "ml", "path": "machine_learning"}, ...
```

### What you may change

- The rule for which link names a folder when several do (`alias_names`), with
  `tests/test_symlinked_folders.py::test_of_two_short_names_for_one_folder_the_first_by_name_wins`.
- A new reader of "the workspace's projects" skips aliases through `is_alias`, never its own `is_symlink` test.
- Never: treating a link to an outside folder as an alias — it is the one way such a checkout joins.

---

## Every MCP server is started with the workspace's environment (2026-10-21)

### Context

A client (Claude Code, the desktop app, the Gemini CLI) starts each server in `.mcp.json` /
`.gemini/settings.json` as a subprocess with the client's OWN environment. The workspace's
`.env` reaches a terminal only through `source project.bashrc`, so a server never saw it:
measured on this machine, the desktop app's vendorcli-mcp ran with `DATA_ROOT`, `MODEL_ROOT` and
`RUN_DIR` unset, and a download that names no folder — which goes under those roots — landed
wherever the server had started.

### Decision

`manager._with_workspace_env` writes each discovered server as

```json
{"command": "<bash>", "args": ["-c", "source \"$0\" >/dev/null; exec \"$@\"", "<root>/project.bashrc", "<server>"]}
```

bash sources the same shell init a terminal does (venv, `PROJECT_ROOT`, `.env`, `PATH`) with its
stdout discarded, then `exec`s the server, which inherits all of it. Without a root
`project.bashrc` (a workspace before its first setup) or without bash on PATH, the plain command,
as before. Discovery and the missing-console-script warning still read the plain map.

### Consequences

- A server sees exactly what a run from a terminal sees, and `.env` stays the one place the
  values live — copying them into the client file's `"env"` would also put `.env`'s secrets in a
  second file.
- Nothing the init prints can corrupt the stdio protocol stream; its stderr still reaches the
  client's log.
- A change to `.env` reaches a server when the client restarts it; `user.bashrc` runs too.
- Starting costs one bash and the init, about 0.05 s here.

### Example

```python
from pathlib import Path
from aisland import manager

root = Path("~/acme").expanduser()
manager._with_workspace_env(root, {"vendorcli": {"command": f"{root}/.venv/bin/vendorcli-mcp"}})
# {"vendorcli": {"command": "/opt/homebrew/bin/bash",
#             "args": ["-c", 'source "$0" >/dev/null; exec "$@"', f"{root}/project.bashrc", f"{root}/.venv/bin/vendorcli-mcp"]}}
```

### What you may change

- The program bash runs (`_WITH_WORKSPACE_ENV`), with
  `tests/test_mcp_config.py::TestTheServersGetTheWorkspaceEnvironment`.
- Never: writing `.env`'s values into a client file, or letting the init's stdout through.

## A terminal on the remote machine rides the VS Code tunnel it already runs (2026-10-21)

### Context

`aisland tunnel start` puts a VS Code Remote Tunnel on a box published only through an HTTPS ingress: no inbound
port, and no sshd. The editor attaches to it; a plain terminal had no way in. Two ways round were refused: an
sshd on the box (there is none, and running one is a platform change) and a second tunnel published for it.

The VS Code CLI's own source (`cli/src/tunnels/control_server.rs`) shows the tunnel is enough. It publishes a control
port, 31545, whose `spawn` request starts any program on the machine and streams its stdin, stdout and stderr back,
and a connection arriving through the tunnel needs no handshake of its own (`requires_auth: AuthRequired::None`) — the
relay admitted only the tunnel's owner. Measured on a remote box (2026-10-21), over `devtunnel connect` and a
30-line MessagePack client: `version` 1.143.0 protocol 6 on connect; `spawn sh -c 'uname -a; tty; …'` answered with
the box's kernel, `not a tty`, an empty `$SHELL`, `python3` on the PATH but no `aisland`, and what was written to
stdin echoed back through `cat`.

### Decision

- **The way through is Microsoft's `devtunnel connect`, on the connecting machine only.** Signed in with the
  account the box's tunnel runs under, `devtunnel list -j` lists that tunnel (label `vscode-server-launcher`, its
  name as another label) and `connect <id>` forwards its ports to `localhost` on the same numbers — it has no option
  to choose another. aisland starts that forward per session and stops it when the shell ends.
- **The client is aisland's** (`tunnel_connect.py`): MessagePack over the forwarded port — `spawn`, then
  `streams_started`, `stream_data`, `stream_ended` and the spawn's result `{message, exit_code}`. `msgpack` is the
  `[connect]` extra: only the connecting machine needs it.
- **A real terminal on the far side, from a helper sent as source** (`tunnel_shell.py`): `spawn` gives pipes, so the
  program spawned is `python3 -c <the helper's source>`, which opens a pseudo-terminal and runs the account's shell
  (from its password entry — `$SHELL` is empty there) in the home folder or `--cwd`. The box needs a Python and
  nothing else; the helper imports only the standard library. Its stdin is framed — one-byte kind, four-byte length —
  so one stream carries keystrokes and window sizes; its stdout is the terminal's bytes.
- **The shell is interactive, not a login shell** (user choice 2026-10-21), as an editor's terminal starts it on
  Linux. Started as a login shell it read `~/.profile` only, and on a box whose `~/.profile` was trimmed of the block
  that loads `~/.bashrc` it had no colour prompt and no `ls --color` (measured: `PS1` was `/etc/bash.bashrc`'s,
  `alias ls` not found); interactive, `~/.bashrc` runs. `~/.profile`'s PATH lines are not lost: the tunnel's
  environment, inherited from the shell `aisland tunnel start` ran in, already carries them.
- **When the input ends the shell gets Ctrl-D, not a hang-up**, so what was typed before still runs; a program that
  ignores it is hung up 10 s later.
- **A taken port 31545 is refused**, naming its process: a forward started by hand reaches a machine nothing names.

### Consequences

- Nothing is added on the box and no second tunnel exists; the terminal lives exactly as long as the tunnel does.
- One session at a time per connecting machine (the fixed local port).
- The control protocol is the VS Code CLI's, not a published one: a CLI release that changes `spawn` or its streams
  breaks `connect`, and `tests/fake_devtunnel.py` must change with it.
- `connect`'s TAB answers from the last list (`devtunnels.json` in the tunnel state folder), never the network.

### Example

```python
from aisland import tunnel_connect, tunnel_shell

tunnel_connect.remote_command(24, 80, "/mnt/workspace")
# ('sh', ['-c', 'for p in python3 python; do … exec "$p" -c "$0" "$@"; done; …', '<helper source>', '24', '80',
#         '/mnt/workspace'])
tunnel_shell.encode_resize(33, 111)    # b'r\x00\x00\x00\x04\x00!\x00o' — a window size on the helper's stdin
```

### What you may change

- The helper's frame kinds (add one with its handler in `tunnel_shell.relay` and a test in
  `tests/test_tunnel_connect.py`), and `HANGUP_AFTER_S`.
- The forward, for another way through the relay to port 31545 — `connect`'s refusals and tests stay.
- Never: an import outside the standard library in `tunnel_shell.py`
  (`::test_the_helper_runs_on_a_python_without_site_packages`), or reusing a forward aisland did not start.

---

## A project declares what it brings; aisland finds it in two stages and the project never imports aisland

*2026-10-22* [declarations]

### Context

aisland hard-coded what belongs to the projects: the `framework` and `tracker` selections are a trainer's ideas
(`selection.py`'s `FRAMEWORKS`, `TRACKERS`), and the services each project needs (`aim`, `clearml`, `labelstudio`,
`fiftyone`, `iqengine`) lived in `aisland/services/`. A workspace without the trainer still offered `aisland
framework set`, and a project could not bring a service of its own.

### Decision

A project declares what it brings, and aisland finds it in two stages. STAGE 1 reads every project's
`pyproject.toml` — `[tool.aisland.selection.<name>]`, `[tool.aisland.service.<name>]` and the entry points of the
group `aisland.plugins` it declares — so a fresh machine can ask a selection's question before the project defining
it is installed. STAGE 2 loads those entry points once the project is installed. A plugin is any object with a
`kind` and that kind's methods: the project implements a protocol and imports nothing from aisland, so it never
gains a dependency on its dev tool. Only what a workspace project declares is loaded.

Rejected: plugins that subclass an aisland base class (every library would depend on aisland); entry points alone
(nothing can be read before the install, so setup could not ask its questions first); a hand-written list of
plugins in aisland (the hard-coding this replaces).

### Consequences

- `aisland workspace show` lists what was found and from which project; a declaration aisland cannot use stops it
  with one line naming the file and the line of the table.
- Choices offered for a selection no project defines are reported, not refused: a workspace without the defining
  project, or with it switched off, still sets up.
- The remaining steps (`TASKS.md`) build on this: `workspace.toml` and the generated `aisland workspace configure`,
  the trainer declaring `framework`/`tracker`, and services declared by the projects that need them.

### Example

```toml
[tool.aisland.selection.framework]
question = "Which ML frameworks should this machine install?"
choices = ["torch", "keras"]

[project.entry-points."aisland.plugins"]
acme-db = "acme_plugin:Database"
```
```python
class Database:            # the project's own code — no aisland import
    kind = "service"
    def start(self, ctx): ...
    def stop(self, ctx): ...
    def status(self, ctx): ...
```

### What you may change

- A new declaration (`[tool.aisland.<kind>.<name>]`) or plugin kind (`PluginKind` with `_KIND_METHODS`), each with
  its refusals and tests in `tests/test_discovery.py`.
- The wording of `aisland workspace show`.
- Never: a plugin that must import aisland, or loading an entry point no workspace project declares.

---

## `workspace.toml`: the workspace written down like `.gitmodules`, kept current by setup

*2026-10-22* [workspace-file] — supersedes the `<name>.workspace.toml` written by `aisland workspace export`.

### Context

The workspace file of 2026-10-20 was written on request (`aisland workspace export`) and read only by `init --from`,
so it went stale the moment a submodule was added, and it sat beside `config.toml` as a second committed description
of the same workspace in a second format (`[projects]` with inline tables, an `alias`).

### Decision

One file, `workspace.toml`, at the root and committed, shaped like `.gitmodules`: `[workspace]`, then a
`[project."<name>"]` per project with `path`, `url` and `branch`. `aisland setup` keeps the projects current
(`workspace_file.refresh`): new ones added, a changed url or path followed, gone ones dropped, a branch written once
kept. `init --from` clones from it and copies it in. `export` is gone. The later steps of the redesign move
`config.toml`'s settings into the same tables, each a key of the plugin that owns it.

Rejected: keeping `export` (a file nobody refreshes is wrong by the next submodule); generating `.gitmodules` from
the file (git and every git tool read `.gitmodules`; the file follows it instead); keeping a project gone from the
workspace with a flag (its entry would clone a project the workspace no longer has).

### Consequences

- Every workspace has the file after its next `aisland setup` (acme: 29 projects, 173 lines).
- A branch changed in `.gitmodules` does not reach the file once a branch is written there; the source plugin
  changes it (`TASKS.md`).

### Example

```toml
[project."models"]
path = "models"
url = "git@github.com:acme/models.git"
branch = "dev/main"
```
```python
from aisland import workspace_file
workspace_file.refresh(Path("~/work/acme").expanduser())   # ['workspace.toml: added models']
```

### What you may change

- The how-to comments (`workspace_file._header`) and the wording of `refresh`'s lines.
- A new key of a project belongs to a plugin of the redesign, never to `workspace_file.py`.

**Amendment (2026-10-28, release review): a row with a url is a project to add, and setup leaves projects clean.**
Writing `[project."lib"]` with a url into the file and running setup dropped the row as "no longer in the workspace" —
and with it the file, when that was its only row. The activation step already cloned any listed project the
workspace lacked (the optional ones); `refresh` had removed this one before it got there. Now `refresh` keeps a listed
project with a url the workspace lacks, saying `lib is not in the workspace yet; setup adds it`, and only a row
nothing could clone is dropped. The cost, accepted by the user: a project leaves the workspace only when its row
goes too (a `git rm` alone is undone by the next setup). In the same review, setup stopped dirtying the projects it
sets up: it created a `.gitignore` in each one for its `pyrightconfig.json`, and the editable install left
`<name>.egg-info/` — two untracked entries that made `aisland wheels build` refuse every project needing it. Both are
now lines in the project's own `.git/info/exclude` (`git_exclude.exclude`, which the agent harness already used for
its link): per machine, never committed, and no project file is created or edited. A `/pyrightconfig.json` line a
project already committed stays; it is the project's.

```python
git_exclude.exclude(project_dir, ["pyrightconfig.json", "**/*.egg-info/"])
# <project>/.git/info/exclude gains:  /pyrightconfig.json   /**/*.egg-info/
```

---

## `aisland workspace configure`: one set of commands built from each plugin's keys; `source` the first

*2026-10-22* [sources] — the committed and per-machine layers of a project's source move.

### Context

Each setting had its own command group (`aisland source`, `aisland mirror`, `aisland framework`, `aisland tracker`),
each with its own list/set/unset written by hand, and the source's committed half sat in `config.toml`'s
`[sources.*]` while the project's url and branch sat in `workspace.toml` — a project's branch twice, its
distribution often written equal to its name.

### Decision

`aisland workspace configure` builds `<plugin>` (the listing, `--json`), `set PROJECT VALUE [--<key> VALUE…]
[--machine-only]` and `unset PROJECT [--machine-only]` from a `configure.Plugin`'s keys; a plugin writes no command.
`source` is the first: its settings go into the project's table of `workspace.toml` (`source`, `version`, and
`distribution` only where it differs from the name; a git source's ref is the project's own `branch`), this machine's
into `.git/config` as `aisland.source.<project>.source|branch|version`. No backward compatibility: an old table or
one-key override is simply not read (user ruling 2026-10-22, `mirror`'s step). `source apply` is gone: setup applies.
`mirror` followed the same way: the project's `mirror`, `aisland.mirror.<project>.mirror`, the action
`aisland workspace mirror sync` one level up.

### Consequences

- `aisland source …` answers `No such command 'source'`; the generic commands carry every later plugin.
- acme's three entries and three per-machine keys were converted by hand; the effective source of each is unchanged.

### Example

```text
$ aisland workspace configure source set loggair local --branch dev/main --machine-only
$ git config --local --get-regexp '^aisland\.source\.loggair'
aisland.source.loggair.source local
aisland.source.loggair.branch dev/main
```

### What you may change

- A plugin's keys, help and listing columns; a new plugin in `configure.PLUGINS`.
- Never: a plugin with commands of its own, or a setting written twice in `workspace.toml`.

---

## A root's two choices: a local repository (`--local`), projects it does not record (`untrack`)

*2026-10-23* [local-workspace]

### Context

`aisland workspace init` always made a root repository and added each project as a submodule, and refused a folder
that already was one. A workspace whose projects are separate repositories, with no root anyone shares, then showed
every starting file as untracked (`?? AGENTS.md`, `?? mypy.ini`, …) and every project's new commit as
` M <project>`, and `git pull-all` committed `chore: sync submodule pointers` into a root nobody reads. A shared root
wanted the second half alone: its projects' commits neither shown nor recorded. Dropping the root
repository (a folder of clones) would lose the per-machine settings kept in its `.git/config` (the editor's theme,
the `active` choices, mirrors) and a `git pull-all` typed in the root.

### Decision

Two independent choices. `init --local` makes a new root that tracks only `.gitmodules` and the projects (`.gitignore`
`/*`, marked `aisland.workspace.local` in `.git/config`, the first commit made by `local_workspace.settle`, which
commits `.gitmodules` and the projects again whenever they change). Tracking is per project, git's own
`ignore = all`: `aisland workspace untrack|track [PROJECT…] [--machine-only]` writes the committed choice to
`workspace.toml` (`track = false`) and `.gitmodules`, or this machine's to `aisland.track.<project>.track` and
`.git/config`; `init --untracked` untracks every project; `git pull-all` never records an untracked submodule. `init`
in an existing repository sets it up in place instead of refusing it (`--from` and `--local` refused there).

### Consequences

- `git status` in the root no longer shows an untracked project; every per-machine setting and `git pull-all` from
  the root keep working.
- The root keeps the commit it last recorded: a `git submodule update` typed there would put the project back on
  it, detached. aisland runs it only for an empty folder, so it never does.
- `git add <project>` skips an untracked project with a hint suggesting `--force`; `/commit-all` and
  `/commit-session` are told not to take it.
- `git submodule add` refuses a `--local` root's ignored path without `-f`; the `active` plugin passes it, a person
  types it.
- An untracked project follows its branch: on no branch (a fresh clone's `git submodule update`), `aisland setup`,
  `untrack` and `git pull-all` fetch it and switch it to `.gitmodules`' branch (`.` = the root's), else origin's
  default — never forced. So a shared root's recorded commits stop mattering for its untracked projects: a new
  machine needs only the clone and `aisland setup`.

### Example

```text
$ aisland workspace init . --untracked
==> Set up workspace acme in /Users/you/work/acme
    .git/                     kept (an existing repository)
    ...
    models                    untracked: the root records none of its commits
$ aisland workspace track models --machine-only
==> tracked on this machine: models (nothing committed)
  - models: tracked on this machine
```

### What you may change

- The commit message and the report rows.
- Never: `update = none` for the projects (setup would no longer check out an optional one), a pull-all that
  records an untracked submodule, or an `ignore` value aisland did not write overwritten.

---

## A clone the workspace does not list: setup adds it as a submodule

*2026-10-23* [clones]

### Context

In a workspace of submodules, a repository cloned into its folder by hand was invisible: discovery and
`workspace.toml` read `.gitmodules` only, so setup neither listed nor installed it, and said nothing.

### Decision

`aisland setup` finds each clone `.gitmodules` does not list (a `.git` folder, an `origin`, a `pyproject.toml`) and runs
`git submodule add -b <its branch> <its origin> <folder>`, which keeps the clone as it is — without asking (user ruling
2026-10-23: a question about a project the workspace already lists in `workspace.toml` was the wrong default). The new project is tracked as the workspace says: `[workspace] track = false` in `workspace.toml` (`init --untracked`,
`untrack` with no project named) untracks every project without a `track` of its own, those added later included —
a workspace-wide choice, never inferred from the other projects (user correction 2026-10-23: the first rule, "like the
others", left a workspace made untracked with its new projects tracked).

### Consequences

- A clone is installed and listed by the same setup run; a `--local` root commits it.
- A data repository (no `pyproject.toml`) or a scratch one (no `origin`) is never offered.

### Example

```text
  - added foo as a submodule (https://host/team/foo.git, dev/main)
```

### What you may change

- The question's wording.
- Never: a question before adding, or moving the clone's checkout.

---

## Which projects a machine has: the `active` key marks a project optional, its value the default

*2026-10-23* [activation]

### Context

A project was optional when it shipped a `uv.toml` or `.gitmodules` said `active = false`; this machine's answer was
git's own `submodule.<name>.active`, which `git submodule deinit` erases (so setup wrote it back after every deinit),
and `--activate`/`--deactivate` were setup flags. `workspace init --from` cloned every project, the private ones too.

### Decision

`workspace.toml` says it: a project with an `active` key is optional and the value is its default. `init --from`
clones only the projects without the key; `aisland setup` asks about each optional one and keeps the answer in
`aisland.active.<project>.active`, then checks out (adding a project the workspace does not have yet from its url and
branch) or empties each folder, and writes what git reads — `.gitmodules` `active = false` for a default-off project
and `submodule.<name>.active` — from the effective value. `aisland workspace configure active set|unset
[--machine-only]` is the one way to change it; the flags and the deinit write-back are gone.

### Consequences

- A fresh machine gets only the projects everyone has, and is asked about the private ones once.
- An optional project `.gitmodules` lacks is not "gone": setup's upkeep keeps it in the file.

### Example

```toml
[project."tools"]
url = "https://bitbucket.example.com/scm/team/tools.git"
active = false
```

### What you may change

- The question's wording and the listing's columns.
- Never: a second place that makes a project optional, or a choice kept in a key git can erase.

---

## What each project installs: selections the projects declare, committed in `workspace.toml`

*2026-10-23* [frameworks] [config]

### Context

aisland hard-coded two selections, `FRAMEWORKS` and `TRACKERS` (their names, questions and hints), beside its own
`EXTRAS`. A project declared its menu under `[tool.aisland] frameworks = [...]`, the choice was committed in
`config.toml` (`frameworks`, `trackers`) and kept per machine as `aisland.frameworks`; the commands were `aisland
framework|tracker list|set|unset` (with `--no-apply`) and `aisland setup --framework/--extra`, and `aisland config
show|migrate` read the file and folded the pre-2026-08 ones into it. A choice that was no extra was found by a
separate check (`require_defined_declarations`) that named no line.

### Decision

A selection is the projects': the one whose `[tool.aisland.selection.<name>]` has `question` defines it (matrainer
asks `framework` and `tracker`), every project's `choices` are its menu, and discovery refuses a choice that is no
extra, naming the file and the line. aisland keeps only `extras`. The choice is committed as `[workspace] <name> =
[...]` in `workspace.toml` and chosen per machine as `aisland.<name>.selected`; each selection is a workspace-level
plugin of `aisland workspace configure` (`set NAME… [--machine-only]`, `unset`), which reinstalls at once.
`config.toml`, `config.py`, `frameworks.py`, `aisland config`, `aisland framework|tracker`, setup's
`--framework/--extra` and `--no-apply` are gone. A service whose backend is missing names `set` with the trackers
already chosen plus its own: `set` takes the whole list, and the old message (`aisland tracker set mlflow`) would
have dropped the rest.

### Consequences

- aisland names no framework or tracker; a workspace has exactly the selections its projects ask for.
- One committed file. Measured on acme: every generated `Jenkinsfile`, `Jenkinsfile.local` and `ci.yml` (78 files)
  is byte-identical to the `config.toml` era's, with every dependency released and with none.
- A machine opts out of a choice with `set … --machine-only`; choosing before a first setup is `set` then
  `setup -y`.

### Example

```toml
# matrainer/pyproject.toml — defines the selection
[tool.aisland.selection.tracker]
question = "Which experiment-tracking backends should this machine install?"
choices = ["mlflow", "tensorboard", "aim", "clearml", "wandb"]

# workspace.toml — the committed choice
[workspace]
tracker = ["mlflow", "tensorboard", "aim", "clearml", "wandb"]
```

### What you may change

- A selection's question (in the project that defines it), the listing's columns, the prompt's layout.
- Never: a selection named in aisland's code (but `extras`), a second committed file, or a machine's choice
  reaching a generated CI file.


## An extra a project marks is installed only when named (2026-10-23)

*2026-10-23* [frameworks]

### Context

`aisland setup` offered every extra and took them all on a blank answer, `-y` or `--all`. One project's extra pinned
two dependencies to their remote branch by URL — meant for a machine with no checkout of them — and a blank answer
installed it in a workspace that had both checked out: uv replaced the local checkouts with the remote branch.

### Decision

A project marks such extras in `[tool.aisland] manual-extras`. They stay on the menu, marked `(only when named)`, but
a blank answer, `-y` and `--all` leave them out; naming one (at the prompt or with `aisland workspace configure
extras set`) installs it. The user chose the declaration over a structural rule (leave out an extra that pins a
workspace project by URL): the project knows what its extra is for, and the declaration holds in every workspace.

### Consequences

- An extra marked by one project is left out for every project offering that name.
- Discovery refuses a marked name that is no extra of the project, naming the file and the line, before setup
  installs anything.

### Example

```toml
[project.optional-dependencies]
remote = ["acme-core @ git+https://example.com/acme/core.git@dev/main"]

[tool.aisland]
manual-extras = ["remote"]
```

### What you may change

- The menu's marker and the prompt's wording.
- Never: a marked extra taken by a blank answer, `-y` or `--all`.

## A project's wheels folder is a find-links path of the generated `uv.toml` (2026-10-23)

*2026-10-23* [wheels]

### Context

A project shipping the wheels of what PyPI lacks (`aisland wheels build`) already carried `[tool.uv] find-links =
["wheels"]`, but uv reads a project's `[tool.uv]` only when it runs inside that folder. `aisland setup` runs uv from
the workspace root, so the wheels were never seen: installing the project failed on a dependency only the folder held
until `python wheels/install.py` had run by hand.

### Decision

The generated root `uv.toml` lists each activated project's `wheels/` folder (marked by its `wheels.yaml`) under
`find-links`, relative to the file. Measured: uv resolves the relative path against the file's folder, from the root
and from a folder below it; it installs a wheel only the folder has; and it takes the folder's `confluid-0.4.1` over
PyPI's `confluid-0.4.1` whose files differ — pip, through `--find-links`, takes PyPI's. The user chose every such
folder, tracked in git or not.

### Consequences

- Setup, `aisland workspace configure … set` and any `uv pip install` run in the workspace use the wheels.
- A dependency taken from PyPI (`source = pypi`) comes from a wheels folder whenever the folder holds the same or a
  newer version; a project checked out in the workspace still installs from its checkout.

### Example

```toml
# <workspace>/uv.toml, generated
find-links = ["bridge/wheels"]
override-dependencies = ["opencv-python ; sys_platform == 'never'", "opencv-python-headless>=4.12,<5"]
```

### What you may change

- Which folders count (the `wheels.yaml` marker).
- Never: an absolute path in the generated file, or `install.py` named as a step of setup.

## The workspace's name can be written in `workspace.toml` (2026-10-23)

*2026-10-23* [workspace-file]

### Context

The name — the editor file, the Jenkins jobs, the how-to's `--prompt` — was the origin's repository name, else the
folder's. A workspace in a folder called `workspace`, with no origin, was named `workspace` with no way to change it,
and the how-to used the folder's name even where the origin gave another.

### Decision

`[workspace] name` in `workspace.toml` comes first, then the origin, then the folder, read in one place
(`manager.get_workspace_name` through `workspace_file.name_of`); the how-to uses the file's name too. A name must be
one plain name; a selection may not be called `name`, since a selection's choice is committed under its own name in
the same table.

### Consequences

- Setting a name renames the editor file at the next setup (the old one stays until removed) and the Jenkins jobs.
- A workspace without a name is named as before.

### Example

```toml
[workspace]
name = "studio"
extras = ["dev", "local"]
```

### What you may change

- The order after the file's name.
- Never: a second place a name is read from.

---

## System tools: declared by the projects, checked before installing, aisland's own in the package

*2026-10-23* [provision]

### Context

`aisland provision` (and setup's first step) read `[tool.aisland.sys-dependencies]` from every checked-out project,
aisland's own `pyproject.toml` among them, and installed the lot. A wrong key or a value that was no list was skipped
silently, an unknown userspace tool was named only on Linux in the middle of setup, nothing showed what a machine
needed without installing it, and a workspace whose aisland came from a wheel had no aisland `pyproject.toml` to
read — it got no service supervisor.

### Decision

Discovery reads the declaration (stage 1) and refuses what it cannot use, naming the file and the line. aisland's own
tools are `provision.AISLAND_TOOLS`, in the package. `aisland workspace provision` lists this machine's tools (who
needs each, whether it is installed) and `install` installs them, the function setup runs; `aisland provision` is
gone. There is no configure plugin: which projects a machine has (the `active` plugin) already decides its tools.

### Consequences

- The same tools install from a checkout and from a wheel.
- A bad declaration fails `aisland setup` before its first install.

### Example

```text
$ aisland workspace provision
  TOOL                           HOW   PROJECTS      INSTALLED
  f1bonacc1/tap/process-compose  brew  aisland       yes
  node                           brew  streamstudio  yes
```

### What you may change

- The listing's columns; a new tool in `AISLAND_TOOLS` or a project's declaration.
- Never: aisland's tools back in its `pyproject.toml`, or an install that starts before the declarations are read.

---

## A service a project brings: declared in its `pyproject.toml`, or a class of its own that imports nothing of aisland

*2026-10-24/25* [declarations]

### Context

Every service was a class inside aisland, listed by hand in each of `aisland start|stop|status|info|reset`, whatever
project actually used it (FiftyOne serves curaitor's datasets, Aim and ClearML matrainer's trackers). A workspace
without that project still carried the service, and adding one meant editing aisland.

### Decision

One list, `services/registry.py`, behind `aisland services [start|stop|…] [NAME…]`. A project adds to it in two ways:
a declaration (`[tool.aisland.service.<name>]`: command, port, ready path, `needs`), run as a `DeclaredService`; or an
`aisland.plugins` entry point of kind `service`, a class of its own needing only `start(ctx)`, run as a
`PluginService` — what it leaves out is aisland's own. The class gets a `ServiceContext` (its folder, the port asked
for, `run`, `write_env`, `fail`, …) and never imports aisland. Both start only when named, get `aisland <name> …`,
and may not take a name aisland uses. A moved service disappears with its project.

### Consequences

- FiftyOne lives in curaitor (`curaitor/fiftyone_service.py`); its data folder stays `data/fiftyone/`.
- `aisland services start` no longer starts FiftyOne; `aisland services start fiftyone` does.
- Finding the list imports each installed project's class: about 0.1 s for curaitor.

### Example

```python
class FiftyOne:                      # curaitor/fiftyone_service.py — no aisland import
    kind = "service"

    def start(self, ctx):
        port = ctx.port or 5151
        ctx.write_env({"FIFTYONE_URL": f"http://localhost:{port}"})
        ctx.run([sys.executable, "-m", "fiftyone", "app", "launch", "--port", str(port), "--remote"],
                port=port, ready="/")
```

### What you may change

- What the context offers — when a real service needs more, never ahead of one.
- Never: a service class that imports aisland, or a project's service started by default.

**Amendment (2026-10-25): Aim and ClearML move to matrainer.** The two needed more than FiftyOne, so the class may
now declare `options` (Aim's `--repo`, offered beside `--port`) and `supervised = False` (ClearML's containers are
Docker's, so its `info` stands alone), and the context gives `workspace_name`, `read_env()` (ClearML reuses the
login it wrote) and `choose_command(selection, name)` — the command that adds a tracker keeping the ones chosen, so
the "not installed" message is word for word aisland's. aisland keeps Jenkins, MLflow, Label Studio, Docker,
SwiftBar and IQEngine.

**Amendment (2026-10-26): Label Studio moves to annotaide, IQEngine to the signal project that reads SigMF.** aisland now keeps Jenkins,
MLflow, Docker and SwiftBar alone. Label Studio is annotaide's `labelstudio` extra, installed only when named (it
pins its own Django); its class asks git for the default user itself, so the context did not grow. IQEngine runs
under Docker (`supervised = False`) with its own `stop`/`status`/`reset`, and its reset wipes no data — so a class
may name the question `aisland <name> reset` asks first (`reset_question`), which `registry.declared` carries onto
the entry. Adding to an `extras` selection never chosen keeps `dev` in the command (`selection.add_command`): unset,
it installs each project's `dev`, and `set labelstudio` alone would have dropped it.

```python
class IQEngine:                      # the signal project's — no aisland import
    kind = "service"
    supervised = False
    options = {"recordings": "Directory of SigMF recordings to serve …"}
    reset_question = f"Remove the IQEngine container and the {image_ref()} image? (recordings are kept)"
```

**Amendment (2026-10-28): MLflow moves to matrainer.** aisland keeps Jenkins, Docker and SwiftBar alone — the
services every workspace may want; a tracking server is the trackers' project's. It came out of the release review:
aisland's own MLflow failed with `aisland workspace configure tracker set mlflow`, a command that exists only where a
project defines the `tracker` selection, so in a workspace without that project its error named a command that is
not there. As matrainer's class it exists only where that selection does, and `selection.missing_message` went with
it (`ctx.choose_command` is the one wording). Nothing the context offers had to grow. What changed for a user: `aisland
mlflow` comes with the project; `aisland services start` without names no longer starts it (a project's service starts
when named); `aisland mlflow ui <db>` is gone (`mlflow ui --backend-store-uri sqlite:///<db>` is the same). Its data
folder is still `data/mlflow/`, so a store written before the move is served after it.

```python
class MLflow:                        # matrainer's — no aisland import
    kind = "service"

    def start(self, ctx: Any) -> None:
        ...
        ctx.run([sys.executable, "-m", "mlflow", "server", ..., "--backend-store-uri", _store(ctx.data_dir)],
                port=port, ready="/health")
```

---

## A project is the workspace's own by its GitHub remote: no organisation is written into aisland

*2026-10-26* [github-owner]

### Context

Three questions asked "is this one of ours?" by looking for `github.com/Gearlux/` in a URL: which projects CI
installs from GitHub (`discover_gearlux_projects` matched `.gitmodules` against the organisation), whether a PyPI
release is the project's own (`released_on_pypi`, `_pypi_provenance`, `wheels.pypi_state`) and which owner the CI's
GitHub App token is for. A workspace of another owner got CI files without the install lines its unreleased
dependencies need, a `pypi` source refused as "owned by a different project", and wheels shipped for packages PyPI
already serves. aisland was about to be released for such workspaces.

### Decision

The owner is read from each project's own remote (`sources.project_repositories`: `.gitmodules`, or a clone's
`origin`; `github_repository` reads every spelling git accepts). A project is internal when that remote is on
github.com, and a runner installs it from `git+https://github.com/<owner>/<repository>.git@main`. A release is the
project's when its metadata names `github.com/<owner>/` (case-insensitive); a project off GitHub has no owner and
matches no release. The token step names the private dependencies' owner and its secrets `<OWNER>_APP_ID` /
`<OWNER>_APP_PRIVATE_KEY`; private dependencies of two owners are refused (one App token reaches one owner).

### Consequences

- For a workspace whose projects are all one owner's (acme), every generated CI file is byte-identical: the 78
  files of `aisland jenkins scaffold --force` compared before and after.
- A submodule of another GitHub owner is now internal; before, only the one organisation's were.
- A detached project is internal only when its remote is on GitHub (before, every detached project was).

### Example

```python
>>> github_repository("git@github.com:acme/lib.git")
GitHubRepository(owner='acme', name='lib')
>>> released_on_pypi("lib", "acme")        # metadata names github.com/acme/… and a final version exists
True
```

### What you may change

- Another host (a GitLab owner) — a second parser beside `github_repository`, with its own install URL.
- Never: an organisation written back into aisland, or a release accepted without its owner's URL.

## `--extra NAME`: one uv command installs the project with its wheels (2026-10-27)

### Context

A project installed outside the workspace from a checkout took two commands: `python wheels/install.py --installer uv
--all --no-project --reinstall`, then `uv pip install -e ./<project> --no-deps`. The user wanted one:
`uv pip install -e ./<project>[wheels]`. An extra holds requirement names; what makes uv take the folder's wheels
is a source per name. Measured with uv 0.13.5: a `[tool.uv.sources]` entry conditional on an extra
(`{ path = "wheels/<file>", extra = "wheels" }`) is read by `uv pip install` of the project, applies only while that
extra is asked for (an install without it is unchanged), reaches a wheel only another wheel needs once the extra
lists it, and a wheel rebuilt under the same version is installed again (uv records the file it came from). `uv
lock` resolves every extra, so the sources are locked too (`{ path = … }` in place of the find-links registry).

### Decision

`aisland wheels build … --extra NAME` writes both pieces into the target's `pyproject.toml` between marker lines
(`wheels_extra.py`): the extra at the end of `[project.optional-dependencies]`, the sources at the end of
`[tool.uv.sources]` (each table appended when missing). A rerun removes what is between the markers and writes it
again, so the file is byte-identical when nothing changed and a hand edit outside the markers survives. Everything is
checked before any build: NAME must be in `[tool.aisland] manual-extras` (taken by `aisland setup`, the wheels would
replace the workspace's checkouts), and no key outside the markers may hold NAME or a source of a built wheel.
`--commit` commits `pyproject.toml` with `wheels/`.

### Consequences

- The generated `wheels/README.md` leads with the uv command; `install.py` stays for pip, which reads no uv sources.
- `uv run` in the project resolves every wheel's requirements, a wheel the project does not need included (a wheel
  needing a private index makes `uv run` need it too).
- The project's own wheel built from the commit carries the extra as that commit had it.

### Example

```bash
aisland wheels build app --all --include-project --extra wheels
uv pip install -e ./app[wheels]        # from the folder holding app, on the other machine
```

### What you may change

- Another name than `wheels`, or several runs writing several extras (each its own markers) — not built yet.
- Remembering (user request 2026-10-27): a build without `--extra` keeps an existing block current, since a
  forgotten flag left the sources naming wheel files the rebuild deleted and the install failed on the other
  machine. Not with `--include-project`, whose build installs the project's own wheel and leaves the block unless
  `--extra` is given; the regenerate command names `--extra` only there.
- Never: tying the extra to `--include-project` (aisland's own run would publish it), or a hand-maintained copy of
  the list beside the generated one.

---

## The test-run record can be turned off per workspace, project or machine

*2026-10-27* [test-stats-switch]

### Context

The pytest plugin records every run of a workspace project, and that record is what `aisland tests report` judges the
fast run by. Installed from PyPI it loads in every venv aisland is in, so a workspace that does not want the record
had only pytest's own switches (`-p no:aisland_test_stats`, `PYTEST_ADDOPTS`, an `addopts` line) — each set by hand,
none visible to aisland or its report.

### Decision

`aisland workspace test-stats on|off [PROJECT…] [--machine-only]`, shaped like `track|untrack` (the user's choice over
a per-project `configure` plugin): no project named covers the whole workspace, those added later included; the
committed layer is `workspace.toml` (`[workspace] test-stats = false`, a project's own value only where it differs),
this machine's is `.git/config` (`aisland.test-stats.default`, `aisland.test-stats.<project>.record`) and wins. The
plugin still loads; for a run it would record it reads the switch with the standard library — `tomllib` and one
`git config --get-regexp` — and a file it cannot read means recorded, so the switch never costs a run.

### Consequences

- A recorded run costs one more `git` call (a few milliseconds); a run the plugin would not record costs nothing.
- `aisland tests report` and its JSON say when a project is off (`recording`, `recorded_by`) and name the command
  that turns it on — `--machine-only` when this machine turned it off.

### Example

```text
$ aisland workspace test-stats off models
==> test runs not recorded: models (in workspace.toml — commit it)
  - models: not recorded
  - tools: recorded
```

### What you may change

- A listing (`aisland workspace test-stats` alone printing each project's state) — not built.
- Never: removing the plugin's record to turn it off, or a switch the plugin needs more than the standard library to read.


---

## `aisland wheels prune`: old wheel versions leave the history; `reset` follows on every other clone (2026-10-27)

### Context

`aisland wheels build --commit` commits `wheels/`; every rebuild that changes a wheel adds its bytes to the history
for good. Measured on a project with seven wheel commits: 59 wheel versions in its history (14.6 MiB), 32 of them
held by a branch tip or a tag (7.5 MiB) — half of every clone was wheels no ref holds. The user asked to keep the
wheels a branch or a tag holds and to drop the accumulated versions, aisland to force-push, and every other machine
to be fixable afterwards.

### Decision

- **What goes:** every blob ever under `wheels/**.whl` that no tip of this clone's branches and tags or origin's
  branches holds (origin's tags are fetched first and must equal this clone's). Nothing else is touched.
- **How:** git-filter-repo (`prune` extra, run as `python -m git_filter_repo`) with `--strip-blobs-with-ids`,
  `--partial`, `--prune-empty never`, `--prune-degenerate never` and `--preserve-commit-hashes`. With these the
  rewrite is a function of each commit alone: no commit disappears and no message changes, so a second clone
  stripping the same blobs gets the same ids (measured: identical ids, an unpushed commit carried on top). That is
  what makes `reset` a re-run of the same strip instead of a rebase.
- **Safety:** refuse tracked changes, a branch ahead of or behind origin's and a differing tag; bundle every ref
  first; check every kept ref's tree is unchanged; push atomically with `--force-with-lease=<ref>:<fetched sha>`
  per ref (no `+` in the refspec: a `+` overrides the lease); a detached HEAD is carried by a temporary ref
  (filter-repo moves refs, never a detached HEAD); reflogs expire for every ref but the stash, symbolic ones
  included (a clone's `origin/HEAD` keeps its own reflog of the old history).
- **The record:** `refs/wheels-prune/map` on origin, a chain of commits, one per prune, each holding
  `stripped-blobs` and `commit-map`. Text only, so it keeps no old commit alive; a chain, so a clone that missed
  several prunes takes each in turn with that prune's own blob list (a union of the lists would strip a version a
  later commit added back). The name holds no workspace or tool name: the repository may be one that must not
  reveal them.
- **The root:** the gitlink it records is replaced by the rewritten id of the same commit, in a commit of that path
  alone (built in a scratch index, so the root's staged changes stay staged). The root never records a newer commit
  of the project than before — an untracked project stays unrecorded.

### Consequences

- Every commit from the first stripped wheel onward gets a new id; old commits of the workspace root name ids that
  are gone. A clone that merges origin before `reset` brings the old history back, so `git pull-all` fetches
  `refs/wheels-prune/*` with each repository and refuses to merge one whose HEAD still reaches a commit a prune's
  `commit-map` lists, naming `aisland wheels reset <project>`; a plain `git pull` is not guarded.
- The host frees the stripped versions only when its own garbage collection runs.
- A clone's local tag on a commit the prune rewrote moves with it and holds origin's files for that commit.

### Example

```bash
aisland wheels prune app              # says what would go, changes nothing
aisland wheels prune app --apply      # rewrites, force-pushes origin, re-records the root's pointer
aisland wheels reset app              # on every other machine, before its next pull
```

### What you may change

- Which refs keep their wheels (a `--keep REF` narrowing it) — not built.
- Never: a `+` refspec beside the lease, a rewrite that drops emptied commits or rewrites messages (two clones would
  disagree), or a record that holds old commits as parents.

## The example environment: each project lists its variables, aisland concatenates them (2026-10-27)

### Context

Every config and project reads machine settings from the workspace's `.env` (`DATA_ROOT`, a service's token, a
library's switch), and a new machine had nothing to start from but `workspace init`'s five-line template; which
variables a project reads lived in its README if anywhere, and drifted. The user asked for the variables in every
project and an aisland command that concatenates them into a file to copy to `.env`; their choices: two files per
project — `env.complete` (every variable, written by the agent) and `env.example` (the ones a person normally sets,
written by the user) — the workspace's two files generated per machine and never committed, a variable several
projects read merged under the project that defines it, a generated test in each project, `--missing` to compare an
existing `.env`.

### Decision

- A project's file is a small line grammar (`env_example.read_project`): `# NAME=VALUE  description` (two spaces part
  value and description), `# -- heading`, `# not for .env: NAME … — reason`, other comments. A line that is no
  comment is refused naming its line — the file is copied to `.env`, so it must set nothing.
- `aisland workspace env-example` (and `aisland setup`, step 5b) reads every checked-out project's files of both kinds
  before writing either, and renders each kind in `env_example.project_order`: the editor file's dependency order
  (`manager._dependency_order` over the folders sorted by name), so the first project reading a variable is the
  library that defines it. A variable is listed once, there, `also read by:` the others, each other wording of its
  description kept under it; the `not for .env` names stay out. `workspace init` seeds a missing `.env` with the
  workspace's `env.example`, else its `env.complete`, else the short template.
- `aisland workspace env-test` writes `tests/test_env_example.py` into a project from
  `templates/test_env_example.py.template`: standard library and pytest only (a project never imports aisland),
  regexes over the non-test `.py` files and the YAML under `config/`, `examples/`, `graphs/` with its comments left
  out (prose like "`$VAR` is expanded" reads nothing). `env.complete` must list every name read; `env.example` may
  hold only `env.complete`'s names. The template's output passes black, isort and flake8 at 120 columns unchanged,
  or every project's gates would rewrite it and `env-test` would never be up to date.

Rejected: one `aisland env check` scanning every project (a project's own test run would not catch its drift, and
the general projects cannot depend on aisland); committing the workspace's files (they follow what this machine has
checked out); choosing the short list automatically (the variables two projects read — a token only one project
reads would never be in it); editing `.env` (`--missing` prints, the person pastes).

### Consequences

- A name the code builds at run time (a prefix plus a key) is not seen by the test; the project lists it by hand.
- A project that discovery does not know (no installable `pyproject.toml`) comes after the others, in the workspace's
  listing order.
- The Bitbucket projects' generated test names aisland in its header, as their generated Jenkinsfiles already do.

### Example

```
$ aisland workspace env-example
==> Wrote env.complete (67 variables from 26 projects)
==> Wrote env.example (0 variables from 0 projects)
    no env.example: loggair, confluid, liquifai, …
```

### What you may change

The header text, the section layout, the folders the test reads. Do not let any line of a generated file set a
variable, and do not make the workspace's files committed or the command edit `.env`.

---

## A command that prints an `Error:` line exits 1 (2026-10-27)

[failures]

### Context

About forty places printed `Error: …` (or `  - Error: …`) with `click.echo` and returned, so the command ended with
exit 0: `aisland jenkins scaffold no-such-project`, `aisland setup` after a project failed to install, `aisland check`
without a `.venv`, a service whose port was taken. A script, CI or the MCP server's `aisland` call saw success. Some
of those failures must not end the command — setup installs every project, `services start` starts the others — so
raising everywhere would have changed what the commands do.

### Decision

Two kinds. A failure that ends the command raises `click.ClickException`: one `Error:` line on stderr, exit 1. A
failure the command goes on past calls `aisland.failures.report(line)`: the line goes to stderr, the command carries
on, and the top-level group's `invoke` turns a run that reported any into exit 1 — also over the command's own
`SystemExit(0)`; a non-zero code of its own is kept. The count is reset as each command starts.

### Consequences

- Those lines move from stdout to stderr.
- `aisland check` with a gate tool missing fails that gate (`isort is not installed in …`) instead of a traceback.
- `aisland supervisor down` that cannot stop the supervisor raises.

### Example

```python
from aisland import failures

for project in projects:
    try:
        install(project)
    except subprocess.CalledProcessError as error:
        failures.report(f"  - Error: Failed to install {project}: {error}")   # setup goes on; the run exits 1
```

### What you may change

- Which failures end a command and which it goes on past.
- Never: an `Error:` line printed with `click.echo` and a return, or a library `sys.exit`.

---

## aisland installs its own tools when a feature first needs one, after asking (2026-10-28)

[on-demand-tools]

### Context

`aisland setup` ran Homebrew for every one of aisland's tools — process-compose, docker, colima, act, SwiftBar — then
downloaded the Jenkins WAR and installed the Java it names, on every machine, whatever the workspace used. A service
command that found process-compose missing installed it at once, adding a third-party tap, from a status-like command
or a script. The release review found a two-project workspace getting a GUI app, a container engine and a JDK it never
asked for.

### Decision

Setup installs what the projects declare (`provision.install(root, aislands_own=False)`). A feature that finds its
tool missing asks — `<who> needs <tool>, which is not installed. Install it (<command>)?` — and installs on a yes;
without a terminal, or on a no, it refuses with the line that installs it and installs nothing. `aisland workspace
provision install`, asked for by name, still installs everything.

### Consequences

- A fresh workspace that never starts a service installs no system software beyond its projects' declarations.
- The first `aisland services start` / `docker start` / `swiftbar start` / `jenkins start` asks once; in CI or an MCP
  call it refuses with the install line.

### Example

```text
$ aisland services start mlflow < /dev/null
Error: aisland's services need process-compose, which is not installed. Install it: brew install f1bonacc1/tap/process-compose
```

### What you may change

- Which features ask for which tool (`provision.NEEDED_BY`).
- Never: installing one of aisland's tools without asking, or asking where no one can answer.

