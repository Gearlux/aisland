"""Writing a file whole or not at all.

A state file written in place (``workspace.toml``, ``.env``, the supervisor's document, a shell start-up file) is
truncated first: a crash, a full disk or a Ctrl-C half way leaves it cut short, and the next run reads what is left.
:func:`write_text` writes a temporary file beside it and moves that into place, so a reader sees the old file or the
new one, never part of either. Standard library only.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

#: The mode of a file that holds secrets (`.env`): readable and writable by its owner alone.
PRIVATE = 0o600


def write_text(path: Path, text: str, new_mode: Optional[int] = None) -> None:
    """Write ``text`` to ``path`` atomically.

    A file that exists keeps its permission bits, and a link keeps being a link: the file it points at is the one
    replaced (a dotfiles repository links ``~/.bashrc``). A new file gets ``new_mode``, else what the umask gives
    an ordinary new file.
    """
    target = Path(os.path.realpath(path))
    try:
        mode: Optional[int] = target.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = new_mode
    descriptor, scratch = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as out:
            out.write(text)
        os.chmod(scratch, mode if mode is not None else 0o666 & ~_umask())
        os.replace(scratch, target)
    except BaseException:
        Path(scratch).unlink(missing_ok=True)
        raise


def _umask() -> int:
    """This process's umask (reading it means setting it, so it is set straight back)."""
    current = os.umask(0)
    os.umask(current)
    return current
