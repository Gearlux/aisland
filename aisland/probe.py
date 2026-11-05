"""A state probe: a short, read-only command whose answer a status line, the doctor or an MCP tool waits for.

A probe never hangs its caller: past :data:`PROBE_SECONDS` it is given up, like a program that is not installed, and
the caller reads "no answer" (a stuck VM's ``colima status`` is not a running engine).
"""

import logging
import shutil
import socket
import subprocess
from typing import Optional, Sequence, Tuple

#: How long a probe may take. A status question answers in well under a second; one that takes this long is stuck.
PROBE_SECONDS = 30.0

_log = logging.getLogger(__name__)


def run(argv: Sequence[str]) -> Optional["subprocess.CompletedProcess[str]"]:
    """Run ``argv`` with its output captured; ``None`` when the program is missing or gives no answer in time."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=PROBE_SECONDS)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        _log.debug("%s gave no answer within %.0f s", argv[0], PROBE_SECONDS)
        return None


def succeeds(argv: Sequence[str]) -> bool:
    """Whether ``argv`` answers with exit code 0 in time."""
    result = run(argv)
    return result is not None and result.returncode == 0


def port_answers(port: int, wait: float = 1.0) -> bool:
    """Whether something on this machine accepts a connection on ``port`` within ``wait`` seconds."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(wait)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def listener(port: int) -> Optional[Tuple[str, str]]:
    """The pid and command name of the process listening on ``port`` (``lsof``), or ``None`` when lsof is missing,
    names nobody or gives no answer in time."""
    lsof = shutil.which("lsof")
    answer = run([lsof, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"]) if lsof else None
    fields = {line[0]: line[1:] for line in (answer.stdout if answer else "").splitlines() if line}
    return (fields["p"], fields.get("c", "?")) if "p" in fields else None
