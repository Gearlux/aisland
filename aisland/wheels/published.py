"""What PyPI has of a workspace project, as ``aisland wheels`` asks it, and the PyPI wheel its comparison downloads.

Published = a version under the name whose metadata points at ``github.com/<owner>/`` — the owner of the project's
own remote — and has a file that is not yanked (:func:`pypi_state`). Pre-releases COUNT (recordstream 0.1.0a2: pip
takes it when no final release exists), which is where this question differs from
:func:`aisland.pypi.released_on_pypi`, the CI scaffolder's. A stranger's package under the same name is not ours —
and is reported, because pip takes the higher version of the two.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional, Tuple

from aisland import downloads
from aisland.errors import AislandError
from aisland.pypi import PypiFile, PypiInfo, PypiProject, points_at_owner, pypi_json

#: PyPI's answer for a project: this workspace's release, none, or a stranger's package under the name.
PypiState = Literal["published", "absent", "foreign"]


def pypi_state(distribution: str, owner: Optional[str]) -> Tuple[PypiState, Optional[PypiProject]]:
    """Whether PyPI has ``distribution`` as THIS workspace's project, with PyPI's JSON when it answered.

    The workspace's project is the release whose metadata names ``github.com/<owner>/``, the owner of the project's
    own remote; a project whose remote is not on GitHub (``owner`` None) has no release aisland can tell apart from a
    stranger's (``foreign``).

    Raises ``click.ClickException`` when PyPI cannot be reached: a guess would either ship a
    wheel PyPI already serves or leave out one it does not.
    """
    status, payload = pypi_json(distribution)
    if status == "missing":
        return "absent", None
    if status == "unreachable":
        raise AislandError(f"cannot tell whether {distribution} is on PyPI: {payload}. Retry when it answers.")
    project: PypiProject = payload
    info: PypiInfo = project.get("info") or {}
    if not points_at_owner(info, owner):
        return "foreign", project
    for files in (project.get("releases") or {}).values():
        if any(not f.get("yanked", False) for f in files or []):
            return "published", project
    return "absent", None


def latest_version(payload: Optional[PypiProject]) -> str:
    """The version PyPI's JSON names as the project's latest (``""`` without one)."""
    info: PypiInfo = (payload or {}).get("info") or {}
    return str(info.get("version") or "")


def pure_wheel(payload: PypiProject) -> Optional[PypiFile]:
    """The pure-Python wheel among PyPI's files of its latest version."""
    entries: List[PypiFile] = payload.get("urls") or []
    for entry in entries:
        if entry.get("packagetype") == "bdist_wheel" and str(entry.get("filename", "")).endswith("-none-any.whl"):
            return entry
    return None


def download_wheel(entry: PypiFile, dest: Path) -> Path:
    """PyPI's file ``entry``, downloaded to ``dest`` and checked against the SHA-256 PyPI lists for it. An entry that
    lists none is refused before anything is fetched: PyPI lists one for every file, and a file nothing vouches for is
    no copy of the release the comparison could trust."""
    wanted = (entry.get("digests") or {}).get("sha256")
    if not wanted:
        raise AislandError(
            f"PyPI lists no SHA-256 for {entry.get('filename') or entry['url']}, so the file cannot be checked; "
            "nothing was downloaded. Retry when PyPI (or the index in between) lists its digests."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    downloads.download(str(entry["url"]), dest)
    if downloads.sha256(dest) != wanted:
        dest.unlink()
        raise AislandError(f"{entry['url']} is not the file PyPI lists: its SHA-256 differs from {wanted}")
    return dest
