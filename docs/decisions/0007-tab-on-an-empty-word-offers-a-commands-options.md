# TAB on an empty word offers a command's options (2026-10-21)

## Context

Click offers a command's options only once the word being completed starts with `-`:
`aisland wheels build app <TAB>` offered nothing, though the command takes nothing more but options, and
the same held for every aisland command. The maintainer asked for the options there.

## Decision

- **`cli._base.Command` completes an empty word as `-`.** Click calls a command's `shell_complete` only when no
  argument is left to fill, so an argument's values still come first, and click's own filtering still leaves
  out a hidden option and one already typed.
- **Every group is a `cli._base.Group`** (`command_class = Command`, `group_class = Group`): a command written with
  `@<group>.command` gets the behaviour with no `cls=`, and a subgroup made with `@<group>.group` is a `Group`
  too — never the root's `_WorkspaceGroup`, whose workspace refusal is for the root only.
- **A group's own TAB is unchanged**: its commands (the `wheels` group: then the projects). Options mixed in
  would bury the commands.

Rejected: patching `click.Command.shell_complete` for the process — it would change every click CLI that
imports aisland.

## Consequences

- An empty word after a finished command lists `--help` too, as `-<TAB>` always did.

## Example

```text
aisland wheels build app <TAB>   →  --include-published --all --include-project --extra --bundle --zip --commit --force --no-check --json --help
aisland tests report aisland <TAB>  →  --days --json --help
aisland <TAB>                       →  backups check clean completion …   (commands only)
```

```python
class Command(click.Command):
    def shell_complete(self, ctx, incomplete):
        return super().shell_complete(ctx, incomplete or "-")
```

## What you may change

- Which words a group offers, with `tests/test_cli_option_completion.py`.
- What must not change: every aisland group is a `Group`, so no command misses the behaviour
  (`::test_every_group_makes_its_commands_and_subgroups_with_the_completing_classes`).

---

[The architecture and every decision](../architecture.md#the-decisions)
