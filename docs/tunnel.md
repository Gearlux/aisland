# Reaching a remote machine (`aisland tunnel`)

A remote box is often published through an HTTPS ingress that carries HTTP and WebSocket but not raw
TCP — a browser IDE under a path prefix, nothing else — so there is no port for Remote-SSH to attach
to, and asking for one is a platform change. `aisland tunnel` uses a VS Code Remote Tunnel instead:
the remote machine dials *out* to the relay, and your editor — or a plain terminal — attaches to that
tunnel. Nothing is exposed through the ingress and nothing listens on the box.

| You want | Run | Where |
| --- | --- | --- |
| the tunnel | `aisland tunnel start` | the remote machine |
| your editor on it | `aisland tunnel open <workspace>` | your machine |
| a terminal on it | `aisland tunnel connect <workspace>` | your machine |

## On the remote machine: start the tunnel

The machine needs no workspace and no checkout of its own: `pip install aisland` there is enough
(its dependencies come from the index). For a box with no index, download the wheels on a
machine that has one, for the box's platform and Python — a Linux box on x86-64 with Python 3.12 here; two of the
packages it installs (PyYAML, MarkupSafe) ship compiled wheels, so the downloading machine's own would not
install there:

```bash
python -m pip download aisland -d dist --only-binary=:all: --platform manylinux2014_x86_64 --python-version 3.12
```

Copy the `dist` folder over and run `pip install --no-index --find-links dist aisland` there
(`--platform manylinux2014_aarch64` for an ARM box). Then:

```bash
aisland tunnel start                              # name: $WORKSPACE, else this machine's hostname
aisland tunnel start --cli ~/vscode_cli.tar.gz    # a box that cannot reach the download host
aisland tunnel status                             # running? under which name and account?
aisland tunnel logs -n 50
aisland tunnel stop
```

The first start signs in — a code to enter in a browser — and keeps the account, so later starts need
no interaction; `aisland tunnel login --provider microsoft` picks the other account. The sign-in
happens before the tunnel is spawned, because `code tunnel` takes no account options and asks
interactively when it has none, which a detached process cannot answer.

`aisland tunnel start` runs `code tunnel --accept-server-license-terms`: starting it accepts the
VS Code Server licence (`code.visualstudio.com/license/server`) for you, as the detached process could not
answer that question either.

The tunnel keeps everything in `~/.aisland/tunnel` on that machine — the CLI binary, the log, the
pidfile, the account and the remembered workspaces — so every command finds the same tunnel wherever
it runs from. A box whose `$HOME` does not survive a restart points it at storage that does, once:

```bash
export AISLAND_TUNNEL_HOME=/mnt/workspace/.aisland/tunnel
```

## From your machine: your editor (`open`)

Paste the workspace's address once — after that it completes:

```bash
aisland tunnel open 'https://<host>/<workspace>/?folder=/mnt/workspace'
aisland tunnel open 'https://<host>/<workspace>/?workspace=/mnt/workspace/repo/dev.code-workspace'
aisland tunnel open <workspace><TAB>   # the workspaces you have opened, newest first
aisland tunnel list
aisland tunnel clear <name>      # forget one — a name or the address
aisland tunnel clear --all       # forget all of them
```

The address carries what to open in whichever parameter the IDE wrote — `workspace` for a
`.code-workspace` file, `folder` for a directory (`workspace` wins if both are there) — and its last
path segment suggests the tunnel name. Only suggests: an ingress serving `<workspace>-prod` says
nothing about what the box inside calls itself, which is usually `<workspace>`. So when the two forms
differ, `open` uses whichever is already remembered, asks when neither is, and takes `--name` when
you would rather say it than be asked:

```bash
aisland tunnel open 'https://<host>/<workspace>-prod/?folder=/mnt/workspace' --name <workspace>
```

## From your machine: a terminal (`connect`)

A terminal goes through the same tunnel — no sshd and nothing else on the remote machine. Your
machine needs Microsoft's `devtunnel`, signed in with the account the remote tunnel runs under, and
the `[connect]` extra:

```bash
brew install --cask devtunnel        # Linux: curl -sL https://aka.ms/DevTunnelCliInstall | bash
devtunnel user login                 # -g for a GitHub account
pip install 'aisland[connect]'
```

```bash
aisland tunnel connect                                   # your VS Code tunnels
aisland tunnel connect <workspace>                       # your shell there, in your home folder
aisland tunnel connect <workspace> --cwd /mnt/workspace  # … starting in that folder
aisland tunnel connect --json                            # the list, machine-readable
```

```
$ aisland tunnel connect
NAME            TUNNEL ID                MACHINE
<workspace>     brave-otter-7k2m9qx.euw    online
Connect with: aisland tunnel connect <name>   (TAB completes)
```

The shell is your account's, started the way an editor's terminal starts it: interactive, so your
`~/.bashrc` (or `~/.zshrc`) runs — its prompt, its colours, its aliases — and in a real terminal, so
editors, Ctrl-C and resizing work. `connect` exits with the shell's status.

The remote machine needs a Python on its PATH and nothing else: what runs there is a small helper
sent with the request, which opens the terminal and starts the shell.
[`examples/01_tunnel_terminal.py`](../examples/01_tunnel_terminal.py) runs that helper on your own
machine, exactly as the remote machine runs it.

Each session forwards the tunnel's control port, 31545, to your machine and closes the forward when
the shell ends, so one session runs at a time. A `devtunnel connect` you started by hand holds that
port and is named in the error until you stop it:

```
Error: Port 31545 on this machine is already taken by pid 45052 (osx-arm64-devtunnel) — a `devtunnel connect` started by hand forwards that same port, to a machine aisland cannot tell. Stop it and run this again.
```

## When something does not connect

`aisland tunnel status`, on the remote machine, reports the name the tunnel registered under and the
account it holds — the two things to compare when your editor or `connect` cannot find a tunnel that
is running. `connect` names the tunnels your `devtunnel` account sees; a tunnel listed `offline` is not
running on its machine.

On your machine, `aisland doctor` checks what `connect` needs once you have used it — `devtunnel` on
the PATH, the `[connect]` extra, the sign-in — and prints the command that fixes a gap.

`aisland tunnel` is deliberately not part of `aisland services start` / `stop`: a tunnel into your own laptop
is nobody's intent, so it runs only where you ask for it. `aisland doctor` probes the relay only on a
machine that has used the tunnel.

Why it is built this way: [architecture.md](architecture.md) → "A terminal on the remote machine rides
the VS Code tunnel it already runs".
