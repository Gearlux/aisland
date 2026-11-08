"""``aisland workspace configure source set|unset``: a project's source checked, recorded in the layer asked for,
applied (:mod:`aisland.workspace.source_apply`), and this machine's editor workspace file rewritten, its folders
following the checkouts. ``aisland setup`` applies every source without changing one (``source_apply.apply_all``).

The checks come first, because a detached source empties the folder: a ``pypi`` release must be this workspace's
project, a ``github`` ref must exist on the remote. A failed apply rolls the configuration back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple, cast

from aisland import console, failures, gitcmd
from aisland.errors import AislandError
from aisland.pypi import PypiInfo, PypiProject, points_at_owner, pypi_json
from aisland.setup.editor_files import write_code_workspace
from aisland.venv import ensure_uv_in_venv, require_venv_bin
from aisland.workspace import file as workspace_file
from aisland.workspace import source_apply, sources
from aisland.workspace.github import github_repository
from aisland.workspace.model import Workspace
from aisland.workspace.projects import get_workspace_root, submodule_active_map


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
    url = sources.remote_url(workspace_root, project)
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
    known = Workspace(root).known_names
    if project not in known:
        raise AislandError(f"'{project}' is not a workspace project. Known: {', '.join(known)}")
    if kind not in sources.SOURCE_KINDS:
        raise AislandError(f"unknown source kind '{kind}'; expected one of {', '.join(sources.SOURCE_KINDS)}")
    if kind in sources.DETACHED_KINDS and project not in {name for name, _path, _active in submodule_active_map(root)}:
        # A folder project (no .gitmodules entry) has no submodule to deinit and none to check out again.
        raise AislandError(
            f"'{project}' is a folder of the workspace, not a submodule: a {kind} source empties its folder, "
            "and nothing could check it out again. Keep it local."
        )


def _checked_source(
    root: Path, project: str, kind: str, ref: Optional[str], version: Optional[str], distribution: str
) -> sources.ProjectSource:
    """The source to set, checked before anything changes: a ``pypi`` release must be this workspace's project, a
    ``github`` ref must exist on the remote — a detached source empties the folder, so a bad one would only show
    after the checkout was gone."""
    if kind == "pypi":
        repository = github_repository(sources.remote_url(root, project) or "")
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
            console.echo(f"  - Verified {distribution} {detail} on PyPI (published from github.com/{owner}).")
        return sources.ProjectSource(kind="pypi", version=version, distribution=distribution)
    listed = workspace_file.load(root).projects.get(project)
    branch = ref or (listed.branch if listed else None) or sources.DEFAULT_REF  # no --branch: the project's own
    source = sources.ProjectSource(kind=cast(sources.SourceKind, kind), ref=branch, distribution=distribution)
    if kind == "github" and not _remote_ref_exists(root, project, source.ref):
        raise AislandError(
            f"'{source.ref}' does not exist on {project}'s remote, so a github source "
            f"could never install. Push the branch first, or pick an existing ref."
        )
    return source


def _record_source(
    root: Path,
    project: str,
    source: sources.ProjectSource,
    committed: Dict[str, sources.ProjectSource],
    machine_only: bool,
) -> Dict[str, sources.ProjectSource]:
    """Write ``source`` to this machine's layer, or commit it (dropping this machine's deviation); the committed
    sources that follow."""
    if machine_only:
        sources.set_local_override(root, project, source)
        console.echo(f"==> {project}: per-machine override set to {source.spec()} (nothing committed).")
        return committed
    sources.set_local_override(root, project, None)
    committed = {**committed, project: source}
    path = sources.write_committed_sources(root, committed)
    console.echo(f"==> {project}: committed source set to {source.spec()} in {path.name}")
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
    (see :func:`aisland.pypi.released_on_pypi`).
    A failed apply ROLLS THE CONFIGURATION BACK — a registry claiming a state the working tree
    does not have is worse than no change at all (and `aisland setup` would keep retrying it). So
    does an apply that raises or is interrupted (Ctrl-C); the exception then goes on as it was.
    """
    root = workspace_root or get_workspace_root()
    _refuse_unmanageable(root, project, kind)
    venv_bin = require_venv_bin(root)

    original = sources.read_committed_sources(root)
    previous_override = sources.local_override(root, project)
    previous = original.get(project)
    distribution = (
        sources.discover_distribution(sources.project_path(root, project))
        or (previous.distribution if previous else None)
        or project
    )
    source = _checked_source(root, project, kind, ref, version, distribution)

    file = root / workspace_file.FILE_NAME
    before = file.read_text() if file.is_file() else None

    def roll_back() -> None:
        sources.set_local_override(root, project, previous_override)
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
        applied = source_apply.apply_source(
            root, project, sources.effective_source(root, project, committed) or source, uv_cmd
        )
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
    :func:`aisland.workspace.activation.reconcile` re-inits it) — set an explicit ``git`` source
    instead if you want the checkout back now.
    """

    root = workspace_root or get_workspace_root()
    committed = sources.read_committed_sources(root)

    if machine_only:
        if sources.local_override(root, project) is None:
            console.echo(f"'{project}' has no per-machine override; nothing to clear.")
            return 0
        sources.set_local_override(root, project, None)
        console.echo(f"==> {project}: per-machine override cleared (committed source still applies).")
        return 0

    if project not in committed and sources.local_override(root, project) is None:
        console.echo(f"'{project}' is already unmanaged.")
        return 0

    sources.set_local_override(root, project, None)
    committed.pop(project, None)
    path = sources.write_committed_sources(root, committed)
    console.echo(f"==> {project}: removed from {path.name} — now unmanaged.")

    write_code_workspace(root)
    return 0
