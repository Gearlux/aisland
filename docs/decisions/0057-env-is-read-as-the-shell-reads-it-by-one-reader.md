# `.env` is read as the shell reads it, by one reader (2026-10-28)

## Context

A finding of the release review's second pass: two `.env` readers disagreed with each other and with the shell that
sources the file (`project.bashrc`'s `set -a; source .env`). The services kept the quotes of `KEY="a b"`, Jenkins
stripped them, neither expanded `$HOME`, read `export` or dropped a trailing comment, and the services' writer wrote a
value with a space unquoted, which the shell then runs as a command.

## Decision

`envfile.read` is the one reader, and it reads what bash exports — `export`, quotes (single
literal, double expanded, `\` escapes), `$VAR`/`${VAR}` from an earlier line then the environment (unset = empty), a
trailing ` # note` dropped, a line bash reads as no assignment skipped — each case pinned against bash itself
(`tests/test_workspace_env.py`). The services, `apply()` and Jenkins read through it; `envfile.write` quotes
what bash would change, so both read a written value back verbatim.

## Consequences

`KEY = value` in `.env` now sets nothing, as in the shell (it used to set `KEY`).

## Example

```text
.env                                envfile.read (= what bash exports, HOME=/home/you)
SPACED="a b"                        SPACED   = a b
LITERAL='$HOME/x'                   LITERAL  = $HOME/x
export EXPORTED=/data               EXPORTED = /data
DATA_ROOT=$HOME/data                DATA_ROOT = /home/you/data
WITH_COMMENT=value # a note         WITH_COMMENT = value
SPACED_KEY = spaced value           (nothing)
```

## What you may change

- The shell forms the reader understands, each with a case checked against bash.
- Never: a second `.env` parser.

---

[The architecture and every decision](../architecture.md#the-decisions)
