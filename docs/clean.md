# Reclaiming disk space

How to free the space a checkout accumulates outside git with `aisland clean`, and how to inspect, restore and prune the backups it makes with `aisland backups`.

## Moving a project's ignored files aside (`aisland clean`)

A checkout accumulates gigabytes that git never sees: `.venv/`, `build/`, `.mypy_cache/`,
coverage reports, `lightning_logs/`, downloaded checkpoints, run directories. `git status`
stays clean the whole time. `aisland clean <project>` removes all of it — but **moves it into
a dated backup instead of deleting it**, because in a workspace an ignored path can be
load-bearing (a provisioned `bin/`, a service's `data/`, an `.env`) and `git clean -Xdf` gives
you no second chance.

```bash
aisland clean models --dry-run   # what would move, and which backups expire — changes nothing
aisland clean models             # move it (asks first)
aisland clean models -y          # ...without the prompt
aisland clean models --keep-days 1   # sweep harder when the disk is actually full
```

```text
Git-ignored content in models (/home/you/acme/models)
  .mypy_cache/                                            4 files   210.9 MiB
  lightning_logs/                                       105 files   252.5 MiB
  runs/                                                 484 files     4.9 MiB
  ...                                                  1892 files     1.4 GiB  in 48 entries

Deleted 2 backup(s) older than 7 days (1.9 GiB):
  .../.aisland/data/backups/models/20260726-101500

Moved 48 entries (1.4 GiB) into .../.aisland/data/backups/models/20260804-164945
Restore with:  aisland backups restore models
```

**Backup layout.** Everything lives under `.aisland/data/backups/` in the workspace (a checkout of aisland uses `aisland/data/backups/`; git-ignored, and *not*
touched by `aisland services reset`, which wipes each **service's** own directory):

```text
<workspace>/.aisland/data/backups/<project>/<YYYYMMDD-HHMMSS>/
├── manifest.json   # project, timestamp, every entry with its size and file count
└── files/          # the project tree, mirrored — restore with `cp -R files/. <project>/`
```

## Inspecting and undoing a clean (`aisland backups`)

`aisland clean` acts on a project's working tree; `aisland backups` acts on the store it
writes to. Two verbs:

```bash
aisland backups list                 # everything in the store, newest first
aisland backups list models          # just one project's
aisland backups restore models       # move the most recent backup back into the project
aisland backups prune                # apply the retention window without cleaning anything
```

```text
  PROJECT                BACKUP              AGE       SIZE  ENTRIES  EXPIRES
  tools                  20260804-171924      0d    2.1 MiB       18  in 7d
  models                 20260804-171409      0d    1.4 GiB       47  in 7d

  2 backup(s), 1.4 GiB under /home/you/acme/.aisland/data/backups
  Restore the newest:  aisland backups restore tools
```

Restore **moves**, like the clean did — the disk was full enough to warrant a clean, so a
second copy of the payload is the one thing that may not fit. The backup is consumed as it is
restored: its manifest is rewritten and a backup with nothing left in it is removed.

```bash
aisland backups restore models --stamp 20260728-101500  # an older one
aisland backups restore models --entry checkpoints      # one path only (repeatable)
aisland backups restore models --dry-run                # what would come back
aisland backups restore models --force                  # replace what the project re-created
```

A path the project has a **different** copy of is skipped, never clobbered — usually a cache
or a `.venv` that rebuilt itself since the clean, and almost always the one you want to keep.
Those entries stay in the backup and the command exits **1**, so a script notices a partial
restore; `--force` replaces them instead (it asks first, `-y` skips the prompt).

A path the project has an **identical** copy of is not a collision at all: it is reported as
`already`, the backup's redundant copy is dropped, and the exit code stays 0. The comparison
is a real one — sizes and file counts first (so a rebuilt `.venv` is rejected without reading
a byte), then byte-for-byte for whatever survives that. Timestamps are deliberately not part
of it, because the case this exists for is a path you already copied back by hand, and
`cp -R` restamps every file it writes.

The manifest is what makes restore finer-grained than `cp -R`: it records the paths that
actually moved, so a backed-up `data/jenkins` merges back into a `data/` the project has
since re-created, and `--entry` can pull back a single directory.

**Pruning on its own.** `aisland clean` already sweeps on every run, so the store keeps itself
in check as long as you keep cleaning. `aisland backups prune` is that same sweep as a
standalone command, for a workspace nobody is currently cleaning:

```bash
aisland backups prune --dry-run      # what has expired
aisland backups prune                # delete it (asks first; -y skips)
aisland backups prune --keep-days 1  # tighter window on a full disk
```

It covers every project — the retention window belongs to the store, not to one project — and
confirms before deleting, because standalone it is the only thing happening: inside a clean
the sweep is bounded by the fresh backup taken right after it, while here a mistyped
`--keep-days 0` would empty the store outright.

**Retention.** Every invocation sweeps backups older than `--keep-days` (default **7**) —
across *all* projects, not just the one being cleaned. The sweep runs before the move, so a
run on a full disk frees the expiring space first. It only ever deletes directories whose
name parses as `YYYYMMDD-HHMMSS`, i.e. ones this tool created; anything else under the backup
root is left alone.

| Guard | Why |
|---|---|
| One project per run — there is no `--all` | "Delete every ignored file in the workspace" would take out the shared `.venv`, every service's data directory and `.env` in one keystroke |
| The workspace **root** repo is refused | Its ignored set *is* that `.venv` / `.env` / generated `uv.toml` + `pyrightconfig.json` |
| A directory that is not its repository's root is refused | `git status` there reports the **enclosing** repo's ignored set |
| A project with no checkout (`github` / `pypi` source) is refused | Nothing to clean; see `aisland workspace configure source` |
| The backup store itself is never moved | It sits in `.aisland/data/backups` at the workspace root, which no project's clean reaches; when aisland runs from its own checkout, the store is `aisland/data/backups` inside that checkout, so cleaning it splits that one entry and moves everything *except* the store |

Cleaning a project undoes work that has to be redone: it removes caches that the next `pytest` / `mypy` run
rebuilds slowly, and a project's ignored build output. (When aisland runs from its own checkout, cleaning that
checkout also takes away the tools aisland installed in its `bin/` and every service's `data/`: re-run
`aisland workspace provision install` and `aisland setup`.)
That is the point of the backup — `aisland backups restore <project>` puts it back, until the
retention window closes.
