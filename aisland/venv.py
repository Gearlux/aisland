"""The workspace's one virtual environment, ``<root>/.venv``: the refusal when it is missing, and its ``uv``.

Every layer that installs into it asks here — setup, the source and selection commands, the wheel builds, the
quality gates — so none of them reaches into another's module for it.
"""

import logging
import os
import subprocess
from pathlib import Path

from aisland import console, locations
from aisland.errors import AislandError

logger = logging.getLogger(__name__)

#: The workspace venv's folder, in the workspace's root.
VENV = ".venv"


def venv_dir(workspace_root: Path) -> Path:
    """The workspace's venv folder, ``<root>/.venv``, whether or not it exists."""
    return workspace_root / VENV


def venv_bin(workspace_root: Path) -> Path:
    """The folder of the workspace venv's programs (``python``, ``pip``, ``uv``, ``aisland``), whether or not it
    exists."""
    return venv_dir(workspace_root) / "bin"


def require_venv_bin(workspace_root: Path) -> Path:
    """:func:`venv_bin`, for a command that installs into the venv or runs its tools: refused (:func:`no_venv`) when
    the workspace has no venv."""
    found = venv_bin(workspace_root)
    if not found.exists():
        raise no_venv(workspace_root)
    return found


def no_venv(workspace_root: Path) -> AislandError:
    """The refusal of a command that needs the workspace's ``.venv`` when it has none, with how to make one."""
    if locations.FROM_CHECKOUT:
        return AislandError(f"Virtual environment not found at {venv_dir(workspace_root)}. Run aisland/setup.sh first.")
    return AislandError(
        f"Virtual environment not found at {venv_dir(workspace_root)}. Create it and install aisland "
        "into it: uv venv --seed .venv && .venv/bin/pip install aisland"
    )


def ensure_uv_in_venv(venv_bin: Path) -> str:
    """Guarantee ``uv`` is present in the workspace venv and return its path.

    Every Jenkins .local job, every ``aisland setup`` run, and every call into the
    workspace's Python tools assumes ``${VENV_BIN}/uv`` exists. If the venv was
    created without uv (or uv got uninstalled by some downstream ``uv pip install``
    shenanigan), this function installs it via the venv's own ``pip``.

    Idempotent: if uv is already executable, returns its path immediately.
    """
    uv_path = venv_bin / "uv"
    if uv_path.exists() and os.access(uv_path, os.X_OK):
        return str(uv_path)

    pip_path = venv_bin / "pip"
    if not pip_path.exists():
        # A venv made by `uv venv` without `--seed` has no pip (setup.sh seeds one with ensurepip).
        remedy = "Re-run aisland/setup.sh." if locations.FROM_CHECKOUT else "Recreate it with `uv venv --seed .venv`."
        raise AislandError(f"{pip_path} not found. Cannot bootstrap uv; the venv is broken. {remedy}")

    console.echo(f"==> Bootstrapping uv into {venv_bin}...")
    logger.debug("running %s install --quiet --upgrade uv", pip_path)
    subprocess.check_call([str(pip_path), "install", "--quiet", "--upgrade", "uv"])
    return str(uv_path)
