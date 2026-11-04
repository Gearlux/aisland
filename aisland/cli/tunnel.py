"""`aisland tunnel …`: a VS Code Remote Tunnel to the machine aisland runs on (:mod:`aisland.tunnel`), and a terminal
on a remote one through it (:mod:`aisland.tunnel.connect`).
"""

import json
from pathlib import Path
from typing import Optional, Tuple

import click

from aisland.cli._base import _Group
from aisland.errors import AislandError
from aisland.tunnel import connect as tunnel_connect_mod
from aisland.tunnel import host as tunnel_mod

# --- Remote Tunnel ---


@click.group(cls=_Group)
def tunnel() -> None:
    """Reach a remote machine from your local editor, over a VS Code Remote Tunnel.

    A box published through an HTTPS ingress — a browser IDE under a path prefix — has no port for
    Remote-SSH to attach to. A tunnel needs none: the remote machine dials OUT to the relay and your
    editor attaches to that tunnel, so nothing has to be exposed through the ingress.

    Two sides. On the machine you want to reach:

    \b
        aisland tunnel start --provider microsoft

    The first start signs in — it prints a code to enter in a browser — and the account is kept, so
    later starts need no interaction. The tunnel takes the machine's own workspace name ($WORKSPACE),
    else its hostname, unless --name says otherwise.

    Everything the tunnel keeps lives in ~/.aisland/tunnel on that machine, wherever you run the
    command from. On a box whose $HOME does not survive a restart, put it on the mount that does —
    once, in the shell profile — and the sign-in lasts:

    \b
        export AISLAND_TUNNEL_HOME=/mnt/workspace/.aisland/tunnel

    On your own machine, paste the address the browser IDE shows — once:

    \b
        aisland tunnel open 'https://HOST/WS-prod/?folder=/mnt/workspace'
        aisland tunnel open 'https://HOST/WS-prod/?workspace=/mnt/workspace/repo/x.code-workspace'

    Whichever of the two query parameters the address carries says what to open: a folder, or a
    .code-workspace file. An address ending in -prod (or -dev, -staging, -test, -qa) names the
    INGRESS, though, not the tunnel — the box inside usually calls itself WS. When the two differ and
    neither is remembered yet, `open` asks which one it is; --name says it outright.

    After that the workspace is remembered, so it is just:

    \b
        aisland tunnel open WS<TAB>

    A terminal instead of the editor — the same tunnel, nothing added on the remote machine:

    \b
        aisland tunnel connect WS<TAB>

    `list` shows what is remembered, `clear` forgets one or all of them, and `status` says whether a
    tunnel runs on this machine, under which name and which account. None of this is part of
    `aisland services start` / `stop`: a tunnel into your own laptop is nobody's intent, so it runs only where
    you ask for it.
    """


