"""The one place aisland runs ``git`` and captures its answer.

Four modules each carried a byte-identical private copy of this call; one helper keeps their behaviour from
drifting apart. ``network=True`` is for a command that talks to a remote: it never prompts for credentials (missing
ones fail at once) and gives up after :data:`NETWORK_TIMEOUT` seconds unless ``timeout`` says otherwise.

Every git command aisland runs against a remote — here or in a runner of its own (a mirror's push, a prune's
fetches) — also gives up on a remote that stops sending: :func:`network_options` and :func:`network_env`. A transfer
may take long, so that guard is no overall limit; :func:`fetch` is a best-effort fetch with the guard alone. git has
such a setting over HTTP(S) only; over SSH, ssh's own ``ServerAliveInterval`` bounds a silent server.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from aisland.errors import AislandError

logger = logging.getLogger(__name__)

#: Seconds a ``network=True`` command may take when the caller names no ``timeout``.
NETWORK_TIMEOUT = 60.0
#: Seconds a git command talking to a remote may go without receiving a byte before it gives up.
STALL_SECONDS = 60


def network_options() -> List[str]:
    """The ``-c`` options that make git give up on a remote that has sent nothing for :data:`STALL_SECONDS`."""
    return ["-c", "http.lowSpeedLimit=1", "-c", f"http.lowSpeedTime={STALL_SECONDS}"]


def network_env() -> Dict[str, str]:
    """The environment of a git command talking to a remote: missing credentials fail at once, never a prompt."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def fetch(repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """``git fetch <args>`` in ``repo``, best effort: no prompt, given up when the remote stops sending, no overall
    limit (a first fetch may be large). The caller reads ``returncode``."""
    done = subprocess.run(
        ["git", *network_options(), "fetch", *args], cwd=repo, capture_output=True, text=True, env=network_env()
    )
    logger.debug("git fetch %s (in %s): exit %d", " ".join(args), repo, done.returncode)
    return done


def git(
    repo: Path, *args: str, network: bool = False, timeout: Optional[float] = None, check: bool = False
) -> "subprocess.CompletedProcess[str]":
    """Run ``git <args>`` in ``repo``, capturing its output as text.

    ``check=True`` raises an :class:`AislandError` naming the command and git's own words when git exits non-zero;
    otherwise the caller reads ``returncode``.
    """
    env = network_env() if network else None
    if timeout is None and network:
        timeout = NETWORK_TIMEOUT
    command = ["git", *(network_options() if network else []), *args]
    done = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=env, timeout=timeout)
    logger.debug("git %s (in %s): exit %d", " ".join(args), repo, done.returncode)
    if check and done.returncode != 0:
        reason = done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}"
        raise AislandError(f"git {' '.join(args)} failed in {repo}: {reason}")
    return done
