"""``docs/commands.md``, the command reference: every aisland command with its usage, its help and its options.

The page is rendered from the click tree, so it says what ``--help`` says. ``tests/test_docs.py`` compares the
committed page with what :func:`render` returns, and writes it instead when ``AISLAND_WRITE_COMMANDS=1`` is set::

    AISLAND_WRITE_COMMANDS=1 .venv/bin/pytest -n 0 tests/test_docs.py -k command_reference

It is rendered in a workspace without projects (the test's ``empty_workspace``), so neither a service a project
brings (``aisland <service> …``) nor a selection a project declares (``aisland workspace configure <selection>``)
appears: those are each workspace's own.
"""

from __future__ import annotations

import inspect
import re
from typing import Iterator, List, Optional, Tuple

import click

PAGE_HEADER = """\
<!-- Generated from aisland's commands; do not edit by hand. To write it again:
     AISLAND_WRITE_COMMANDS=1 .venv/bin/pytest -n 0 tests/test_docs.py -k command_reference -->
# Command reference

Every aisland command, with its usage, its help and its options, as its `--help` shows them. The services a
workspace's projects bring (`aisland <service> start|stop|…`) and the selections they declare (`aisland workspace
configure <selection>`) are that workspace's own: `aisland --help` and `aisland workspace configure` list them there.
The pages linked from the [README](../README.md) explain each command at length.
"""

#: What Markdown would read as markup outside a code span: a tag's `<`, emphasis, a heading or list at a line's start.
_MARKUP = re.compile(r"([<>*\\])")
_CODE_SPAN = re.compile(r"(`[^`]*`)")


def _escaped(text: str) -> str:
    """``text`` with Markdown's markup characters escaped outside its code spans."""
    return "".join(part if part.startswith("`") else _MARKUP.sub(r"\\\1", part) for part in _CODE_SPAN.split(text))


def _help(text: str) -> List[str]:
    """click's help text as Markdown: each paragraph on one line, a ``\\b`` paragraph (kept as written) fenced."""
    text = inspect.cleandoc(text).split("\f", 1)[0]
    lines: List[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        if paragraph.startswith("\b"):
            block = paragraph[1:].strip("\n")
            lines += ["```text", block, "```", ""]
        else:
            lines += [_escaped(" ".join(line.strip() for line in paragraph.splitlines())), ""]
    return lines


def _walk(command: click.Command, ctx: click.Context) -> Iterator[Tuple[click.Context, click.Command]]:
    """``command`` and every command under it that is not hidden, a group before its commands."""
    yield ctx, command
    if isinstance(command, click.Group):
        for name in command.list_commands(ctx):
            sub: Optional[click.Command] = command.get_command(ctx, name)
            if sub is None or sub.hidden:
                continue
            yield from _walk(sub, click.Context(sub, info_name=name, parent=ctx))


def _section(ctx: click.Context, command: click.Command) -> List[str]:
    level = "#" * min(ctx.command_path.count(" ") + 2, 4)
    lines = [f"{level} `{ctx.command_path}`", ""]
    usage = " ".join([ctx.command_path, *command.collect_usage_pieces(ctx)])
    lines += ["```text", usage, "```", ""]
    if command.help:
        lines += _help(command.help)
    options = []
    for param in command.get_params(ctx):
        record = param.get_help_record(ctx)
        if record is not None:
            names, text = record
            options.append(f"- `{names}`" + (f" — {_escaped(' '.join(text.split()))}" if text else ""))
    if options:
        lines += ["Options:", "", *options, ""]
    if isinstance(command, click.Group):
        listed = [
            name for name in command.list_commands(ctx) if not getattr(command.get_command(ctx, name), "hidden", True)
        ]
        if listed:
            lines += ["Commands: " + ", ".join(f"`{name}`" for name in listed) + ".", ""]
    return lines


def render(main: click.Command) -> str:
    """The whole page for the command tree under ``main``."""
    lines = [PAGE_HEADER]
    root = click.Context(main, info_name="aisland", terminal_width=120, max_content_width=120)
    for ctx, command in _walk(main, root):
        lines += _section(ctx, command)
    return "\n".join(lines).rstrip("\n") + "\n"