@tunnel.command(name="start")
@click.option("--name", default=None, help="Tunnel name. Default: $WORKSPACE, else this machine's hostname.")
@click.option(
    "--provider",
    type=click.Choice(["github", "microsoft"]),
    default=None,
    help="Account to sign in with; remembered on this machine for the next start.",
)
@click.option(
    "--cli",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Install the VS Code CLI from this archive instead of downloading it (for a box that cannot reach it).",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def tunnel_start(name: Optional[str], provider: Optional[str], cli: Optional[Path], as_json: bool) -> None:
    """Bring up a tunnel to THIS machine, so a local editor can attach to it.

    Fetches the VS Code CLI on first use (--cli installs it from an archive you uploaded instead, for
    a box that cannot reach the download host), then leaves the tunnel running in the background and
    reports what its log said: the tunnel's address once it is up, or the sign-in to complete when no
    account is stored yet. Running it again while a tunnel runs reports that one rather than starting
    a second. `aisland tunnel logs` and `aisland tunnel status` follow it from there.
    """
    payload = tunnel_mod.start(name=name, cli=cli, provider=provider, sign_in=not as_json)
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if payload.get("signin_required"):
        click.echo(f"This machine has no account yet. Sign in, then start again:\n    {payload['command']}")
        return
    if payload.get("already_running"):
        click.echo(f"A tunnel is already running (pid {payload['pid']}).")
    elif payload.get("pending_signin"):
        signin = payload["pending_signin"]
        click.echo(f"Sign in to continue: {signin['url']}" + (f" — code {signin['code']}" if signin["code"] else ""))
        click.echo("    Then run `aisland tunnel status` to see the tunnel come up.")
    elif payload.get("url"):
        click.echo(f"Tunnel '{payload['name']}' is up.\n    {payload['url']}")
    else:
        click.echo(f"Tunnel '{payload['name']}' started, but its log said nothing yet. `aisland tunnel logs`.")
    if payload.get("log"):
        click.echo(f"    log   {payload['log']}\n    stop  aisland tunnel stop")


@tunnel.command(name="open")
@click.argument("name_or_url", required=False, shell_complete=tunnel_mod.complete_workspace)
@click.option(
    "--name",
    default=None,
    help="The tunnel's name, when the address does not carry it (an ingress serving WS-prod fronts a "
    "box that names its tunnel WS). Wins over the name in the address.",
)
@click.option(
    "--path",
    "--folder",
    "open_path",
    default=None,
    help="What to open on the remote machine — a folder or a .code-workspace file. Default: whatever "
    "the address carries, else what this workspace was opened with last time.",
)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output; never asks anything.")
def tunnel_open(name_or_url: Optional[str], name: Optional[str], open_path: Optional[str], as_json: bool) -> None:
    """Attach YOUR editor to a tunnel — by address the first time, by name (TAB) after that.

    NAME_OR_URL is the address the browser IDE shows, a name you have opened before (TAB completes
    it), or nothing at all for the workspace you opened last.

    From an address: its `workspace` or `folder` parameter says what to open — a .code-workspace file
    or a directory — and its last path segment suggests the tunnel name. Suggests, because an ingress
    serving WS-prod says nothing about what the box inside calls itself; when the stripped form is
    the one remembered it is used, when neither is remembered yet you are asked, and --name settles
    it without being asked.
    """
    payload = tunnel_mod.open_editor(target=name_or_url, folder=open_path, name=name, ask=not as_json)
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    where = f" ({payload['folder']})" if payload["folder"] else ""
    click.echo(f"Opening VS Code on '{payload['name']}'{where}…")


@tunnel.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def tunnel_list(as_json: bool) -> None:
    """The workspaces you have opened, newest first — what `open` completes on TAB.

    Each row is the tunnel name, what was opened there (a folder or a .code-workspace file), how long
    ago, and the address it was learned from. The twenty most recent are kept; `clear` forgets them.
    """
    entries = tunnel_mod.load_store()
    if as_json:
        click.echo(json.dumps(tunnel_mod.store_payload(entries), indent=2))
        return
    click.echo(tunnel_mod.render_store(entries))


@tunnel.command(name="clear")
@click.argument("names_or_urls", nargs=-1, shell_complete=tunnel_mod.complete_workspace)
@click.option("--all", "clear_all", is_flag=True, help="Forget every remembered workspace.")
@click.option("--yes", is_flag=True, help="Do not ask before forgetting everything.")
def tunnel_clear(names_or_urls: Tuple[str, ...], clear_all: bool, yes: bool) -> None:
    """Forget a remembered workspace (by name or address), or all of them with --all.

    NAMES_OR_URLS take either spelling — the name TAB offers, or the address you pasted. This only
    forgets what `open` completes on; it never touches a running tunnel (`stop` does that).
    """
    if clear_all:
        remembered = len(tunnel_mod.load_store())
        if not remembered:
            click.echo("No workspace remembered yet.")
            return
        plural = "s" if remembered > 1 else ""
        if not yes and not click.confirm(f"Clear all {remembered} remembered workspace{plural}?"):
            click.echo("Kept.")
            return
        click.echo(f"Cleared {tunnel_mod.forget_all()} workspace{plural}.")
        return

    if not names_or_urls:
        raise click.UsageError("Name a workspace to clear, or pass --all.")
    cleared, unknown = tunnel_mod.forget(names_or_urls)
    for name in cleared:
        click.echo(f"Cleared '{name}'.")
    if unknown:
        raise AislandError(
            "Not remembered: " + ", ".join(f"'{name}'" for name in unknown) + " (`aisland tunnel list` shows what is)."
        )
    click.echo(f"{len(tunnel_mod.load_store())} workspace(s) remembered.")


@tunnel.command(name="connect")
@click.argument("target", required=False, shell_complete=tunnel_connect_mod.complete_tunnel)
@click.option(
    "--cwd",
    "folder",
    default=None,
    help="Folder on the remote machine to start the shell in. Default: your home folder there.",
)
@click.option("--json", "as_json", is_flag=True, help="With no tunnel named: the list, machine-readable.")
@click.pass_context
def tunnel_connect(ctx: click.Context, target: Optional[str], folder: Optional[str], as_json: bool) -> None:
    """A terminal on a remote machine, through the VS Code tunnel `aisland tunnel start` runs there.

    Run it on YOUR machine. With no name it lists the VS Code tunnels of your account; with one it
    opens your shell over there, in a real terminal (editors, Ctrl-C, resizing and your ~/.bashrc's
    colours work), and
    exits with the shell's status:

    \b
        aisland tunnel connect
        aisland tunnel connect WS<TAB>
        aisland tunnel connect WS --cwd /mnt/workspace

    Nothing is added on the remote machine: no sshd, no second tunnel. This machine needs Microsoft's
    `devtunnel`, signed in with the account the remote tunnel runs under (`devtunnel user login`, or
    `-g` for GitHub), and the [connect] extra (pip install 'aisland[connect]').
    """
    if target is None:
        tunnels = tunnel_connect_mod.list_tunnels()
        if as_json:
            click.echo(json.dumps(tunnel_connect_mod.tunnels_payload(tunnels), indent=2))
            return
        click.echo(tunnel_connect_mod.render_tunnels(tunnels))
        return
    if as_json:
        raise click.UsageError("--json lists the tunnels; it takes no tunnel name.")
    ctx.exit(tunnel_connect_mod.connect(target, folder=folder))


@tunnel.command(name="status")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def tunnel_status(as_json: bool) -> None:
    """Whether a tunnel to this machine is running, and under which account.

    Run it on the machine you started the tunnel on: it reads that machine's own state (the process
    it started, the account it signed in with, the tunnel address once the log carries it). On your
    own machine it reports your laptop's tunnel, not the remote one.
    """
    payload = tunnel_mod.status()
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if not payload["running"]:
        click.echo("No tunnel running on this machine. Start one with `aisland tunnel start`.")
    else:
        click.echo(f"Tunnel running (pid {payload['pid']}).")
        if payload.get("url"):
            click.echo(f"    {payload['url']}")
    click.echo(f"    name       {payload['name'] or '(none registered yet — see the log)'}")
    click.echo(f"    account    {payload['account'] or '(not signed in — aisland tunnel login)'}")
    click.echo(f"    provider   {payload['provider'] or '(none chosen)'}")
    click.echo(f"    workspaces {payload['workspaces']} remembered")


@tunnel.command(name="logs")
@click.option("--lines", "-n", default=80, show_default=True, help="How many trailing lines to show.")
def tunnel_logs(lines: int) -> None:
    """The tunnel process's own log — where the sign-in prompt and the tunnel address appear.

    This is what to read when `start` said nothing useful: the CLI writes its progress, its
    device-code prompt and any relay error here.
    """
    text = tunnel_mod.logs(lines=lines)
    click.echo(text if text else f"No log yet: {tunnel_mod.log_path()}")


@tunnel.command(name="login")
@click.option(
    "--provider",
    type=click.Choice(["github", "microsoft"]),
    default=None,
    help="Account to sign in with; remembered on this machine.",
)
def tunnel_login(provider: Optional[str]) -> None:
    """Sign the VS Code CLI in, so the next start needs no interaction.

    `start` already does this when no account is stored; run it on its own to sign in ahead of time,
    or to switch account with --provider. The choice is remembered on this machine, which is why it
    is not in the workspace's committed configuration: the account is yours, not the workspace's.
    """
    code = tunnel_mod.login(provider=provider)
    if code != 0:
        raise AislandError(f"Sign-in failed (exit {code}).")
    click.echo("Signed in.")


@tunnel.command(name="stop")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def tunnel_stop(as_json: bool) -> None:
    """Stop the tunnel to this machine; saying nothing ran is not an error.

    Run it on the machine the tunnel runs on. The remembered workspaces are untouched (`clear`
    forgets those), and the sign-in is kept, so a later `start` needs no interaction.
    """
    payload = tunnel_mod.stop()
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo("Tunnel stopped." if payload["stopped"] else "No tunnel was running.")
