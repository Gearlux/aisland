"""The one place aisland runs ``git`` and captures its answer.

Four modules each carried a byte-identical private copy of this call; one helper keeps their behaviour from
drifting apart. ``network=True`` is for a command that talks to a remote: it never prompts for credentials (missing
ones fail at once) and gives up after :data:`NETWORK_TIMEOUT` seconds unless ``timeout`` says otherwise.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from aisland.errors import AislandError

logger = logging.getLogger(__name__)

#: Seconds a ``network=True`` command may take when the caller names no ``timeout``.
NETWORK_TIMEOUT = 60.0


def git(
    repo: Path, *args: str, network: bool = False, timeout: Optional[float] = None, check: bool = False
) -> "subprocess.CompletedProcess[str]":
    """Run ``git <args>`` in ``repo``, capturing its output as text.

    ``check=True`` raises an :class:`AislandError` naming the command and git's own words when git exits non-zero;
    otherwise the caller reads ``returncode``.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"} if network else None
    if timeout is None and network:
        timeout = NETWORK_TIMEOUT
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, timeout=timeout)
    logger.debug("git %s (in %s): exit %d", " ".join(args), repo, done.returncode)
    if check and done.returncode != 0:
        reason = done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}"
        raise AislandError(f"git {' '.join(args)} failed in {repo}: {reason}")
    return done
