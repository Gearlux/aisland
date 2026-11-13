"""The workspace ``.env``, read and written in the shell's dialect.

``project.bashrc`` runs ``set -a; source .env``, so the file means what bash makes of it: ``export``, quotes (single
literal, double expanded, ``\\`` escapes), ``$VAR`` and ``${VAR}`` from an earlier line then the environment, a
trailing comment dropped. :func:`read` is the ONE reader of that dialect — the services, the supervisor's ``apply`` and
Jenkins all ask it — and :func:`write` the writer services publish through, quoting a value so both read it back as
written. Imports nothing of aisland but :mod:`aisland.atomic`, :mod:`aisland.locations` and :mod:`aisland.paths`.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aisland import atomic, locations, paths

#: A variable's name, as the shell reads one.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: A value the shell reads back as written without quotes.
_BARE = re.compile(r"[A-Za-z0-9_./:@%+,=-]*")


def _expand(raw: str, start: int, known: Dict[str, str]) -> Tuple[str, int]:
    """The ``$NAME`` / ``${NAME}`` at ``raw[start]``, expanded as the shell does — an earlier ``.env`` line first, then
    the environment, unset is empty — and where the text goes on. A ``${…}`` that is no plain name stays as written."""
    if raw.startswith("${", start):
        stop = raw.find("}", start + 2)
        if stop < 0:
            return raw[start:], len(raw)
        name = raw[start + 2 : stop]
        if not _NAME.fullmatch(name):
            return raw[start : stop + 1], stop + 1
        return known.get(name, os.environ.get(name, "")), stop + 1
    match = _NAME.match(raw, start + 1)
    if match is None:
        return "$", start + 1
    return known.get(match.group(), os.environ.get(match.group(), "")), match.end()


def _value(raw: str, known: Dict[str, str]) -> str:
    """The value the shell assigns from ``raw``, the text after ``NAME=``: single quotes literal, double quotes and bare
    text expanded, a backslash escaping the next character, unquoted whitespace ending it (a `` # note`` after it)."""
    out: List[str] = []
    index, size = 0, len(raw)
    while index < size:
        char = raw[index]
        if char == "'":
            stop = raw.find("'", index + 1)
            stop = size if stop < 0 else stop
            out.append(raw[index + 1 : stop])
            index = stop + 1
        elif char == '"':
            index += 1
            while index < size and raw[index] != '"':
                if raw[index] == "\\" and index + 1 < size and raw[index + 1] in '"\\$`':
                    out.append(raw[index + 1])
                    index += 2
                elif raw[index] == "$":
                    text, index = _expand(raw, index, known)
                    out.append(text)
                else:
                    out.append(raw[index])
                    index += 1
            index += 1
        elif char == "$":
            text, index = _expand(raw, index, known)
            out.append(text)
        elif char == "\\" and index + 1 < size:
            out.append(raw[index + 1])
            index += 2
        elif char in " \t":
            break
        else:
            out.append(char)
            index += 1
    return "".join(out)


def read(workspace_root: Optional[Path] = None) -> Dict[str, str]:
    """The workspace ``.env`` as the shell reads it — ``project.bashrc`` runs ``set -a; source .env`` — so every service
    and Jenkins sees the values a run in a terminal sees: ``export`` and quotes understood, ``$VAR`` and ``${VAR}``
    expanded (an earlier line first, then the environment), a trailing comment dropped. A line the shell reads as no
    assignment (``KEY = value``, a name with a dash) sets nothing. The one reader of the file: used by the services
    that publish and consume workspace configuration, by :func:`aisland.services.supervisor.apply` for ``${VAR}`` expansion,
    and by Jenkins. ``workspace_root`` defaults to the discovered root."""
    env_file = (workspace_root or locations.workspace_root()) / ".env"
    if not env_file.exists():
        return {}
    out: Dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, equals, raw = line.partition("=")
        if not equals or not _NAME.fullmatch(name):
            continue
        out[name] = _value(raw, out)
    return out


def _quoted(value: str) -> str:
    """``value`` written so the shell reads it back as it is: bare when nothing in it is special, else single-quoted."""
    if _BARE.fullmatch(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _assigns(line: str) -> Optional[Tuple[str, bool]]:
    """The name a line sets, as :func:`read` reads it, and whether it says ``export``; None for any other line."""
    stripped = line.strip()
    exported = stripped.startswith("export ")
    if exported:
        stripped = stripped[len("export ") :].lstrip()
    name, equals, _raw = stripped.partition("=")
    return (name, exported) if equals and _NAME.fullmatch(name) else None


def write(values: Dict[str, str]) -> Path:
    """Set each ``KEY=VALUE`` in the workspace ``.env``, replacing an existing key IN PLACE.

    The counterpart of :func:`read`, for services that publish what they
    decided (a served directory, a URL) so a config or a later start can read it back as
    ``$KEY``. Neighbouring lines — comments included — are left untouched; a key not yet
    present is appended. A key set on several lines has its LAST one replaced — the one the shell and :func:`read`
    keep — its ``export`` kept. A value is quoted when the shell would otherwise change it (a space,
    a ``$``, a quote), so both the shell and the reader get it back as written. The read and the write are one step
    under one lock (:func:`aisland.atomic.locked`, its file in the state's data folder, never beside ``.env`` in the
    workspace root): two services starting at once each keep their key.
    """
    env_file = locations.workspace_root() / ".env"
    with atomic.locked(paths.data_dir() / "env"):
        lines = env_file.read_text().splitlines() if env_file.exists() else []
        for key, value in values.items():
            entry = f"{key}={_quoted(value)}"
            for index in reversed(range(len(lines))):
                assigned = _assigns(lines[index])
                if assigned is not None and assigned[0] == key:
                    lines[index] = f"export {entry}" if assigned[1] else entry
                    break
            else:
                lines.append(entry)
        # Whole or not at all, and a new one readable by its owner alone: it holds the workspace's secrets.
        atomic.write_text(env_file, "\n".join(lines) + "\n", new_mode=atomic.PRIVATE)
    return env_file
