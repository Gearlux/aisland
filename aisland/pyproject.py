"""The one reader of a project's ``pyproject.toml``.

Fifteen places in nine modules opened the file and parsed it with ``tomllib``, each with an error handling of its
own: most took a missing or broken file as one declaring nothing, two refused it naming the file, the rest let
``tomllib``'s own error through. One module keeps those three answers from drifting apart, and is where a new reader
starts:

- :func:`read` — the tolerant answer: a file that is missing, cannot be read or is no valid TOML declares nothing
  (``{}``). For a reader that lists or probes (the MCP servers, the uv overrides, a selection's choices), where
  discovery is what refuses a broken file.
- :func:`parse` — the refusing answer, over text the caller holds (discovery needs it for the line numbers it names;
  ``aisland wheels build --extra`` parses the file without its generated blocks): a :class:`PyprojectError`
  ``<file>: <tomllib's message>``, which names the line and column.
- :func:`load` — the file as ``tomllib`` reads it, its errors as they are, for a reader whose file was already found
  and checked (a project discovery listed).

:func:`name` is the ``[project] name`` two readers want. The standard-library-only modules (the wheel zip's
installer, the tunnel's shell, the pytest plugin, :mod:`aisland.locations`) and the harness scripts keep their own
reads: they run where aisland is not installed.
"""

import logging
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

from aisland.errors import AislandError

logger = logging.getLogger(__name__)


#: A parsed TOML table, as ``tomllib`` returns it.
Table = Dict[str, Any]  # Any: TOML's values; each reader checks the ones it uses


class PyprojectError(AislandError, ValueError):
    """A ``pyproject.toml`` that is no valid TOML: ``<file>: <tomllib's message>``, its line and column named."""


def load(path: Path) -> Table:
    """``path`` parsed, as ``tomllib.load`` reads it: a missing file raises :class:`FileNotFoundError` and broken TOML
    :class:`tomllib.TOMLDecodeError`."""
    logger.debug("reading %s", path)
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def read(path: Path) -> Table:
    """``path`` parsed, or ``{}`` when it is missing, cannot be read or is no valid TOML."""
    try:
        return load(path)
    except (OSError, tomllib.TOMLDecodeError) as error:
        logger.debug("%s declares nothing: %s", path, error)
        return {}


def parse(text: str, path: Path) -> Table:
    """``text``, the content of ``path``, parsed. Raises :class:`PyprojectError` naming ``path`` when it is no valid
    TOML."""
    logger.debug("parsing %s", path)
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PyprojectError(f"{path}: {error}") from error


def name(path: Path) -> Optional[str]:
    """The ``[project] name`` of ``path``; ``None`` when the file is missing or unreadable or names no project."""
    found = (read(path).get("project") or {}).get("name")
    return found if isinstance(found, str) and found else None
