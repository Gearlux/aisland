"""Per-project dependency sources (``aisland workspace configure source``): what each project's source is.

A workspace project can be consumed in exactly three ways ON THIS MACHINE; the choice drives
what has to agree with it — the checkout, the shared ``.venv`` and the editor folder list:

========== ================================= =====================================
source     directory                         ``.venv``
========== ================================= =====================================
``local``  checked out, ``git switch <ref>`` ``uv pip install -e <dir>[dev,…]``
``github`` deinit'd (empty)                  ``uv pip install git+…@<ref>``
``pypi``   deinit'd (empty)                  ``uv pip install -U <dist>``
========== ================================= =====================================

**No CI file reads a source.** A runner installs a RELEASED dependency from PyPI and an
unreleased one from GitHub ``main`` (:func:`aisland.pypi.released_on_pypi`, asked by
:func:`aisland.ci.ci_scaffold.project_facts`): CI installs what is released, and from GitHub what is not. So
``aisland workspace configure source set`` / ``unset`` never regenerate a CI file.

``local`` and ``github`` both carry a ``ref`` (branch or tag) and differ ONLY in whether the
working tree survives: ``local`` is "I develop this here", ``github`` is "pin it to a remote
commit, I am not editing it". The names are deliberately far apart because the commands are one
word apart and one of them runs ``git submodule deinit`` — ``git``/``github`` would have been a
three-character difference between keeping and deleting a checkout.

A project with no entry anywhere is *unmanaged*: nothing is switched and nothing is installed
differently — so this module changes no behaviour until a source is set.

Two configuration layers, mirroring the existing ``.gitmodules active`` +
``.git/config`` split of the projects' activation (:mod:`aisland.workspace.activation`):

* **committed** — each project's ``source``, ``version`` and ``distribution`` (only where it differs from the
  project's name) in its ``[project."<name>"]`` table of ``<workspace>/workspace.toml``, a git source's ref being the
  project's own ``branch`` (tracked, shared between machines; see :mod:`aisland.workspace.file`);
* **per-machine** — ``git config --local aisland.source.<project>.source`` (and ``.branch`` / ``.version``), never
  committed; a missing ``.branch`` is the project's committed branch.

Which layer drives what is load-bearing, because a machine-local choice must never reach a
tracked file:

* *committed source only* -> the registry's ``distribution`` field, which lets the CI scaffolder
  DISCOVER a detached project whose directory is empty (never to choose its install line);
* *effective source* (override, else committed) -> submodule init/deinit + branch switch and the
  editable/PyPI install into the shared ``.venv`` (:func:`aisland.workspace.source_apply.apply_source`) — and,
  through the checkout, the folder list of the generated, gitignored ``*.code-workspace`` file, rewritten after
  every source change (:mod:`aisland.setup.source_commands`).

This module is the model: the kinds, both layers and the effective source. Applying one is
:mod:`aisland.workspace.source_apply`; ``set`` and ``unset`` are :mod:`aisland.setup.source_commands`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple, cast, get_args

from aisland import failures, gitcmd, locations, machine_config
from aisland import pyproject as pyproject_toml
from aisland.workspace import file as workspace_file
from aisland.workspace.projects import get_workspace_root, submodule_active_map

logger = logging.getLogger(__name__)

# The closed set of source kinds. Everything that needs to enumerate them (click's
# ``Choice``, docs, future MCP/JSON schemas) derives from THIS annotation via
# ``get_args`` — never a restated parallel list.
SourceKind = Literal["local", "github", "pypi"]
SOURCE_KINDS: Tuple[str, ...] = get_args(SourceKind)

#: The kinds that carry a git ``ref``. ``pypi`` carries a ``version`` instead.
REF_KINDS: Tuple[str, ...] = ("local", "github")

#: The kinds that leave the project directory EMPTY (submodule deinit'd). Consumed by
#: :func:`detached_projects` -> :func:`aisland.workspace.activation.reconcile`.
DETACHED_KINDS: Tuple[str, ...] = ("github", "pypi")

#: A project's settings this module owns in its ``[project."<name>"]`` table of ``workspace.toml`` (beside the
#: project's own ``branch``, which a ``local`` / ``github`` source checks out or installs from).
SOURCE_KEYS: Tuple[str, ...] = ("source", "version", "distribution")

#: The settings a machine overrides, each ``aisland.source.<project>.<key>`` in ``.git/config``.
_MACHINE_KEYS: Tuple[str, ...] = ("source", "branch", "version")

#: Ref used by a ``local`` / ``github`` source that does not name one — matches the ``@main``
#: that the CI scaffolder emitted unconditionally before this module existed, so an unmanaged
#: project and an explicit ``local:main`` produce byte-identical build files.
DEFAULT_REF = "main"


@dataclass(frozen=True)
class ProjectSource:
    """Where one workspace project's package is consumed from.

    Args:
        kind: ``"local"`` (checkout on ``ref``, editable install), ``"github"`` (no checkout;
            installed from the remote at ``ref``) or ``"pypi"`` (no checkout; published wheel).
        ref: Branch or tag for the ``local`` / ``github`` kinds. Ignored by ``pypi``.
        version: PEP 440 specifier for the ``pypi`` kind (``">=0.1.0"``). ``None`` => latest.
        distribution: The PyPI / distribution name, which differs from the directory name for
            at least one project (``acme`` -> ``acme-tools``). Recorded in the committed
            file because a detached project's directory is empty and can no longer be read for it.
    """

    kind: SourceKind
    ref: str = DEFAULT_REF
    version: Optional[str] = None
    distribution: Optional[str] = None

    @property
    def detached(self) -> bool:
        """Whether this source leaves the project directory EMPTY (no checkout)."""
        return self.kind in DETACHED_KINDS

    def spec(self) -> str:
        """Compact round-trippable form: ``"local:dev/main"`` / ``"github:main"`` / ``"pypi:>=0.1.0"``."""
        if self.kind == "pypi":
            return f"pypi:{self.version}" if self.version else "pypi"
        return f"{self.kind}:{self.ref}"

    @classmethod
    def parse(cls, raw: str, *, distribution: Optional[str] = None) -> "ProjectSource":
        """Inverse of :meth:`spec`. Raises ``ValueError`` on an unknown kind."""
        kind_raw, _, rest = raw.strip().partition(":")
        kind_raw = kind_raw.strip().lower()
        rest = rest.strip()
        if kind_raw not in SOURCE_KINDS:
            raise ValueError(f"unknown source kind {kind_raw!r}; expected one of {', '.join(SOURCE_KINDS)}")
        if kind_raw == "pypi":
            return cls(kind="pypi", version=rest or None, distribution=distribution)
        return cls(kind=cast(SourceKind, kind_raw), ref=rest or DEFAULT_REF, distribution=distribution)

    def requirement(self, project: str) -> str:
        """The ``uv pip install`` argument for a ``pypi`` source (``"loggair>=0.1.0"``)."""
        return f"{self.distribution or project}{self.version or ''}"


# --- committed layer: each project's table in <workspace>/workspace.toml ------------------


#: The keys a project's table in ``workspace.toml`` holds for its source; its ref is the project's own ``branch``.
COMMITTED_KEYS = ("source", "version", "distribution")


def read_committed_sources(workspace_root: Path) -> Dict[str, ProjectSource]:
    """Each project of ``workspace.toml`` that has a ``source``; its ``branch`` is a git source's ref.

    An unknown kind is skipped with a warning naming its line; a file aisland cannot read is refused
    (:class:`aisland.workspace.file.FileError`).
    """
    result: Dict[str, ProjectSource] = {}
    for name, project in workspace_file.load(workspace_root).projects.items():
        if "source" not in project.others:
            continue
        kind = str(project.others["source"]).strip().lower()
        if kind not in SOURCE_KINDS:
            failures.warn(
                f"  - Warning: {project.where}: project {name!r} has source {kind!r}; "
                f"expected one of {', '.join(SOURCE_KINDS)}"
            )
            continue
        version = project.others.get("version")
        distribution = project.others.get("distribution")
        result[name] = ProjectSource(
            kind=cast(SourceKind, kind),
            ref=project.branch or DEFAULT_REF,
            version=str(version) if version else None,
            distribution=str(distribution) if distribution else None,
        )
    return result


def write_committed_sources(workspace_root: Path, sources: Dict[str, ProjectSource]) -> Path:
    """Set each project's source settings in ``workspace.toml`` to ``sources``, leaving every other setting alone.

    Nothing is written twice: a ``distribution`` only where it differs from the project's name, a ``version`` only for
    ``pypi``; a git source's ref is the project's ``branch``. A project left with nothing but what a source gave it is
    dropped (:func:`aisland.workspace.file.set_settings`).
    """
    values: Dict[str, Dict[str, Optional[str]]] = {}
    for name, src in sources.items():
        given: Dict[str, Optional[str]] = {"source": src.kind}
        if src.kind in REF_KINDS:
            given["branch"] = src.ref
        elif src.version:
            given["version"] = src.version
        if src.distribution and src.distribution != name:
            given["distribution"] = src.distribution
        values[name] = given
    return workspace_file.set_settings(workspace_root, SOURCE_KEYS, values)


# --- per-machine layer: .git/config aisland.source.<project>.<key> ------------------------


def _machine_settings(workspace_root: Path) -> Dict[str, Dict[str, str]]:
    """Every ``aisland.source.<project>.<key>`` entry of ``.git/config``: ``{project: {key: value}}``."""
    found: Dict[str, Dict[str, str]] = {}
    for key, value in machine_config.get_all(workspace_root, r"^aisland\.source\.").items():
        project, dot, setting = key[len("aisland.source.") :].rpartition(".")
        if dot and setting in _MACHINE_KEYS:
            found.setdefault(project, {})[setting] = value
    return found


def local_override(workspace_root: Path, project: str) -> Optional[ProjectSource]:
    """The per-machine source of ``project`` (``aisland.source.<project>.source`` and its ``.branch``/``.version``),
    or ``None`` when this machine has none. A missing branch is the project's committed one (``workspace.toml``)."""
    settings = _machine_settings(workspace_root).get(project, {})
    kind = settings.get("source")
    if not kind:
        return None
    if kind not in SOURCE_KINDS:
        failures.warn(
            f"  - Warning: ignoring aisland.source.{project}.source = {kind!r}; expected one of {', '.join(SOURCE_KINDS)}"
        )
        return None
    committed = workspace_file.load(workspace_root).projects.get(project)
    branch = settings.get("branch") or (committed.branch if committed else None) or DEFAULT_REF
    return ProjectSource(kind=cast(SourceKind, kind), ref=branch, version=settings.get("version"))


