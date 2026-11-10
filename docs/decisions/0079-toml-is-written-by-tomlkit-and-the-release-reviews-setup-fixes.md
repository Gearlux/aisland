# TOML is written by tomlkit, and the release review's setup, wheels and CI fixes

*2026-11-08* (follows "Imports at the top, containers typed, one completion guard")

## Context

The 0.1.0 review of setup, the wheels run, the CI scaffold and the workspace model found one class of defect three
times over: a serializer of aisland's own. `uv_config._emit_uv_toml` pasted each string into `"…"` unescaped and wrote a
list of tables as Python's repr, so one project's `[[index]]` table, a backslash path or a marker holding `"` made
the root `uv.toml` unreadable — and every install of the workspace reads it. `workspace.file.render` rebuilt the
committed `workspace.toml` from its parsed data on every `refresh` and `set_settings`: a person's comments vanished and a
date came back as text, while the file's own header said "the rest is yours to edit". `wheels.extra` inserted its
block by line into a `[project.optional-dependencies]` header and missed the same table written inline
(`optional-dependencies = { … }`), as dotted keys or as `[tool.uv] sources = { … }`; the write then failed after every
wheel was built and the wheels folder replaced.

Beside it the review found writes that fail open or leave work behind: a Jenkins that answered the linter with 403 or
500 had the Jenkinsfile written as validated; a failed wheels check left the clone's `wheels/` and `pyproject.toml`
rewritten; a PyPI wheel listed without a SHA-256 was accepted unchecked; the import check had no time limit; `.env`
was edited without a lock and its FIRST assignment replaced while the shell keeps the LAST; setup dropped the projects
activation could not bring in line and exited 0; `.gemini/settings.json` was written over whole; a broken
`pyproject.toml` of an unrelated project stopped `aisland wheels` with a raw `TOMLDecodeError`. And `aisland wheels
prune` left a mirror's refs on the old history, so the next `aisland workspace mirror sync` saw every branch
"ahead" and named a merge that brought the stripped wheels back.

## Decision

- **tomlkit writes every TOML file aisland writes** [toml-writer] — a base dependency (`tomlkit>=0.12.0`: every test
  of the three writers passes on it as on 0.15), kept out of the standard-library-only modules by the `stdlib-only`
  contract. Reading stays `tomllib` (`pyproject.py`, `workspace.file.read`).
  - `workspace.toml` is EDITED: `render(workspace, name, text)` parses the file as it is and syncs it to the
    `WorkspaceFile` — a key gone is deleted, a changed value replaced where it stands, a new key or project added at
    the end, a value equal to the one written left as written. The how-to (the comment block opening with the file's
    name) is aisland's and rewritten; a file opening with comments of its own keeps them.
  - The `--extra` block is one function, `wheels.extra.edit`: the old blocks out, each table moved into a table of its
    own when the project writes it inline or dotted (tomlkit; the move must change no value), the blocks in, and the
    result read back as the file's settings plus exactly what was generated. `check_extra` runs it on the text as a
    dry run before the first build.
  - The root `uv.toml` is a tomlkit document under the header, read back with tomllib before it is written.
- **A write that cannot be checked is not written as checked.** The scaffold's linter keeps Jenkins' session cookie
  with its crumb; an HTTP error refuses the file, and only an unreachable Jenkins writes it unchecked, saying so. A
  failed wheels check puts `wheels/` and `pyproject.toml` back as they were (`build._Before`) and makes no zip. A PyPI
  file without a digest is refused before it is fetched. The import run stops after `IMPORT_SECONDS` (900), its
  process group killed, naming the module it was importing (each name written to `<result>.progress` first).
- **The mirror after a prune** [mirror-after-prune]. The prune never rewrites another remote's refs — they are that
  host's, and rewritten they would claim it holds commits it has not got — so `--apply` refuses while one reaches a
  version that goes, naming the command that drops them. The sync reads the primary's `refs/wheels-prune/map`
  (`workspace/prune_record.py`, below both layers): a mirror's branch or tag on a commit a prune rewrote receives the
  primary's commit — a descendant of the rewritten twin — leased on the mirror's commit, never fetched; the record
  goes too, so a clone of the mirror can `reset`; commits pushed to the mirror on top of the old history are refused
  with a `cherry-pick` of exactly them. ADR 0024's "never force" keeps every other case; the lease is what keeps a push
  made to the mirror meanwhile.
- The rest: `.env` is written under one lock (`data/env.lock`) and its LAST assignment replaced, `export` kept;
  setup reports each project `activation.reconcile` could not bring in line; the MCP client files keep every key but
  `mcpServers`, and one that is no JSON object is reported and left; `aisland wheels`' projects come from
  `Workspace.folders`, a broken `pyproject.toml` raised only where the run needs that project, two projects of one
  distribution refused; the scaffold finds a project by its name, its `workspace.toml` row or its folder (a link
  resolves to its folder), lists the unreleased dependencies by name (the same files on every machine) and measures a
  child package's own subpackage (`coverage`, from `[tool.setuptools.packages.find] include`); `configure <selection>
  set` installs setup's dependency groups; the prune's root pointer leaves a gitlink the user staged; `atomic` fsyncs
  the file before the move and the folder after; every state or committed file of setup and the wheels run goes through
  `atomic.write_text`.

## Consequences

- A person's `workspace.toml` keeps its layout through every setup; a project added by setup is appended, not placed
  by the projects' order.
- A first `--extra` on a project with an inline `optional-dependencies` rewrites that one table as a table of its own.
- Pruning a mirrored project takes one more command (drop the mirror's refs), and the next sync pushes every rewritten
  branch with a lease.
- A failed wheels check no longer leaves its wheels to inspect; the report names what failed.

## Example

```text
$ aisland wheels prune app --apply
Error: app: the refs of remote 'mirror' reach wheel versions the prune strips — drop them first (the next fetch of mirror brings them back): git -C /w/app for-each-ref --format='delete %(refname)' refs/remotes/mirror/ | git -C /w/app update-ref --stdin
$ aisland workspace mirror sync app
==> app → git@github.com:Org/app.git
    dev/main      pushed (the primary's history after a wheels prune)
```

## What you may change

- `IMPORT_SECONDS`, the tomlkit floor (with a run of the TOML writers' tests on it), the wording of every refusal.
- What must not change: no hand-written TOML; a mirror's ref moves without a fast-forward only from a commit a prune's
  record lists, leased; a failed check leaves the clone as it was.

---

[The architecture and every decision](../architecture.md#the-decisions)
