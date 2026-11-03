"""Per-project dependency-source management (``aisland workspace configure source``).

A workspace project can be consumed in exactly three ways ON THIS MACHINE, and this module owns
the choice plus the artifacts that have to agree with it — the checkout, the shared ``.venv``
and the editor folder list:

========== ================================= =====================================
source     directory                         ``.venv``
========== ================================= =====================================
``local``  checked out, ``git switch <ref>`` ``uv pip install -e <dir>[dev,…]``
``github`` deinit'd (empty)                  ``uv pip install git+…@<ref>``
``pypi``   deinit'd (empty)                  ``uv pip install -U <dist>``
========== ================================= =====================================

**No CI file reads a source.** A runner installs a RELEASED dependency from PyPI and an
unreleased one from GitHub ``main`` (:func:`released_on_pypi`, asked by
:func:`aisland.ci_scaffold.project_facts`) — user ruling 2026-09-26:
"it should always point to released packages, and to GitHub for non-released ones". So
``aisland workspace configure source set`` / ``unset`` never regenerate a CI file.

``local`` and ``github`` both carry a ``ref`` (branch or tag) and differ ONLY in whether the
working tree survives: ``local`` is "I develop this here", ``github`` is "pin it to a remote
commit, I am not editing it". The names are deliberately far apart because the commands are one
word apart and one of them runs ``git submodule deinit`` — ``git``/``github`` would have been a
three-character difference between keeping and deleting a checkout.

A project with no entry anywhere is *unmanaged*: nothing is switched and nothing is installed
differently — so this module changes no behaviour until a source is set.

Two configuration layers, mirroring the existing ``.gitmodules active`` +
``.git/config`` split of the projects' activation (:mod:`aisland.activation`):

* **committed** — each project's ``source``, ``version`` and ``distribution`` (only where it differs from the
  project's name) in its ``[project."<name>"]`` table of ``<workspace>/workspace.toml``, a git source's ref being the
  project's own ``branch`` (tracked, shared between machines; see :mod:`aisland.workspace_file`);
* **per-machine** — ``git config --local aisland.source.<project>.source`` (and ``.branch`` / ``.version``), never
  committed; a missing ``.branch`` is the project's committed branch.

Which layer drives what is load-bearing, because a machine-local choice must never reach a
tracked file:

* *committed source only* -> the registry's ``distribution`` field, which lets the CI scaffolder
  DISCOVER a detached project whose directory is empty (never to choose its install line);
* *effective source* (override, else committed) -> submodule init/deinit + branch switch and the
  editable/PyPI install into the shared ``.venv`` (via :func:`apply_source`) — and, through the
  checkout, the folder list of the generated, gitignored ``*.code-workspace`` file
  (:func:`aisland.editor_files.write_code_workspace`, rewritten after every source change).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple, cast, get_args

import click

from aisland import failures, gitcmd, locations, machine_config
from aisland import pyproject as pyproject_toml
from aisland import workspace_file
from aisland.editor_files import write_code_workspace
from aisland.errors import AislandError
from aisland.github import github_repository
from aisland.installers import ensure_uv_in_venv
from aisland.mcp_config import no_venv
from aisland.projects import get_workspace_root, set_submodule_active, submodule_active_map
from aisland.pypi import PypiInfo, PypiProject, points_at_owner, pypi_json
from aisland.selection import editable_target

logger = logging.getLogger(__name__)

# The closed set of source kinds. Everything that needs to enumerate them (click's
# ``Choice``, docs, future MCP/JSON schemas) derives from THIS annotation via
# ``get_args`` — never a restated parallel list.
SourceKind = Literal["local", "github", "pypi"]
SOURCE_KINDS: Tuple[str, ...] = get_args(SourceKind)

#: The kinds that carry a git ``ref``. ``pypi`` carries a ``version`` instead.
REF_KINDS: Tuple[str, ...] = ("local", "github")

#: The kinds that leave the project directory EMPTY (submodule deinit'd). Consumed by
#: :func:`detached_projects` -> :func:`aisland.activation.reconcile`.
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
    (:class:`aisland.workspace_file.FileError`).
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
    dropped (:func:`aisland.workspace_file.set_settings`).
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


def _run(cmd: List[str], cwd: Path, *, quiet: bool = False) -> bool:
    """Run ``cmd``; return True on success. A failure is reported, never raised.

    One project's unreachable remote or dirty checkout must not abort a whole
    ``aisland setup`` sweep — the same non-fatal contract as
    :func:`aisland.activation.reconcile`.
    """
    logger.debug("running %s (in %s)", " ".join(cmd), cwd)
    try:
        subprocess.check_call(cmd, cwd=str(cwd))
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if not quiet:
            failures.report(f"  - Error: `{' '.join(cmd)}` failed: {e}")
        return False


def _deinit_remedy(workspace_root: Path, path: Path, rel: str) -> str:
    """Name the remedy for a refused ``git submodule deinit`` — matching the ACTUAL blocker.

    Deinit refuses on two different conditions, and git reports both with the same wording
    ("contains local modifications") even though the fix lives in a DIFFERENT repository for
    each:

    * a dirty work tree INSIDE the submodule — fixed there, by committing or stashing;
    * a **stale gitlink** — the submodule is checked out at a commit other than the one the
      SUPERPROJECT records. This refuses even when the submodule's own tree is spotless, and
      it is the common case right after someone commits in the submodule without bumping the
      pointer in the workspace repo.

    Guessing the wrong one sends the operator hunting through a clean tree (the message used
    to say "commit or stash the changes in <sub>" unconditionally), so probe for each and
    report only what is true — and when neither probe can answer, stay generic rather than
    assert something unverified.
    """
    clauses: List[str] = []
    # A probe that cannot answer degrades to "unknown": never raise into the middle of a source switch.
    if gitcmd.read(path, "status", "--porcelain"):
        clauses.append(f"Commit or stash the changes in {rel}.")
    head = gitcmd.read(path, "rev-parse", "HEAD")
    recorded = gitcmd.read(workspace_root, "rev-parse", f"HEAD:{rel}")
    if head and recorded and head != recorded:
        clauses.append(
            f"The superproject records {recorded[:7]} for {rel} but the checkout is at "
            f"{head[:7]} — commit the pointer with `git add {rel} && git commit -- {rel}`."
        )
    if not clauses:
        return "Resolve the local modifications in the checkout or the superproject, then re-run."
    return " ".join(clauses) + " Then re-run."


# --- applying a source --------------------------------------------------------------------


def apply_source(workspace_root: Path, project: str, source: ProjectSource, uv_cmd: str) -> bool:
    """Bring the checkout and the shared ``.venv`` in line with ``source``. Returns success.

    ``local``: activate the submodule, initialize it if needed, fetch, switch to ``ref``, install
    editable. ``github`` / ``pypi``: resolve the distribution name while the directory is still
    readable, deinit (protectively — no ``-f``, so uncommitted work refuses and aborts BEFORE the
    venv is touched), then install from the remote at ``ref`` / from PyPI.
    """
    path = project_path(workspace_root, project)
    rel = str(path.relative_to(workspace_root))

    if source.kind == "local":
        click.echo(f"==> {project}: local checkout @ {source.ref}")
        set_submodule_active(workspace_root, project, True)
        if not (path / ".git").exists():
            if not _run(["git", "submodule", "update", "--init", rel], workspace_root):
                return False
        # Non-fatal: an offline machine can still switch to a branch it already has.
        gitcmd.fetch(path, "--quiet", "origin")
        # No ``-f``: a switch that would discard uncommitted work refuses, protecting it. The
        # bare ref relies on git's DWIM (--guess) to create a tracking branch from origin/<ref>.
        if not _run(["git", "switch", source.ref], path):
            click.echo(f"  - {project}: left on its current branch; resolve local changes and re-run.", err=True)
            return False
        # The SAME target `aisland setup` installs (dev extra + the selected framework/tracker
        # extras) — the bare directory used to drop every extra the machine had selected.
        return _run([uv_cmd, "pip", "install", "-e", editable_target(workspace_root, path, project)], workspace_root)

    # github / pypi — both detach. Read the distribution name while the directory still exists
    # (afterwards there is no pyproject.toml left to ask), then empty it.
    distribution = source.distribution or discover_distribution(path) or project
    if source.kind == "github":
        url = remote_url(workspace_root, project)
        if url is None:
            failures.report(
                f"Error: {project} has no submodule URL in .gitmodules, so a github source has "
                f"nothing to install from. Use: aisland workspace configure source set {project} local",
            )
            return False
        target = pip_vcs_url(url, source.ref)
        click.echo(f"==> {project}: github @ {source.ref} ({target})")
    else:
        target = ProjectSource(kind="pypi", version=source.version, distribution=distribution).requirement(project)
        click.echo(f"==> {project}: pypi ({target})")

    if (path / ".git").exists():
        if not _run(["git", "submodule", "deinit", rel], workspace_root):
            click.echo(
                f"  - {project}: deinit refused; the venv was left untouched. "
                f"{_deinit_remedy(workspace_root, path, rel)}",
                err=True,
            )
            return False
    set_submodule_active(workspace_root, project, False)
    # Uninstall first, unconditionally: an editable install would otherwise keep its .pth entry,
    # and re-pinning github:main -> github:dev/main reports the SAME version, which uv would
    # treat as already satisfied and skip.
    _run([uv_cmd, "pip", "uninstall", distribution], workspace_root, quiet=True)
    return _run([uv_cmd, "pip", "install", "-U", target], workspace_root)


def apply_all(workspace_root: Path, uv_cmd: str) -> None:
    """Apply every managed project's EFFECTIVE source. Unmanaged projects are not touched."""
    committed = read_committed_sources(workspace_root)
    managed = sorted(set(committed) | set(_overridden_projects(workspace_root)))
    if not managed:
        return
    click.echo(f"==> Applying per-project sources ({workspace_file.FILE_NAME} + local overrides)...")
    for project in managed:
        source = effective_source(workspace_root, project, committed)
        if source is not None:
            apply_source(workspace_root, project, source, uv_cmd)


