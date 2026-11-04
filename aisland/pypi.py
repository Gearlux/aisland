"""PyPI's JSON document for a distribution, and the question the CI scaffolder asks of it: is it RELEASED?

:func:`pypi_json` is the one request; :func:`points_at_owner` is the provenance marker (a release whose metadata names
the GitHub owner of the project's own remote is the project's, not a stranger's of the same global name). What
``aisland workspace configure source`` and ``aisland wheels`` ask of the document is theirs
(:func:`aisland.sources._pypi_provenance`, :func:`aisland.wheels.build.pypi_state`).
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict, List, Literal, Optional, Tuple, TypedDict

from packaging.version import InvalidVersion, Version

from aisland.errors import AislandError


class ReleaseProbeError(AislandError, ConnectionError):
    """PyPI could not answer whether ``distribution`` is released — the scaffold must stop, not guess."""

    def __init__(self, distribution: str, reason: str) -> None:
        super().__init__(
            f"cannot tell whether {distribution!r} is released: {reason}. The CI files were not written — "
            f"generating them needs PyPI, because a guess would flip every released dependency to a "
            f"GitHub install line. Retry when PyPI is reachable."
        )
        self.distribution = distribution


class PypiFile(TypedDict, total=False):
    """One file of a release in PyPI's JSON document (``urls``, ``releases``): the keys aisland reads."""

    filename: str
    url: str
    packagetype: str
    yanked: bool


class PypiInfo(TypedDict, total=False):
    """The ``info`` of PyPI's JSON document: the keys aisland reads."""

    version: str
    home_page: Optional[str]
    project_urls: Optional[Dict[str, str]]


class PypiProject(TypedDict, total=False):
    """PyPI's JSON document for a distribution (``https://pypi.org/pypi/<name>/json``): the keys aisland reads."""

    info: PypiInfo
    releases: Dict[str, List[PypiFile]]
    urls: List[PypiFile]


def pypi_json(distribution: str) -> Tuple[Literal["ok", "missing", "unreachable"], Any]:  # Any: see the docstring
    """PyPI's JSON document for ``distribution``: ``("ok", data)``, ``("missing", None)`` on a 404,
    or ``("unreachable", reason)`` — shared by the provenance probe and the release probe. The second item is
    ``Any``: a :class:`PypiProject`, ``None`` or a reason, told apart by the first, which a union cannot say."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{distribution}/json", timeout=5) as resp:
            return "ok", json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "missing", None
        return "unreachable", f"PyPI returned HTTP {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return "unreachable", f"could not reach PyPI ({e})"


def points_at_owner(info: PypiInfo, owner: Optional[str]) -> bool:
    """The provenance marker: the published metadata names a ``github.com/<owner>/…`` URL (GitHub owners are
    case-insensitive). No owner — a project whose remote is not on GitHub — matches nothing."""
    if owner is None:
        return False
    prefix = f"github.com/{owner}/".lower()
    urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
    return any(prefix in str(url).lower() for url in urls)


@functools.lru_cache(maxsize=None)
def released_on_pypi(distribution: str, owner: str) -> bool:
    """Whether ``distribution`` is RELEASED: PyPI holds at least one FINAL (non-pre-release)
    version with a file that is not yanked, and its metadata points at ``github.com/<owner>/``,
    the owner of the project's own remote.

    The ONE question the CI scaffolder asks per internal dependency (user ruling 2026-09-26):
    released -> the runner installs it from PyPI (``.[dev]`` resolves it); not released -> from
    GitHub ``main``. The workspace SOURCE of the dependency (``workspace.toml``) plays no part —
    it describes one machine's checkout, never what a runner builds against. A pre-release
    alone (``recordstream`` 0.1.0a1) is not a release: a plain ``>=0.1.0`` excludes it, and the
    project is still developed from ``main``. A stranger's package under the same global name
    is not ours (the provenance rule :func:`_pypi_provenance` applies).

    Unlike :func:`_pypi_provenance` — where an unreachable index warns and continues — this
    RAISES :class:`ReleaseProbeError`: a guess here would rewrite committed CI files, and
    "assume unreleased" flips every released dependency to a GitHub line. Cached per process —
    a workspace scaffold asks about the same few distributions for every project.
    """
    status, payload = pypi_json(distribution)
    if status == "missing":
        return False
    if status == "unreachable":
        raise ReleaseProbeError(distribution, str(payload))
    project: PypiProject = payload
    info: PypiInfo = project.get("info") or {}
    if not points_at_owner(info, owner):
        return False
    for version, files in (project.get("releases") or {}).items():
        try:
            final = not Version(version).is_prerelease
        except InvalidVersion:
            continue
        if final and any(not f.get("yanked", False) for f in files or []):
            return True
    return False
