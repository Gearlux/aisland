"""The one place aisland runs ``git`` and captures its answer.

Every module that runs git and reads its answer runs it here, so their behaviour cannot drift apart. ``network=True``
is for a command that talks to a remote: it never prompts for credentials (missing ones fail at once) and gives up
after :data:`NETWORK_TIMEOUT` seconds unless ``timeout`` says otherwise — :data:`NO_LIMIT` for a transfer that may be
long (a first push to a mirror, a prune's push). A command reads no terminal: its stdin is empty unless ``input``
gives it text.

Every git command aisland runs against a remote — here or in a runner of its own (a mirror's push, a prune's
fetches) — also gives up on a remote that stops sending: :func:`network_options` and :func:`network_env`. A transfer
may take long, so that guard is no overall limit; :func:`fetch` is a best-effort fetch with the guard alone. git has
such a setting over HTTP(S) only; over SSH, ssh's own ``ServerAliveInterval`` bounds a silent server.
"""

import logging
import math
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from aisland.errors import AislandError

logger = logging.getLogger(__name__)

#: Seconds a ``network=True`` command may take when the caller names no ``timeout``.
NETWORK_TIMEOUT = 60.0
#: Seconds a git command talking to a remote may go without receiving a byte before it gives up.
STALL_SECONDS = 60
#: The ``timeout`` of a command with no overall limit (it still gives up on a remote that stops sending).
NO_LIMIT = math.inf


def network_options() -> List[str]:
    """The ``-c`` options that make git give up on a remote that has sent nothing for :data:`STALL_SECONDS`."""
    return ["-c", "http.lowSpeedLimit=1", "-c", f"http.lowSpeedTime={STALL_SECONDS}"]


def network_env() -> Dict[str, str]:
    """The environment of a git command talking to a remote: missing credentials fail at once, never a prompt."""
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def fetch(repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    """``git fetch <args>`` in ``repo``, best effort: no prompt, given up when the remote stops sending, no overall
    limit (a first fetch may be large). The caller reads ``returncode``."""
    return git(repo, "fetch", *args, network=True, timeout=NO_LIMIT)


def git(
    repo: Path,
    *args: str,
    network: bool = False,
    timeout: Optional[float] = None,
    check: bool = False,
    input: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> "subprocess.CompletedProcess[str]":
    """Run ``git -C <repo> <args>``, capturing its output as text.

    ``repo`` is git's ``-C``, never the process's working folder: a folder that is missing or no repository is git's
    exit code (128), not an exception. ``input`` is the command's stdin (else empty), ``env`` variables laid over the
    environment. ``check=True`` raises an :class:`AislandError` naming the command and git's own words when git exits
    non-zero; otherwise the caller reads ``returncode``.
    """
    environment = {**(network_env() if network else os.environ), **env} if env else (network_env() if network else None)
    if timeout is None and network:
        timeout = NETWORK_TIMEOUT
    command = ["git", "-C", str(repo), *(network_options() if network else []), *args]
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=environment,
        timeout=None if timeout == NO_LIMIT else timeout,
        input=input,
        stdin=None if input is not None else subprocess.DEVNULL,
    )
    logger.debug("git %s (in %s): exit %d", " ".join(args), repo, done.returncode)
    if check and done.returncode != 0:
        reason = done.stderr.strip() or done.stdout.strip() or f"exit {done.returncode}"
        raise AislandError(f"git {' '.join(args)} failed in {repo}: {reason}")
    return done


def read(repo: Path, *args: str) -> Optional[str]:
    """What ``git <args>`` prints in ``repo``, stripped; ``None`` when git could not answer (an unset key, a missing
    ref, ``repo`` no repository, git not installed)."""
    try:
        done = git(repo, *args)
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def remote_url(repo: Path, remote: str = "origin") -> Optional[str]:
    """The address of ``repo``'s ``remote``, or ``None`` for no such remote, no repository or no git."""
    return read(repo, "remote", "get-url", remote)