def _overridden_projects(workspace_root: Path) -> List[str]:
    """Project names carrying a per-machine source in ``.git/config`` (``aisland.source.<project>.source``)."""
    return [project for project, settings in _machine_settings(workspace_root).items() if "source" in settings]


def detached_projects(workspace_root: Path) -> set[str]:
    """Projects whose EFFECTIVE source empties their directory (``github`` or ``pypi``).

    Consumed by :func:`aisland.activation.reconcile` so the activation machinery and the
    source machinery cannot fight over the same submodule.
    """
    committed = read_committed_sources(workspace_root)
    managed = set(committed) | set(_overridden_projects(workspace_root))
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


def _pypi_provenance(distribution: str, owner: Optional[str]) -> Tuple[Optional[bool], str]:
    """Whether PyPI's ``distribution`` really is THIS workspace's project.

    Returns ``(True, version)``; ``(False, reason)`` when the name is absent or belongs to an
    unrelated project; ``(None, reason)`` when PyPI could not be reached.

    Two failures this prevents, both of which would otherwise land AFTER the directory had
    already been deinit'd. The obvious one: an unpublished project fails deep inside
    ``uv pip install`` with "not found in the package registry". The subtle and worse one: PyPI
    names are GLOBAL, so a workspace project can share a name with a stranger's package, and a
    bare existence check would happily install that stranger's code into the shared venv. A
    project's published metadata names its repository, so a release whose URLs point at the GitHub
    owner of the project's own remote (``owner``) is the project's; a project whose remote is not on
    GitHub (``owner`` None) cannot be matched to a release and is refused.

    An unreachable index yields ``None`` (warn and continue) — never a false negative that would
    block an offline machine.
    """
    status, data = pypi_json(distribution)
    if status == "missing":
        return False, "is not published on PyPI"
    if status == "unreachable":
        return None, str(data)

    project: PypiProject = data
    info: PypiInfo = project.get("info") or {}
    if owner is None:
        return False, (
            f"is published on PyPI (version {info.get('version')}), but its remote is not on GitHub, so aisland "
            "cannot tell its release from a stranger's package of the same name"
        )
    if not points_at_owner(info, owner):
        return False, (
            f"is published on PyPI (version {info.get('version')}) but its metadata does not point at "
            f"github.com/{owner}/… — that name is owned by a different project"
        )
    return True, str(info.get("version") or "unknown")


