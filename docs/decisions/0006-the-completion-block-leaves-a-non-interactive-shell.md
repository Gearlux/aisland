# The completion block leaves a non-interactive shell before it runs anything (2026-10-20)

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
  $ bash -ic "source .project.bashrc.completion; complete -p labels"
  complete -o default -F _labels_completion labels
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

[The architecture and every decision](../architecture.md#the-decisions)