def set_local_override(workspace_root: Path, project: str, source: Optional[ProjectSource]) -> None:
    """Record (or clear, when ``source`` is ``None``) the per-machine source, one key each. Never committed."""
    values: Dict[str, str] = {}
    if source is not None:
        values["source"] = source.kind
        if source.kind in REF_KINDS:
            values["branch"] = source.ref
        elif source.version:
            values["version"] = source.version
    for key in _MACHINE_KEYS:
        machine_config.put(workspace_root, f"aisland.source.{project}.{key}", values.get(key))


def effective_source(
    workspace_root: Path,
    project: str,
    committed: Optional[Dict[str, ProjectSource]] = None,
) -> Optional[ProjectSource]:
    """The source that governs THIS machine's checkout and venv: override, else committed, else ``None``.

    A compact override (``"pypi"``) carries no distribution name, so it inherits the committed
    entry's — otherwise switching a machine to PyPI would try to install the directory name
    (``acme``) instead of the distribution (``acme-tools``).
    """
    if committed is None:
        committed = read_committed_sources(workspace_root)
    override = local_override(workspace_root, project)
    if override is None:
        return committed.get(project)
    if override.distribution is None and project in committed:
        return ProjectSource(
            kind=override.kind,
            ref=override.ref,
            version=override.version,
            distribution=committed[project].distribution,
        )
    return override


