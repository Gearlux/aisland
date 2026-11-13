# A terminal on the remote machine rides the VS Code tunnel it already runs (2026-10-21)

## Context

`aisland tunnel start` puts a VS Code Remote Tunnel on a box published only through an HTTPS ingress: no inbound
port, and no sshd. The editor attaches to it; a plain terminal had no way in. Two ways round were refused: an
sshd on the box (there is none, and running one is a platform change) and a second tunnel published for it.

The VS Code CLI's own source (`cli/src/tunnels/control_server.rs`) shows the tunnel is enough. It publishes a control
port, 31545, whose `spawn` request starts any program on the machine and streams its stdin, stdout and stderr back,
and a connection arriving through the tunnel needs no handshake of its own (`requires_auth: AuthRequired::None`) — the
relay admitted only the tunnel's owner. Measured on a remote box (2026-10-21), over `devtunnel connect` and a
30-line MessagePack client: `version` 1.143.0 protocol 6 on connect; `spawn sh -c 'uname -a; tty; …'` answered with
the box's kernel, `not a tty`, an empty `$SHELL`, `python3` on the PATH but no `aisland`, and what was written to
stdin echoed back through `cat`.

## Decision

- **The way through is Microsoft's `devtunnel connect`, on the connecting machine only.** Signed in with the
  account the box's tunnel runs under, `devtunnel list -j` lists that tunnel (label `vscode-server-launcher`, its
  name as another label) and `connect <id>` forwards its ports to `localhost` on the same numbers — it has no option
  to choose another. aisland starts that forward per session and stops it when the shell ends.
- **The client is aisland's** (`tunnel/connect.py`): MessagePack over the forwarded port — `spawn`, then
  `streams_started`, `stream_data`, `stream_ended` and the spawn's result `{message, exit_code}`. `msgpack` is the
  `[connect]` extra: only the connecting machine needs it.
- **A real terminal on the far side, from a helper sent as source** (`tunnel/shell.py`): `spawn` gives pipes, so the
  program spawned is `python3 -c <the helper's source>`, which opens a pseudo-terminal and runs the account's shell
  (from its password entry — `$SHELL` is empty there) in the home folder or `--cwd`. The box needs a Python and
  nothing else; the helper imports only the standard library. Its stdin is framed — one-byte kind, four-byte length —
  so one stream carries keystrokes and window sizes; its stdout is the terminal's bytes.
- **The shell is interactive, not a login shell** (chosen 2026-10-21), as an editor's terminal starts it on
  Linux. Started as a login shell it read `~/.profile` only, and on a box whose `~/.profile` was trimmed of the block
  that loads `~/.bashrc` it had no colour prompt and no `ls --color` (measured: `PS1` was `/etc/bash.bashrc`'s,
  `alias ls` not found); interactive, `~/.bashrc` runs. `~/.profile`'s PATH lines are not lost: the tunnel's
  environment, inherited from the shell `aisland tunnel start` ran in, already carries them.
- **When the input ends the shell gets Ctrl-D, not a hang-up**, so what was typed before still runs; a program that
  ignores it is hung up 10 s later.
- **A taken port 31545 is refused**, naming its process: a forward started by hand reaches a machine nothing names.

## Consequences

- Nothing is added on the box and no second tunnel exists; the terminal lives exactly as long as the tunnel does.
- One session at a time per connecting machine (the fixed local port).
- The control protocol is the VS Code CLI's, not a published one: a CLI release that changes `spawn` or its streams
  breaks `connect`, and `tests/fake_devtunnel.py` must change with it.
- `connect`'s TAB answers from the last list (`devtunnels.json` in the tunnel state folder), never the network.

## Example

```python
from aisland import tunnel_connect, tunnel_shell

tunnel.connect.remote_command(24, 80, "/mnt/workspace")
# ('sh', ['-c', 'for p in python3 python; do … exec "$p" -c "$0" "$@"; done; …', '<helper source>', '24', '80',
#         '/mnt/workspace'])
tunnel.shell.encode_resize(33, 111)    # b'r\x00\x00\x00\x04\x00!\x00o' — a window size on the helper's stdin
```

## What you may change

- The helper's frame kinds (add one with its handler in `tunnel.shell.relay` and a test in
  `tests/test_tunnel_connect.py`), and `HANGUP_AFTER_S`.
- The forward, for another way through the relay to port 31545 — `connect`'s refusals and tests stay.
- Never: an import outside the standard library in `tunnel/shell.py`
  (`::test_the_helper_runs_on_a_python_without_site_packages`), or reusing a forward aisland did not start.

---

[The architecture and every decision](../architecture.md#the-decisions)
