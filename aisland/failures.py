"""Failures a command reports and goes on past, so the run still exits 1.

A failure that ends a command (no such project, no ``.venv``) is raised as a ``click.ClickException``: one ``Error:``
line, exit 1. Some failures must not end the command: ``aisland setup`` installs every project even when one fails,
``aisland services start`` starts the others when one does not come up, a Jenkins step that fails leaves the next one
to run. Such a failure is :func:`report`-ed: its line goes to stderr, the command carries on, and the CLI's group
(``cli._WorkspaceGroup.invoke``) turns a run that reported any into exit 1 — so a script, CI or an MCP client sees
the failure the user reads. A :func:`warn`-ing goes to stderr too and fails nothing. Standard library and click
only: everything else imports it.
"""

from __future__ import annotations

import contextvars

from aisland import console

#: How many failures this run reported — per thread and task, as :mod:`aisland.console`'s console is, so two MCP tool
#: calls served at once never count each other's.
_reported: contextvars.ContextVar[int] = contextvars.ContextVar("aisland_failures_reported", default=0)


def report(message: str) -> None:
    """Print ``message`` (an ``Error:`` line) to stderr and mark this run failed; the caller goes on."""
    console.echo(message, err=True)
    _reported.set(_reported.get() + 1)


def warn(message: str) -> None:
    """Print ``message`` (a ``Warning:`` line) to stderr; the run is not failed. Never stdout: a command's output
    is what a script reads (``--json``), and a warning in it would break the reading."""
    console.echo(message, err=True)


def reported() -> int:
    """How many failures this run reported."""
    return _reported.get()


def reset() -> None:
    """Forget the failures reported so far — the CLI does it as each command starts."""
    _reported.set(0)