def project_path(workspace_root: Path, project: str) -> Path:
    """Absolute path of ``project``'s directory, honouring a ``.gitmodules`` path that differs from its name."""
    for name, path, _active in submodule_active_map(workspace_root):
        if name == project:
            return workspace_root / path
    return workspace_root / project


def discover_distribution(project_dir: Path) -> Optional[str]:
    """The ``[project] name`` of a checked-out project, or ``None`` when it cannot be read.

    Called BEFORE a ``pypi`` switch deinits the directory — afterwards there is no
    ``pyproject.toml`` left to ask, which is exactly why the answer is persisted into the
    committed registry.
    """
    return pyproject_toml.name(project_dir / "pyproject.toml")


def remote_url(workspace_root: Path, project: str) -> Optional[str]:
    """The submodule's configured remote from ``.gitmodules``, or ``None`` when undeclared.

    Read rather than built from a GitHub owner and the name, so a ``github`` source also
    works for the submodules that are NOT on GitHub (a private project on an internal Bitbucket), and so it always matches the remote the operator's git auth is
    already set up for.
    """
    gitmodules = workspace_root / ".gitmodules"
    if not gitmodules.exists():
        return None
    return gitcmd.read(workspace_root, "config", "-f", ".gitmodules", "--get", f"submodule.{project}.url") or None


