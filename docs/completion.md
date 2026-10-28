# Shell completion

How a workspace shell gets tab-completion for `aisland` and the other command-line tools of the workspace, and how `aisland completion install` keeps it current. `viewer`, `lib` and `viewer-export` below stand for command-line tools of your own projects.

[`examples/04_completion.py`](../examples/04_completion.py) writes the completion file into a temporary file and checks
that a non-interactive shell sourcing it runs no program.

## Shell completion

Enable it by starting a shell that sources `project.bashrc`:

```bash
source project.bashrc      # activates .venv AND exports .env AND loads completion
                           # (and defines cdp/cdd/cdm/cdr — cd into $PROJECT_ROOT, $DATA_ROOT, $MODEL_ROOT, $RUN_DIR;
                           #  `cdm mod<TAB>` completes a folder under $MODEL_ROOT — and h [word]: the history, filtered)
                           # (and tm [tmux new-session options]: a tmux session here, with this shell's environment)
aisland <TAB>              # check docker doctor jenkins mcp services ...
aisland wheels build models <TAB>   # --include-published --all --include-project --zip ... --help
viewer exp<TAB>            # export export-workflow
lib <TAB>                  # run
```

Once an `aisland` command has every argument it needs, TAB on an empty word offers its options (those
not typed yet); a command group offers its commands.

After adding, renaming, or removing a CLI, refresh it with **`aisland completion
install`** — a one-second command, not a re-provision. See
[Shell Completion](#shell-completion-aisland-completion) below for the three CLI
families it covers, the `--target-rc` option, and how stale blocks are pruned.

Click refuses to emit a bash script for **bash < 4.4**, and macOS ships bash
3.2 as `/bin/bash` — the generated block guards on `BASH_VERSINFO`, so on a
stock-bash machine `aisland` completion is skipped rather than erroring. Install
a modern bash (`brew install bash`) to get it.

## Shell Completion (`aisland completion`)

Installs tab-completion for **every** CLI in the workspace in one command:

```bash
aisland completion install
```

It is idempotent — every block it writes is marker-delimited and rewritten in place — so run
it again whenever you add a project or a command-line tool.

```
==> /path/to/workspace/.project.bashrc.completion (bash)
    liquifai apps: lib, models, tools-cli, viewer, ...
    other CLIs:    aisland, viewer-export
    pruned stale:  old-tool
```

**Three families, because the workspace has three kinds of CLI**, and a CLI that is none of
them silently gets no completion at all:

| Family | Who | Mechanism |
|---|---|---|
| liquifai apps | `lib`, `models`, `tools-cli`, … | the `liquifai.apps` entry-point group, handed to liquifai when it is installed (without it this family is empty) |
| click apps | `aisland`, `app` | click's `_<PROG>_COMPLETE` protocol |
| argparse apps | `viewer-export` | `argcomplete` |

**Where it writes.** By default, the workspace-local `.project.bashrc.completion`, which
`project.bashrc` sources — so your global `~/.bashrc` stays clean and the tracked tree stays
free of diffs. Point it somewhere else with `--target-rc`:

```bash
aisland completion install --target-rc ~/.bashrc     # into your own shell rc
aisland completion install --target-rc project.bashrc
aisland completion install --shell zsh               # default: detected from $SHELL
```

Writing to an existing rc appends (and refreshes) only its own marked blocks; the rest of the
file is untouched. If nothing sources the target yet, the command prints the line to add.

**Stale blocks are pruned.** liquifai's installer merges by marker and leaves blocks it did
not write alone, so a renamed or removed CLI would keep a dead block forever. Any block whose
program no longer exists is dropped — unless *no* liquifai app was discovered at all, which
means discovery is broken rather than everything being uninstalled, and pruning then would
empty the file. Keep a stale block deliberately with `--no-prune`.

Programs are looked up in the active venv's `bin` before `PATH`, so the command behaves the
same whether or not you have sourced `project.bashrc`.

> **Verifying.** `complete -p aisland` in a new shell should print a completion function. If
> it does not, check that something sources the target file — the command tells you when
> nothing does.
