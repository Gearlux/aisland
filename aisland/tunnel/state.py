"""What every part of `aisland tunnel` shares: the machine's state folder and the relay's rule for a tunnel's name.

The folder is one per machine, not per workspace (:func:`state_dir`): a tunnel belongs to the machine, and deriving
its folder from the working directory meant ``start`` in one directory and ``status`` in another disagreed about
whether a tunnel existed (measured on a remote box, 2026-10-20).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Names another folder for the tunnel's state (default ``~/.aisland/tunnel``): what a machine sets
#: when its ``$HOME`` is not on storage that survives a restart.
TUNNEL_HOME_ENV = "AISLAND_TUNNEL_HOME"

#: The relay's rules for a tunnel name: lowercase letters, digits and dashes, 4 to 20 characters.
NAME_MIN = 4
NAME_MAX = 20
_NAME_DISALLOWED = re.compile(r"[^a-z0-9-]+")


def state_dir() -> Path:
    """The folder holding the CLI, the log, the pidfile, the account and the remembered workspaces.

    One folder per machine — ``~/.aisland/tunnel`` — so every command finds the same tunnel
    wherever it is run from. ``AISLAND_TUNNEL_HOME`` names another folder, for a machine whose
    ``$HOME`` does not survive a restart: point it at a persistent mount and the sign-in lasts.
    """
    named = os.environ.get(TUNNEL_HOME_ENV, "").strip()
    if named:
        return Path(named).expanduser()
    return Path.home() / ".aisland" / "tunnel"


def sanitize_name(raw: str) -> str:
    """``raw`` as a name the relay accepts: lowercased, stripped of anything but ``a-z0-9-``, 4-20 long."""
    name = _NAME_DISALLOWED.sub("-", raw.strip().lower())[:NAME_MAX].strip("-")
    while len(name) < NAME_MIN:
        name = f"{name}0" if name else "tunnel"
    return name
