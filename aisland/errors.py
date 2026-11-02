"""aisland's exceptions: one root, each also the builtin it stands for.

Every failure aisland expects — a refusal, a file it cannot use, a tool or service that is missing — is an
:class:`AislandError`, so ``except AislandError`` catches aisland's own and nothing else. It is a
:class:`click.ClickException`, which is how the command line prints it: one ``Error: <message>`` line on stderr and
exit 1 (``aisland --debug`` shows the traceback too). A subclass also derives from the builtin it replaces
(``ValueError`` for a value aisland cannot use, ``ConnectionError`` for a server that does not answer), so a caller's
``except ValueError`` keeps working.

A failure the command goes on past is reported instead (:mod:`aisland.failures`); only an error that ends the command
is raised.

The one place aisland swallows a failure of its own is a shell-completion callback (:func:`quiet_completion`): it runs
inside the user's shell on every TAB, where a traceback would break the line being typed.
"""

import functools
from typing import Callable, List, ParamSpec, TypeVar

import click

P = ParamSpec("P")
T = TypeVar("T")


class AislandError(click.ClickException):
    """A failure aisland expected: its message is the whole story, printed as one ``Error:`` line."""


def quiet_completion(callback: Callable[P, List[T]]) -> Callable[P, List[T]]:
    """``callback``, a shell-completion function, answering nothing instead of raising: a workspace it cannot read, a
    cache file half written or a bug of its own offers no candidates, never a traceback in the user's shell."""

    @functools.wraps(callback)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> List[T]:
        try:
            return callback(*args, **kwargs)
        except Exception:  # a completion callback must never break the shell
            return []

    return guarded