def pip_vcs_url(url: str, ref: str) -> str:
    """Turn a git remote into a ``uv pip install`` VCS requirement pinned at ``ref``.

    ``git@host:org/repo.git`` is scp-style syntax that pip cannot parse — the colon must become
    a ``/`` under an explicit ``ssh://`` scheme. An https remote just gains the ``git+`` prefix.
    A ref containing a slash (``dev/main``) is fine after the ``@``; pip splits on the LAST one.
    """
    if url.startswith("git@"):
        host, _, path = url.partition(":")
        url = f"ssh://{host}/{path}"
    if not url.startswith("git+"):
        url = f"git+{url}"
    return f"{url}@{ref}"


def overridden_projects(workspace_root: Path) -> List[str]:
    """Project names carrying a per-machine source in ``.git/config`` (``aisland.source.<project>.source``)."""
    return [project for project, settings in _machine_settings(workspace_root).items() if "source" in settings]


def detached_projects(workspace_root: Path) -> set[str]:
    """Projects whose EFFECTIVE source empties their directory (``github`` or ``pypi``).

    Consumed by :func:`aisland.workspace.activation.reconcile` so the activation machinery and the
    source machinery cannot fight over the same submodule.
    """
    committed = read_committed_sources(workspace_root)
    managed = set(committed) | set(overridden_projects(workspace_root))
    result: set[str] = set()
    for project in managed:
        source = effective_source(workspace_root, project, committed)
        if source is not None and source.detached:
            result.add(project)
    return result


# --- reporting ----------------------------------------------------------------------------


def checkout_state(workspace_root: Path, project: str) -> str:
    """Human-readable on-disk state: the checked-out branch, or ``empty`` for a deinit'd directory."""
    path = project_path(workspace_root, project)
    if not (path / ".git").exists():
        return "empty"
    branch = gitcmd.git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return f"on {branch}" if branch else "checked out"


def known_projects(workspace_root: Path) -> List[str]:
    """Every project ``aisland workspace configure source`` can manage: declared submodules plus any sibling with a pyproject (a link to
    another folder of the workspace is none, :func:`aisland.locations.is_alias`)."""
    names = {name for name, _path, _active in submodule_active_map(workspace_root)}
    for child in workspace_root.iterdir():
        if child.is_dir() and (child / "pyproject.toml").exists() and not locations.is_alias(child, workspace_root):
            names.add(child.name)
    return sorted(names)


def describe(workspace_root: Optional[Path] = None) -> List[Dict[str, str]]:
    """One row per managed project for ``aisland workspace configure source``."""
    root = workspace_root or get_workspace_root()
    committed = read_committed_sources(root)
    rows: List[Dict[str, str]] = []
    for project in sorted(set(committed) | set(overridden_projects(root))):
        override = local_override(root, project)
        effective = effective_source(root, project, committed)
        rows.append(
            {
                "project": project,
                "committed": committed[project].spec() if project in committed else "-",
                "override": override.spec() if override else "-",
                "effective": effective.spec() if effective else "-",
                "state": checkout_state(root, project),
            }
        )
    return rows
