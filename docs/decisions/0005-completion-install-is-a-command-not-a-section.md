# Completion install is a command, not a section of the bootstrap script (2026-07-31)

**Context.** Shell completion for the workspace's CLIs was installed by ~70 lines
of inline shell in `setup.sh` — sections 5 (liquifai apps), 5a (prune stale
blocks) and 5b (click + argparse apps). It worked. It was also, structurally,
write-only: the only way to run it was to run `setup.sh`, which also provisions
the venv, runs `aisland setup`, and rewrites the git config.

Nobody runs a full provisioning script to refresh a tab-completion. So when 5b
was added on 2026-07-27 to close a real gap (`aisland` and `canvas` had no
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

**Decision.** The install is `aisland/aisland/setup/completion.py`, exposed as
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
NON_LIQUIFAI_CLIS = (NonLiquifaiCli("aisland", "click"),)   # a project's click CLIs: its [tool.aisland] click-commands
```

**What you may change.** aisland's own CLIs are the rows of `NON_LIQUIFAI_CLIS`; a
project's click CLI is declared by the project (`[tool.aisland] click-commands`, since
2026-10-28: record "A project names its click CLIs, and the package names nothing of the
workspace it was written in"), never a row here. A fourth
completion *mechanism* means a new `CliKind` and its renderer. Do not move any of
this back into `setup.sh`, and do not let it grow a second copy there — the
duplicate would rot exactly as the original did.

---

[The architecture and every decision](../architecture.md#the-decisions)