#: Seconds `git ls-remote` may take to answer whether a ref exists.
REMOTE_TIMEOUT = 60


def _remote_ref_exists(workspace_root: Path, project: str, ref: str) -> bool:
    """Whether ``ref`` resolves on ``project``'s remote. Unreachable remote => ``True``.

    The counterpart of :func:`_pypi_provenance` for the ``github`` kind: a ref that does not
    exist would fail INSIDE ``uv pip install``, i.e. after the deinit had already emptied the
    directory. An unreachable remote (offline, auth prompt) is inconclusive, not a rejection —
    same rule as the PyPI probe: never block a machine on a check it cannot perform.
    """
    url = remote_url(workspace_root, project)
    if url is None:
        return True
    # One call answers both questions: a non-zero exit means the remote could not be reached,
    # while exit 0 with empty output means it answered and has no such ref.
    # Never a password prompt (a script or the MCP server would wait on it), never longer than a minute.
    try:
        probe = gitcmd.git(
            workspace_root, "ls-remote", "--heads", "--tags", "--", url, ref, network=True, timeout=REMOTE_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        probe = subprocess.CompletedProcess([], 1)
    if probe.returncode != 0:
        failures.warn(f"  - Warning: could not reach {project}'s remote to verify '{ref}'; continuing.")
        return True
    return bool(probe.stdout.strip())


def _refuse_unmanageable(root: Path, project: str, kind: str) -> None:
    """Refuse a project the workspace does not know, a kind there is none of, and a detached kind for a folder."""
    if project not in known_projects(root):
        raise AislandError(f"'{project}' is not a workspace project. Known: {', '.join(known_projects(root))}")
    if kind not in SOURCE_KINDS:
        raise AislandError(f"unknown source kind '{kind}'; expected one of {', '.join(SOURCE_KINDS)}")
    if kind in DETACHED_KINDS and project not in {name for name, _path, _active in submodule_active_map(root)}:
        # A folder project (no .gitmodules entry) has no submodule to deinit and none to check out again.
        raise AislandError(
            f"'{project}' is a folder of the workspace, not a submodule: a {kind} source empties its folder, "
            "and nothing could check it out again. Keep it local."
        )


def _checked_source(
    root: Path, project: str, kind: str, ref: Optional[str], version: Optional[str], distribution: str
) -> ProjectSource:
    """The source to set, checked before anything changes: a ``pypi`` release must be this workspace's project, a
    ``github`` ref must exist on the remote — a detached source empties the folder, so a bad one would only show
    after the checkout was gone."""
    if kind == "pypi":
        repository = github_repository(remote_url(root, project) or "")
        owner = repository.owner if repository else None
        published, detail = _pypi_provenance(distribution, owner)
        if published is False:
            raise AislandError(
                f"'{distribution}' {detail}. Publish it first, or use: "
                f"aisland workspace configure source set {project} github"
            )
        if published is None:
            failures.warn(f"  - Warning: {detail}; continuing without verifying '{distribution}'.")
        else:
            click.echo(f"  - Verified {distribution} {detail} on PyPI (published from github.com/{owner}).")
        return ProjectSource(kind="pypi", version=version, distribution=distribution)
    listed = workspace_file.load(root).projects.get(project)
    branch = ref or (listed.branch if listed else None) or DEFAULT_REF  # no --branch: the project's own
    source = ProjectSource(kind=cast(SourceKind, kind), ref=branch, distribution=distribution)
    if kind == "github" and not _remote_ref_exists(root, project, source.ref):
        raise AislandError(
            f"'{source.ref}' does not exist on {project}'s remote, so a github source "
            f"could never install. Push the branch first, or pick an existing ref."
        )
    return source


def _record_source(
    root: Path, project: str, source: ProjectSource, committed: Dict[str, ProjectSource], machine_only: bool
) -> Dict[str, ProjectSource]:
    """Write ``source`` to this machine's layer, or commit it (dropping this machine's deviation); the committed
    sources that follow."""
    if machine_only:
        set_local_override(root, project, source)
        click.echo(f"==> {project}: per-machine override set to {source.spec()} (nothing committed).")
        return committed
    set_local_override(root, project, None)
    committed = {**committed, project: source}
    path = write_committed_sources(root, committed)
    click.echo(f"==> {project}: committed source set to {source.spec()} in {path.name}")
    return committed


def set_project_source(
    project: str,
    kind: str,
    ref: Optional[str] = None,
    version: Optional[str] = None,
    machine_only: bool = False,
    workspace_root: Optional[Path] = None,
) -> int:
    """Record and apply a project's source. Returns a process exit code (0 = success).

    Order matters: validate first, then write configuration, then apply to the checkout + venv,
    then rewrite this machine's workspace file, whose folder list follows the checkouts.
    No CI file is touched: CI installs what is RELEASED, never what this machine checked out
    (see :func:`released_on_pypi`).
    A failed apply ROLLS THE CONFIGURATION BACK — a registry claiming a state the working tree
    does not have is worse than no change at all (and `aisland setup` would keep retrying it). So
    does an apply that raises or is interrupted (Ctrl-C); the exception then goes on as it was.
    """
    root = workspace_root or get_workspace_root()
    _refuse_unmanageable(root, project, kind)
    venv_bin = root / ".venv" / "bin"
    if not venv_bin.exists():
        raise no_venv(root)

    original = read_committed_sources(root)
    previous_override = local_override(root, project)
    previous = original.get(project)
    distribution = (
        discover_distribution(project_path(root, project)) or (previous.distribution if previous else None) or project
    )
    source = _checked_source(root, project, kind, ref, version, distribution)

    file = root / workspace_file.FILE_NAME
    before = file.read_text() if file.is_file() else None

    def roll_back() -> None:
        set_local_override(root, project, previous_override)
        if not machine_only:
            # The file as it was, byte for byte: a branch the set wrote goes too, and a workspace that had no
            # file keeps none.
            if before is None:
                file.unlink(missing_ok=True)
            else:
                file.write_text(before)

    try:
        committed = _record_source(root, project, source, original, machine_only)
        uv_cmd = ensure_uv_in_venv(venv_bin)
        applied = apply_source(root, project, effective_source(root, project, committed) or source, uv_cmd)
    except BaseException:
        roll_back()
        raise
    if not applied:
        roll_back()
        raise AislandError(f"{project}: source change reverted; configuration is unchanged")

    # The workspace file is per machine and gitignored, so a --machine-only change rewrites it too.
    write_code_workspace(root)
    return 0


def unset_project_source(
    project: str,
    machine_only: bool = False,
    workspace_root: Optional[Path] = None,
) -> int:
    """Return a project to *unmanaged*. Returns a process exit code (0 = success).

    Deliberately does NOT touch the checkout or the venv: "unmanaged" means aisland holds no
    opinion, not that the current state is wrong. Dropping a ``pypi`` entry therefore leaves the
    directory empty until the next ``aisland setup`` (whose ``.gitmodules``-driven
    :func:`aisland.activation.reconcile` re-inits it) — set an explicit ``git`` source
    instead if you want the checkout back now.
    """

    root = workspace_root or get_workspace_root()
    committed = read_committed_sources(root)

    if machine_only:
        if local_override(root, project) is None:
            click.echo(f"'{project}' has no per-machine override; nothing to clear.")
            return 0
        set_local_override(root, project, None)
        click.echo(f"==> {project}: per-machine override cleared (committed source still applies).")
        return 0

    if project not in committed and local_override(root, project) is None:
        click.echo(f"'{project}' is already unmanaged.")
        return 0

    set_local_override(root, project, None)
    committed.pop(project, None)
    path = write_committed_sources(root, committed)
    click.echo(f"==> {project}: removed from {path.name} — now unmanaged.")

    write_code_workspace(root)
    return 0


def apply_configured(project: Optional[str] = None, workspace_root: Optional[Path] = None) -> int:
    """Re-apply configured source(s) without changing configuration. Returns an exit code."""

    root = workspace_root or get_workspace_root()
    venv_bin = root / ".venv" / "bin"
    if not venv_bin.exists():
        raise no_venv(root)
    uv_cmd = ensure_uv_in_venv(venv_bin)

    if project is None:
        apply_all(root, uv_cmd)
        write_code_workspace(root)
        return 0

    source = effective_source(root, project)
    if source is None:
        click.echo(f"'{project}' has no configured source — it is unmanaged. Nothing to apply.")
        return 0
    applied = apply_source(root, project, source, uv_cmd)
    write_code_workspace(root)
    return 0 if applied else 1


def describe(workspace_root: Optional[Path] = None) -> List[Dict[str, str]]:
    """One row per managed project for ``aisland workspace configure source``."""
    root = workspace_root or get_workspace_root()
    committed = read_committed_sources(root)
    rows: List[Dict[str, str]] = []
    for project in sorted(set(committed) | set(_overridden_projects(root))):
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
