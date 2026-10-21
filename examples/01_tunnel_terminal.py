"""What runs on the remote machine during `aisland tunnel connect` — run here, the way the machine runs it.

`connect` asks the remote machine's VS Code tunnel to start one program: a small helper, sent as the
source text of a `python3 -c` argument, that opens a real terminal and starts the account's shell in
it. This example starts that same program on this machine, through the same launcher, and talks to it
the way `connect` does: a window size, then keystrokes, each as a frame on its stdin.

It shows, and asserts:

- the shell runs in a terminal of the size it was given (`stty size`), in the folder asked for;
- the shell is interactive, not a login shell, so its `~/.bashrc` (or `~/.zshrc`) runs — that is where
  a prompt's colours and `ls --color` live;
- the shell's exit status comes back as the program's.

No network, no tunnel and no `devtunnel` are needed: `python examples/01_tunnel_terminal.py`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from aisland import tunnel_connect, tunnel_shell


def main() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch) / "home"
        folder = Path(scratch) / "project"
        home.mkdir()
        folder.mkdir()
        # A start-up file of the kind a remote account has; both, whichever shell the account uses.
        for rc in (".bashrc", ".zshrc"):
            (home / rc).write_text("export GREETING='read from the start-up file'\n")

        # The very program `connect` asks the remote machine to start: rows, columns, folder.
        command, arguments = tunnel_connect.remote_command(24, 80, str(folder))
        typed = 'stty size; pwd; echo "$GREETING"; exit 3\n'
        frames = tunnel_shell.encode_resize(40, 132) + tunnel_shell.encode_data(typed.encode())
        result = subprocess.run(
            [command, *arguments],
            input=frames,
            capture_output=True,
            env={**os.environ, "HOME": str(home), "TERM": "xterm-256color"},
            timeout=60,
        )

    screen = result.stdout.decode(errors="replace").replace("\r", "")
    print(screen)
    assert "40 132" in screen, "the window size reached the terminal"
    assert os.path.realpath(folder) in screen or str(folder) in screen, "the shell started in the folder asked for"
    assert "read from the start-up file" in screen, "the interactive start-up file ran"
    assert result.returncode == 3, f"the shell's exit status came back (got {result.returncode})"
    print(f"exit status {result.returncode}: terminal 40x132, start-up file read, folder {folder.name}")


if __name__ == "__main__":
    main()
