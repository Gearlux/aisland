"""The shell start-up file ``aisland setup`` links into the workspace root: ``<workspace>/project.bashrc``, a symlink
to aisland's own copy (:func:`link_project_bashrc`; ``aisland workspace bashrc`` runs it alone). The copy is
``aisland/project.bashrc`` in a checkout, and from a wheel the packaged file written to ``<workspace>/.aisland/``.
"""

import logging
import os
from pathlib import Path

from aisland import atomic, console, failures, locations, paths

logger = logging.getLogger(__name__)


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
    atomic.write_text(target, text)


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
