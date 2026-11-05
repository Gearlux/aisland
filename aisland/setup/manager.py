"""``aisland setup``: provision the system tools, activate and install every project editable into the workspace's
one ``.venv``, and write the files the workspace generates per machine (the shell start-up file ``project.bashrc``,
the editor and MCP client files, the root ``uv.toml``, the example environment files, the git aliases).

The pieces live in their own modules — :mod:`aisland.workspace.projects` (the workspace and its projects),
:mod:`aisland.setup.installers` (system tools), :mod:`aisland.setup.editor_files`, :mod:`aisland.setup.mcp_config`,
:mod:`aisland.setup.uv_config`, :mod:`aisland.setup.git_settings`, :mod:`aisland.setup.playwright_browser` — and this module runs them in
order.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Iterable, List, Tuple

import click

from aisland import console, failures, locations, paths
from aisland.harness import install_harness
from aisland.paths import SERVICES_DIR
from aisland.setup import env_example, provision
from aisland.setup.editor_files import sync_mypy_path, write_code_workspace, write_pyrightconfig
from aisland.setup.git_settings import merge_on_pull
from aisland.setup.installers import ensure_uv_in_venv
from aisland.setup.mcp_config import no_venv, write_gemini_settings, write_mcp_json
from aisland.setup.playwright_browser import install_browsers
from aisland.setup.uv_config import write_root_uv_toml
from aisland.workspace import activation
from aisland.workspace import check as workspace_check
from aisland.workspace import clones
from aisland.workspace import file as workspace_file
from aisland.workspace import selection
from aisland.workspace.mirrors import apply_all as apply_mirrors
from aisland.workspace.projects import (
    ProjectMetadata,
    get_install_order,
    get_workspace_root,
    install_groups,
    internal_dependencies,
)
from aisland.workspace.selection import axes, resolve_for_setup
from aisland.workspace.sources import apply_all

logger = logging.getLogger(__name__)


def _run_installs(workspace_root: Path, uv_cmd: str, projects: List[ProjectMetadata]) -> Tuple[List[str], List[str]]:
    """Editable-install every project, one unit per :func:`install_groups` group.

    Returns ``(failed, skipped)``. A project whose dependency failed (or was itself skipped) is
    SKIPPED and named, never attempted: it would fail against the stale index copy of what just
    failed, and a dozen such repeats buried the one real error. Unrelated projects still install.
    """
    adj = internal_dependencies(projects)
    failed: List[str] = []
    skipped: List[str] = []
    for group in install_groups(projects):
        names = [p["name"] for p in group]
        blocked = sorted({dep for n in names for dep in adj[n] if dep in failed or dep in skipped} - set(names))
        if blocked:
            for n in names:
                console.echo(f"==> Skipping {n}: its dependency {', '.join(blocked)} did not install.")
            skipped.extend(names)
            continue
        # Each target carries every extra the project DECLARES, narrowed to what the machine SELECTED
        # (shared with `aisland workspace configure source … local`, so the two installs cannot drift).
        targets = [selection.editable_target(workspace_root, p["path"], p["name"]) for p in group]
        suffix = " — a dependency cycle, installed together" if len(group) > 1 else ""
        console.echo(f"==> Installing {', '.join(names)} ({' '.join(targets)}){suffix}...")
        cmd = [uv_cmd, "pip", "install"] + [arg for t in targets for arg in ("-e", t)]
        try:
            # Run from the workspace root so uv reads the generated root uv.toml (the merged
            # private indexes); the editable target is absolute, so cwd doesn't change WHAT installs.
            logger.debug("running %s (in %s)", " ".join(cmd), workspace_root)
            subprocess.check_call(cmd, cwd=workspace_root)
        except subprocess.CalledProcessError as e:
            failures.report(f"  - Error: Failed to install {', '.join(names)}: {e}")
            failed.extend(names)
    return failed, skipped


def _copy_packaged_bashrc(target: Path) -> None:
    """From a wheel: put the packaged ``project.bashrc`` at ``target`` (``<workspace>/.aisland/``), replacing an older
    copy. The copy is aisland's (its header says so), and its place there makes the workspace its parent folder. A
    clone of aisland no workspace lists, installed editable, works like a wheel but carries no packaged copy: its own
    file, beside the package, is the one copied."""
    packaged = locations.PACKAGE_DIR / "project.bashrc"
    if not packaged.is_file():
        packaged = locations.CHECKOUT_DIR / "project.bashrc"
    text = packaged.read_text()
    if target.exists() and target.read_text() == text:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def link_project_bashrc(workspace_root: Path) -> None:
    """Symlink ``<workspace>/project.bashrc`` to the canonical aisland-managed copy.

    The source of truth lives at ``aisland/project.bashrc`` (tracked); from a wheel, the packaged copy is first
    written to ``<workspace>/.aisland/project.bashrc`` and the link points there. The
    workspace-root entry is a symlink so `source ./project.bashrc` from the
    workspace still works for muscle-memory. Replaces a stale symlink in place,
    but refuses to clobber a real file — that would silently destroy the user's
    customizations if they ever predated this migration.
    """
    target = paths.state_dir() / "project.bashrc"
    link = workspace_root / "project.bashrc"
    if not locations.FROM_CHECKOUT:
        _copy_packaged_bashrc(target)

    if not target.exists():
        failures.warn(f"  - Warning: {target} not found; skipping project.bashrc symlink.")
        return

    if link.is_symlink():
        if link.resolve() == target.resolve():
            logger.debug("%s already links to %s", link, target)
            return
        link.unlink()
    elif link.exists():
        failures.warn(
            f"  - Warning: {link} exists as a regular file. Move or delete it manually so "
            f"`aisland setup` can symlink it to {target}."
        )
        return

    rel = os.path.relpath(target, workspace_root)
    os.symlink(rel, link)
    console.echo(f"==> Linked {link} -> {rel}")


def write_generated_env_example(workspace_root: Path) -> None:
    """Setup's step: the workspace's ``env.complete`` and ``env.example`` (``aisland workspace env-example``); a
    project's malformed file is said and setup goes on."""

    try:
        written = env_example.write(workspace_root)
    except click.ClickException as error:
        console.echo(f"==> env.complete and env.example not written: {error.message}")
        return
    for kind, result in written.items():
        if result.changed:
            console.echo(f"==> Wrote {env_example.FILE_NAMES[kind]} ({env_example.counts(result)})")


