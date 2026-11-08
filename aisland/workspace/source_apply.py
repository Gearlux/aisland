"""Bringing a project's checkout and the workspace's ``.venv`` in line with its source (:mod:`aisland.workspace.sources`).

``local``: the submodule activated, initialized, fetched and switched to its ref, then installed editable.
``github`` / ``pypi``: the distribution name read while the folder is still there, the submodule deinit'd without
``-f`` (uncommitted work refuses, and the venv is left untouched), then installed from the remote at its ref or from
PyPI. A failure is reported and the sweep goes on: one project's unreachable remote never stops ``aisland setup``.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from aisland import console, failures, gitcmd
from aisland.workspace import file as workspace_file
from aisland.workspace import sources
from aisland.workspace.projects import set_submodule_active
from aisland.workspace.selection import editable_target

logger = logging.getLogger(__name__)


def _run(cmd: List[str], cwd: Path, *, quiet: bool = False, env: Optional[Dict[str, str]] = None) -> bool:
    """Run ``cmd`` (in ``env``, else this environment); return True on success. A failure is reported, never raised.

    One project's unreachable remote or dirty checkout must not abort a whole
    ``aisland setup`` sweep — the same non-fatal contract as
    :func:`aisland.workspace.activation.reconcile`.
    """
    logger.debug("running %s (in %s)", " ".join(cmd), cwd)
    try:
        subprocess.check_call(cmd, cwd=str(cwd), env=env)
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


def apply_source(workspace_root: Path, project: str, source: sources.ProjectSource, uv_cmd: str) -> bool:
    """Bring the checkout and the shared ``.venv`` in line with ``source``. Returns success.

    ``local``: activate the submodule, initialize it if needed, fetch, switch to ``ref``, install
    editable. ``github`` / ``pypi``: resolve the distribution name while the directory is still
    readable, deinit (protectively — no ``-f``, so uncommitted work refuses and aborts BEFORE the
    venv is touched), then install from the remote at ``ref`` / from PyPI.
    """
    path = sources.project_path(workspace_root, project)
    rel = str(path.relative_to(workspace_root))

    if source.kind == "local":
        console.echo(f"==> {project}: local checkout @ {source.ref}")
        set_submodule_active(workspace_root, sources.submodule_name(workspace_root, project), True)
        if not (path / ".git").exists():
            # It clones: no credential prompt, and a remote that stops sending is given up (gitcmd's guard).
            clone = ["git", *gitcmd.network_options(), "submodule", "update", "--init", "--", rel]
            if not _run(clone, workspace_root, env=gitcmd.network_env()):
                return False
        # Non-fatal: an offline machine can still switch to a branch it already has.
        gitcmd.fetch(path, "--quiet", "origin")
        # No ``-f``: a switch that would discard uncommitted work refuses, protecting it. The
        # bare ref relies on git's DWIM (--guess) to create a tracking branch from origin/<ref>.
        # The ref comes from workspace.toml, another person's file: a branch name, never an option (`--orphan=x`).
        if source.ref.startswith("-"):
            failures.report(f"  - Error: {project}: {source.ref!r} is no branch name (a branch never starts with '-')")
            return False
        if not _run(["git", "switch", source.ref], path):
            console.echo(f"  - {project}: left on its current branch; resolve local changes and re-run.", err=True)
            return False
        # The SAME target `aisland setup` installs (dev extra + the selected framework/tracker
        # extras) — the bare directory used to drop every extra the machine had selected.
        return _run([uv_cmd, "pip", "install", "-e", editable_target(workspace_root, path, project)], workspace_root)

    # github / pypi — both detach. Read the distribution name while the directory still exists
    # (afterwards there is no pyproject.toml left to ask), then empty it.
    distribution = source.distribution or sources.discover_distribution(path) or project
    if source.kind == "github":
        url = sources.remote_url(workspace_root, project)
        if url is None:
            failures.report(
                f"Error: {project} has no submodule URL in .gitmodules, so a github source has "
                f"nothing to install from. Use: aisland workspace configure source set {project} local",
            )
            return False
        target = sources.pip_vcs_url(url, source.ref)
        console.echo(f"==> {project}: github @ {source.ref} ({target})")
    else:
        target = sources.ProjectSource(kind="pypi", version=source.version, distribution=distribution).requirement(
            project
        )
        console.echo(f"==> {project}: pypi ({target})")

    if (path / ".git").exists():
        if not _run(["git", "submodule", "deinit", rel], workspace_root):
            console.echo(
                f"  - {project}: deinit refused; the venv was left untouched. "
                f"{_deinit_remedy(workspace_root, path, rel)}",
                err=True,
            )
            return False
    set_submodule_active(workspace_root, sources.submodule_name(workspace_root, project), False)
    # Uninstall first, unconditionally: an editable install would otherwise keep its .pth entry,
    # and re-pinning github:main -> github:dev/main reports the SAME version, which uv would
    # treat as already satisfied and skip.
    _run([uv_cmd, "pip", "uninstall", distribution], workspace_root, quiet=True)
    return _run([uv_cmd, "pip", "install", "-U", target], workspace_root)


def apply_all(workspace_root: Path, uv_cmd: str) -> None:
    """Apply every managed project's EFFECTIVE source. Unmanaged projects are not touched."""
    committed = sources.read_committed_sources(workspace_root)
    managed = sorted(set(committed) | set(sources.overridden_projects(workspace_root)))
    if not managed:
        return
    console.echo(f"==> Applying per-project sources ({workspace_file.FILE_NAME} + local overrides)...")
    for project in managed:
        source = sources.effective_source(workspace_root, project, committed)
        if source is not None:
            apply_source(workspace_root, project, source, uv_cmd)
