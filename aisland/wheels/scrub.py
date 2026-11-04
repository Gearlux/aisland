"""What a wheel keeps of its METADATA when its folder goes into a repository that is not on GitHub.

A project whose own remote is elsewhere (a company's server) must not reveal the workspace it is developed in, and the
wheels ``aisland wheels build`` writes beside it are the workspace's own projects: their METADATA carries each one's
README (with its links to the GitHub repositories), its GitHub URLs and an author naming the workspace or its GitHub
owner. :func:`scrub_wheel` rewrites a wheel without them. What installs stays as built: the code, the requirements (a
third-party ``git+https`` requirement among them), the entry points. The rewrite is a function of the wheel, so a
rebuild of an unchanged project still gives the same file.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from aisland.errors import AislandError

#: The fields that hold a URL: one naming GitHub goes.
URL_FIELDS = ("home-page", "download-url", "project-url")

#: The fields that name a person or an organisation: one naming the workspace or a GitHub owner goes.
AUTHOR_FIELDS = ("author", "author-email", "maintainer", "maintainer-email")

#: The description, which is the project's README: in the body, or (older metadata) in a field of its own. It goes.
DESCRIPTION_FIELDS = ("description", "description-content-type")

_GITHUB = "github.com"

#: A dist-info file at the wheel's top level.
_DIST_INFO = re.compile(r"[^/]+\.dist-info/(?P<file>[^/]+)")


def scrub_metadata(text: str, names: Sequence[str]) -> str:
    """``text``, a wheel's METADATA, without its description, its URL fields naming GitHub and its author and
    maintainer fields naming one of ``names`` (in any case). Every other field stays as it is, in its place."""
    head = text.partition("\n\n")[0]
    lowered = [name.lower() for name in names if name]
    kept = [lines for key, lines in _fields(head) if not _drops(key, lines, lowered)]
    return "".join(f"{line}\n" for lines in kept for line in lines)


def _fields(head: str) -> List[Tuple[str, List[str]]]:
    """The header's fields in order: each one's key (lower case) and its lines, continuation lines included."""
    fields: List[Tuple[str, List[str]]] = []
    for line in head.splitlines():
        if line[:1] in (" ", "\t") and fields:
            fields[-1][1].append(line)
        else:
            fields.append((line.partition(":")[0].strip().lower(), [line]))
    return fields


def _drops(key: str, lines: List[str], names: Sequence[str]) -> bool:
    value = "\n".join(lines).partition(":")[2].lower()
    if key in DESCRIPTION_FIELDS:
        return True
    if key in URL_FIELDS:
        return _GITHUB in value
    if key in AUTHOR_FIELDS:
        return any(name in value for name in names)
    return False


def scrub_wheel(wheel: Path, names: Sequence[str]) -> bool:
    """Rewrite ``wheel`` in place with its METADATA scrubbed (:func:`scrub_metadata`) and RECORD's line for it
    following; every other member is copied as it is, with its own time and attributes. Returns whether the wheel
    changed: one with nothing to scrub is left untouched."""
    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        contents: Dict[str, bytes] = {info.filename: archive.read(info) for info in infos}
    metadata = _dist_info_file(wheel, contents, "METADATA")
    old = contents[metadata]
    new = scrub_metadata(old.decode("utf-8"), names).encode("utf-8")
    if new == old:
        return False
    contents[metadata] = new
    record = _dist_info_file(wheel, contents, "RECORD")
    contents[record] = _record_with(contents[record].decode("utf-8"), metadata, new).encode("utf-8")
    part = wheel.with_name(f"{wheel.name}.part")
    with zipfile.ZipFile(part, "w") as archive:
        for info in infos:
            archive.writestr(info, contents[info.filename])
    os.replace(part, wheel)
    return True


def _dist_info_file(wheel: Path, contents: Dict[str, bytes], name: str) -> str:
    found = [m for m in contents if (match := _DIST_INFO.fullmatch(m)) and match["file"] == name]
    if len(found) != 1:
        raise AislandError(f"{wheel.name}: expected one dist-info {name}, found {len(found)}")
    return found[0]


def _record_with(record: str, member: str, data: bytes) -> str:
    """``record`` with ``member``'s line naming ``data``'s digest and size; its line ending is kept, and a RECORD
    without the line is returned as it is."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    lines = record.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.split(",", 1)[0] == member:
            ending = line[len(line.rstrip("\r\n")) :]
            lines[index] = f"{member},sha256={digest},{len(data)}{ending}"
    return "".join(lines)
