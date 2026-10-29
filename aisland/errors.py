"""aisland's exceptions: one root, each also the builtin it stands for.

Every failure aisland expects — a refusal, a file it cannot use, a tool or service that is missing — is an
:class:`AislandError`, so ``except AislandError`` catches aisland's own and nothing else. It is a
:class:`click.ClickException`, which is how the command line prints it: one ``Error: <message>`` line on stderr and
exit 1 (``aisland --debug`` shows the traceback too). A subclass also derives from the builtin it replaces
(``ValueError`` for a value aisland cannot use, ``ConnectionError`` for a server that does not answer), so a caller's
``except ValueError`` keeps working.

A failure the command goes on past is reported instead (:mod:`aisland.failures`); only an error that ends the command
is raised.
"""

import click


class AislandError(click.ClickException):
    """A failure aisland expected: its message is the whole story, printed as one ``Error:`` line."""
