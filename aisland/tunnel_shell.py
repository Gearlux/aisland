"""The terminal on the far side of ``aisland tunnel connect``: a login shell in a pseudo-terminal.

The tunnel's ``spawn`` request starts a program on the remote machine with three plain pipes —
stdin, stdout, stderr — and no terminal (measured on a remote box: ``tty`` printed ``not a tty``).
A shell on pipes has no line editing, no job control, no Ctrl-C and no window size, so ``vim`` and
``top`` do not work. This module is the program ``connect`` spawns instead: it opens a
pseudo-terminal, starts the account's login shell in it, and relays between the pipes and the
terminal.

Two decisions follow from where it runs:

* **Standard library only, run from its own source.** It is sent to the box as the text of a
  ``python3 -c`` argument (:func:`aisland.tunnel_connect.remote_command`), because the box need not
  have aisland on the PATH the tunnel gives a program (measured: it had not) — only a Python. So
  nothing is installed there, nothing here may import from aisland, and nothing may need a Python
  newer than the box's (``from __future__ import annotations`` needs 3.7).
* **stdin is framed, stdout is not.** The client has one stream to the box, and it must carry both
  keystrokes and window-size changes, so every chunk on stdin is a frame: a one-byte kind, a
  four-byte big-endian length, the payload (:func:`encode_data`, :func:`encode_resize`). The way
  back carries only the terminal's output, so stdout is the raw bytes.

The shell comes from the account's password entry, not ``$SHELL``: the tunnel starts programs with
an empty ``$SHELL`` (measured). It starts in the home folder unless a folder is given.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import pwd
import select
import signal
import struct
import sys
import termios
import time
from typing import List, Optional, Tuple

#: Frame kinds on stdin: terminal input, and a new window size.
FRAME_DATA = b"d"
FRAME_RESIZE = b"r"
_HEADER = struct.Struct(">cI")
_SIZE = struct.Struct(">HH")

#: Exit status when the starting folder does not exist; a shell's own statuses stay below 126.
EXIT_NO_FOLDER = 2

#: After the input ends, how long a program that ignores end-of-file keeps the terminal.
HANGUP_AFTER_S = 10.0

#: End-of-file typed at a terminal: a shell reading a fresh line leaves on it.
CTRL_D = b"\x04"


def encode_data(data: bytes) -> bytes:
    """A frame carrying terminal input."""
    return _HEADER.pack(FRAME_DATA, len(data)) + data


def encode_resize(rows: int, cols: int) -> bytes:
    """A frame carrying a new window size."""
    return _HEADER.pack(FRAME_RESIZE, _SIZE.size) + _SIZE.pack(rows, cols)


class FrameReader:
    """Splits a byte stream into ``(kind, payload)`` frames; a partial frame waits for its rest."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> List[Tuple[bytes, bytes]]:
        """Add ``data`` and return every frame it completes, in order."""
        self._buffer += data
        frames: List[Tuple[bytes, bytes]] = []
        while len(self._buffer) >= _HEADER.size:
            kind, length = _HEADER.unpack_from(self._buffer)
            end = _HEADER.size + length
            if len(self._buffer) < end:
                break
            frames.append((kind, self._buffer[_HEADER.size : end]))
            self._buffer = self._buffer[end:]
        return frames


def set_size(fd: int, rows: int, cols: int) -> None:
    """Give the terminal on ``fd`` a window size; the kernel tells the shell's job (SIGWINCH)."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def login_shell() -> Tuple[str, str]:
    """The account's shell and home folder, from its password entry."""
    entry = pwd.getpwuid(os.getuid())
    return entry.pw_shell or "/bin/sh", entry.pw_dir or "/"


def _write_all(fd: int, data: bytes) -> None:
    while data:
        written = os.write(fd, data)
        data = data[written:]


def relay(child: int, master: int, stdin: int = 0, stdout: int = 1) -> int:
    """Pass framed input to the terminal and its output back until the shell ends; its exit status.

    When the input ends, the terminal gets an end-of-file (Ctrl-D) rather than a hang-up, so the
    shell still runs what was typed before it and then leaves; a program that ignores Ctrl-D (an
    editor) is hung up :data:`HANGUP_AFTER_S` later, as a closed terminal would.
    """
    reader = FrameReader()
    open_input = True
    hangup_at: Optional[float] = None
    while True:
        watched = [master, stdin] if open_input else [master]
        timeout = None if hangup_at is None else max(0.0, hangup_at - time.monotonic())
        ready, _, _ = select.select(watched, [], [], timeout)
        if not ready and hangup_at is not None:
            os.kill(child, signal.SIGHUP)
            hangup_at = None
            continue
        if master in ready:
            try:
                output = os.read(master, 65536)
            except OSError as exc:  # Linux reports the shell's end as EIO on the terminal
                if exc.errno != errno.EIO:
                    raise
                output = b""
            if not output:
                break
            _write_all(stdout, output)
        if open_input and stdin in ready:
            data = os.read(stdin, 65536)
            if not data:
                open_input = False
                _write_all(master, CTRL_D)
                hangup_at = time.monotonic() + HANGUP_AFTER_S
                continue
            for kind, payload in reader.feed(data):
                if kind == FRAME_RESIZE:
                    set_size(master, *_SIZE.unpack(payload))
                elif kind == FRAME_DATA:
                    _write_all(master, payload)
    _, status = os.waitpid(child, 0)
    # By hand rather than os.waitstatus_to_exitcode: the box's Python may predate 3.9.
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)


def main(argv: List[str]) -> int:
    """``rows cols [folder]``: run the login shell in a terminal of that size, in that folder."""
    rows, cols = int(argv[0]), int(argv[1])
    folder: Optional[str] = argv[2] if len(argv) > 2 and argv[2] else None
    shell, home = login_shell()
    start = folder or home
    if not os.path.isdir(start):
        sys.stderr.write(f"aisland: no such folder on the remote machine: {start}\n")
        return EXIT_NO_FOLDER
    child, master = pty.fork()
    if child == 0:  # pragma: no cover - the child replaces itself with the shell
        os.chdir(start)
        os.environ["SHELL"] = shell
        os.execv(shell, ["-" + os.path.basename(shell)])
    set_size(master, rows, cols)
    return relay(child, master)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
