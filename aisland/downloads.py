"""Downloads with a time limit.

``urllib.request.urlretrieve`` takes no timeout: a server that accepts the connection and then sends nothing holds
the command forever. Every file aisland fetches (a userspace tool's archive, Jenkins' WAR and plugin manager) goes
through :func:`download` instead; a file pinned by a digest is checked with :func:`sha256`.
"""

import hashlib
import shutil
import urllib.request
from pathlib import Path

#: Seconds a download may wait for the server — to connect, or for the next block of data; not the whole transfer,
#: which may take minutes on a slow line.
TIMEOUT = 60


def download(url: str, target: Path, timeout: float = TIMEOUT) -> None:
    """Fetch ``url`` into ``target``. Raises ``OSError`` (``urllib.error.URLError``, ``TimeoutError``) when the
    server does not answer within ``timeout`` seconds; a partial ``target`` is removed."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response, target.open("wb") as handle:
            shutil.copyfileobj(response, handle)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def sha256(path: Path) -> str:
    """The hex sha256 of a file, read in chunks (constant memory)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
