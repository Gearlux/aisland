"""Where something is in a TOML file: the file and line of a table, and the line of a key inside it.

Every refusal of a file aisland reads — a project's ``pyproject.toml``, ``workspace.toml``, a template set's
``template.toml``, a harness set's ``harness.toml`` — names ``file:line``; these find that line in the text, since
``tomllib`` keeps no positions. A leaf module: it imports nothing of aisland, so any reader can use it.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Where:
    """A place in a TOML file: the file, and the line of the table (``None`` when not found)."""

    file: Path
    line: Optional[int]

    def __str__(self) -> str:
        return f"{self.file}:{self.line}" if self.line else str(self.file)


def _part(name: str) -> str:
    """One dotted-key part as a pattern: bare, or quoted either way."""
    escaped = re.escape(name)
    return rf'(?:{escaped}|"{escaped}"|\'{escaped}\')'


def table_line(text: str, *parts: str) -> Optional[int]:
    """The 1-based line of the table header ``[a.b.c]`` (spaces and quoting allowed), or None."""
    dotted = r"\s*\.\s*".join(_part(part) for part in parts)
    pattern = re.compile(rf"^\s*\[\s*{dotted}\s*\]")
    for number, line in enumerate(text.splitlines(), start=1):
        if pattern.match(line):
            return number
    return None


def key_line(text: str, header: int, key: str) -> Optional[int]:
    """The line of ``key = …`` below the table header on line ``header``: the table's own, since valid TOML keeps a
    table's keys under its header and cannot define the key again in a later table."""
    pattern = re.compile(rf"^\s*{_part(key)}\s*=")
    lines = text.splitlines()[header:]
    return next((number for number, line in enumerate(lines, start=header + 1) if pattern.match(line)), None)