def _say(lines: Iterable[str]) -> None:
    """A step's report, one ``  - <line>`` each."""
    for line in lines:
        console.echo(f"  - {line}")


def _choose_selections(workspace_root: Path, non_interactive: bool, install_all: bool) -> None:
    """Each selection (aisland's ``extras`` and each one a project declares) resolved for setup: asked once when
    nothing is chosen, a recorded choice used silently. ``install_all`` (``--all``) is the extras selection's own: a
    framework or tracker is a machine's choice of one backend each."""
    for axis in axes(workspace_root):
        selected, _just_chosen = resolve_for_setup(
            workspace_root, non_interactive, axis, select_all=install_all and axis.from_defined_extras
        )
        if selected:
            console.echo(f"==> {axis.name}: {', '.join(selected)}")


def _report_installs(failed: List[str], skipped: List[str]) -> None:
    """The editable installs' summary, said only when one failed or was skipped."""
    if failed or skipped:
        console.echo(f"==> {len(failed)} project(s) failed to install: {', '.join(failed) or '-'}")
        if skipped:
            console.echo(f"==> {len(skipped)} skipped because a dependency failed: {', '.join(skipped)}")


def setup_workspace(non_interactive: bool = False, install_all: bool = False) -> None:
    """Perform a full workspace setup: system provisioning followed by editable pip installs.

    Each selection (:mod:`aisland.workspace.selection`: aisland's ``extras`` and those the projects declare) installs what this
    machine has chosen, asked once and committed in ``workspace.toml``; ``install_all`` (``--all``) takes every extra
    every project defines. A choice is changed with ``aisland workspace configure <selection> set``.
    """
    workspace_root = get_workspace_root()
    venv_bin = workspace_root / ".venv" / "bin"
    if not venv_bin.exists():
        raise no_venv(workspace_root)

    # 0. uv in the venv — everything below needs it, and so does every Jenkinsfile.local job of this workspace.
    uv_cmd = ensure_uv_in_venv(venv_bin)
    # 0b. The root's `project.bashrc`, a link to aisland's copy, so `source ./project.bashrc` works from the root.
    link_project_bashrc(workspace_root)
    # 0c. The agent harness in `.agents/` (aisland.harness) — linked from a checkout, copied from a wheel. Before
    #     provisioning, which can stop a setup half-way.
    _say(install_harness(workspace_root))
    # 0c'. A clone of the workspace's folder that .gitmodules does not list: added as a submodule, never asked about
    #      (aisland.workspace.clones), before workspace.toml, discovery and the installs read the projects.
    _say(clones.add_unlisted(workspace_root))
    # 0d. workspace.toml: its projects brought in line with .gitmodules (or the clones) — a new one added, a changed
    #     url or folder followed, a gone one dropped — then each key no part of aisland reads named (a typo).
    _say(workspace_file.refresh(workspace_root))
    for line in workspace_check.unknown_keys(workspace_root):
        console.echo(line, err=True)

    # 1. The system tools the projects declare (aisland's own are installed by the feature that first needs one).
    provision.install(workspace_root, aislands_own=False)
    # 1b. Activation (aisland.workspace.activation): ask about each optional project, then check each one out or empty it as
    #     this machine has it. Before discovery, so an inactive checkout drops out.
    activation.resolve(workspace_root, non_interactive)
    activation.reconcile(workspace_root)
    # 1c. Per-project sources: each managed project on its branch, or replaced by its published wheel (which empties
    #     its folder). Before discovery, so a pypi-sourced project drops out of the editable installs.
    apply_all(workspace_root, uv_cmd)
    # 1d. Per-project mirrors: each checkout's `mirror` remote follows its setting. No network.
    apply_mirrors(workspace_root)

    # 2. Dependency discovery (now reflects the active checkouts), and the editor and type-checker files from it.
    console.echo(f"==> Resolving workspace dependency graph in {workspace_root}...")
    install_order = get_install_order(workspace_root)
    write_pyrightconfig(workspace_root, install_order)
    sync_mypy_path(workspace_root, install_order)  # append-only: the root mypy.ini is committed
    # 2b. The selections — after discovery (their menus come from the projects), before the installs that read them. A
    #     declaration aisland cannot use stops setup here, naming its file and line, before anything is installed.
    _choose_selections(workspace_root, non_interactive, install_all)
    # 2c. The root uv.toml — each activated project's private index merged in — before `uv pip install` reads it.
    write_root_uv_toml(workspace_root, install_order)

    # 3. Each project's git settings, its editable install (cycles together, dependents of a failure skipped), then
    #    the browser a `playwright` dev dependency needs, for each project that installed.
    merge_on_pull(install_order)
    failed, skipped = _run_installs(workspace_root, uv_cmd, install_order)
    _report_installs(failed, skipped)
    install_browsers([p for p in install_order if p["name"] not in failed + skipped], venv_bin)

    # 4. The MCP client files — every workspace MCP server for the Gemini CLI and Claude Code, with absolute venv
    #    paths. After the installs, so the console scripts exist.
    write_gemini_settings(workspace_root, venv_bin)
    write_mcp_json(workspace_root, venv_bin)
    # 5. The VS Code workspace file (its loggair log folder needs loggair installed to be asked), then the workspace's
    #    example environment made of each project's env.example (never .env itself).
    write_code_workspace(workspace_root)
    write_generated_env_example(workspace_root)
    # 6. The git aliases, however aisland is installed: from a wheel they name the scripts by their path in the venv,
    #    so a recreated venv or a new aisland is pointed at by the next setup; a newly activated submodule gets them.
    install_git_aliases(workspace_root)

    console.echo("==> Workspace setup complete!")


#: The script that installs the workspace git aliases (`git pull-all`, `git push-all`, ...), shipped in the package.
SETUP_GIT_SCRIPT = SERVICES_DIR / "git" / "setup_git.sh"


def install_git_aliases(workspace_root: Path, quiet: bool = False) -> bool:
    """Install the workspace git aliases into the local config of ``workspace_root`` and each active submodule.

    Runs the packaged ``setup_git.sh`` with the root as its argument, so the scripts need not sit in the workspace:
    from a wheel the aliases name them by their path in ``site-packages`` (`aisland setup` runs it every time).
    Returns whether the script succeeded. Its output is passed through, or with ``quiet`` shown only on a failure.
    """
    result = subprocess.run(
        ["bash", str(SETUP_GIT_SCRIPT), str(workspace_root)],
        cwd=workspace_root,
        capture_output=quiet,
        text=True,
    )
    if result.returncode != 0:
        if quiet:
            console.echo((result.stdout or "") + (result.stderr or ""), nl=False)
        failures.report(f"  - Error: installing the git aliases failed (bash {SETUP_GIT_SCRIPT} {workspace_root}).")
    return result.returncode == 0
