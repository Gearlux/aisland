"""What aisland says and asks, and who hears it.

Library code never writes to the terminal itself: it calls :func:`echo`, :func:`confirm` and :func:`prompt` here, and
the console in charge answers. In a terminal that is click (:class:`Terminal`), so a command prints and asks as it
always did. The MCP server runs each tool inside :func:`collecting`: what the code says is kept instead of written —
the server's stdout is its JSON-RPC stream — and a question raises :class:`NoOneToAsk`, since no person is there to
answer and a tool must never act on a guessed answer.

The console is a context variable, so a tool call (a thread or a task of its own) changes nothing for another.
"""

import contextvars
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, List, Protocol

import click

from aisland.errors import AislandError


class NoOneToAsk(AislandError, RuntimeError):
    """A question asked where no person can answer (an MCP tool call)."""


class Console(Protocol):
    """Where aisland's output goes and its questions are answered."""

    def echo(self, message: object = None, *, err: bool = False, nl: bool = True) -> None:
        """Say ``message``: a result or progress on stdout, a warning or an error on stderr (``err``)."""

    def confirm(self, question: str, default: bool = False) -> bool:
        """A yes or a no to ``question``."""

    def prompt(self, text: str, **options: Any) -> Any:  # Any: click.prompt's options and answer, passed as they are
        """An answer typed for ``text``."""


class Terminal:
    """The person at the terminal, through click."""

    def echo(self, message: object = None, *, err: bool = False, nl: bool = True) -> None:
        click.echo(message, err=err, nl=nl)

    def confirm(self, question: str, default: bool = False) -> bool:
        return click.confirm(question, default=default)

    def prompt(self, text: str, **options: Any) -> Any:  # Any: as Console.prompt
        return click.prompt(text, **options)


@dataclass
class Collector:
    """No terminal: every line said is kept, unstyled, and every question is refused."""

    out: List[str] = field(default_factory=list)
    err: List[str] = field(default_factory=list)

    def echo(self, message: object = None, *, err: bool = False, nl: bool = True) -> None:
        text = click.unstyle("" if message is None else str(message))
        (self.err if err else self.out).extend(text.splitlines() or [""])

    def confirm(self, question: str, default: bool = False) -> bool:
        raise NoOneToAsk(f"no one is here to answer: {question}")

    def prompt(self, text: str, **options: Any) -> Any:  # Any: as Console.prompt
        raise NoOneToAsk(f"no one is here to answer: {text}")


_current: contextvars.ContextVar[Console] = contextvars.ContextVar("aisland_console", default=Terminal())


def echo(message: object = None, *, err: bool = False, nl: bool = True) -> None:
    """Say ``message`` to the console in charge."""
    _current.get().echo(message, err=err, nl=nl)


def confirm(question: str, default: bool = False) -> bool:
    """Ask the console in charge ``question``."""
    return _current.get().confirm(question, default=default)


def prompt(text: str, **options: Any) -> Any:  # Any: as Console.prompt
    """Ask the console in charge for an answer to ``text``."""
    return _current.get().prompt(text, **options)


def can_ask() -> bool:
    """Whether a person can answer a question now: the console in charge is the terminal, and standard input and
    output are both one — a script, CI and an MCP tool call are not."""
    return isinstance(_current.get(), Terminal) and sys.stdin.isatty() and sys.stdout.isatty()


@contextmanager
def collecting() -> Iterator[Collector]:
    """Keep what is said inside the block, and refuse every question asked there."""
    collector = Collector()
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)
