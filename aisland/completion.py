"""Shell completion installation for every CLI in the workspace.

This used to live as ~70 lines of inline shell in ``setup.sh`` (sections 5/5a/5b), which
had one practical consequence: a completion fix could only reach a machine by re-running
the whole provisioning script — venv, ``aisland setup``, git config and all. Nobody re-runs
that to refresh a tab-completion, so the generated rc file silently rotted for weeks while
every ``<TAB>`` kept working for the apps installed back when it was written. Owning it here
makes it a one-second command, and puts it where aisland's own tests can reach it.

**Three families, because the workspace has three kinds of CLI:**

* **liquifai apps** (``recordstream``, ``matrainer``, ``sonair``, ``annotaide``, …) declare
  themselves in the ``liquifai.apps`` entry-point group. :func:`liquifai.completion.install_for_apps`
  discovers and installs them — called in-process, not through the
  ``liquifai-install-completions`` console script, so a missing PATH entry cannot silently
  skip the whole family.
* **click apps** (``aisland``) ship their own ``_<PROG>_COMPLETE`` protocol.
* **argparse apps** (``streamstudio``) use argcomplete's ``register-python-argcomplete``.

Every block is marker-delimited, so re-running rewrites in place rather than appending.

Programs are looked up in the ACTIVE VENV's ``bin`` first and only then on ``PATH``. That is
deliberate: the pruner drops blocks for programs that no longer exist, and a ``PATH``-only
check would call every app missing whenever the command runs from an unactivated venv —
deleting the entire file. Resolving against ``sys.prefix`` makes the answer independent of
whether the caller sourced ``project.bashrc``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Literal, NamedTuple, Optional, Sequence, Tuple

from aisland.manager import get_workspace_root

#: The rc fragment `project.bashrc` sources. Gitignored, so writing it leaves no diff.
DEFAULT_RC_NAME = ".project.bashrc.completion"

_NON_LIQUIFAI_START = "# >>> acme completion for non-liquifai CLIs >>>"
_NON_LIQUIFAI_END = "# <<< acme completion for non-liquifai CLIs <<<"

_LIQUIFAI_BLOCK_PREFIX = "# >>> liquifai completion for "

#: The command a reader of the generated file is told to re-run. Pinned against the real
#: click command by `tests/test_completion.py`, because a provenance header naming a command
#: the CLI does not accept is worse than none (workspace mandate).
REGENERATE_COMMAND = "aisland completion install"

CliKind = Literal["click", "argcomplete"]


class NonLiquifaiCli(NamedTuple):
    """A workspace CLI that is not a liquifai app, and how it completes."""

    prog: str
    kind: CliKind


#: DATA, not behaviour — a new non-liquifai CLI is one row here. Kept a plain table for the
#: same reason sonair's backend table is: the per-kind rendering differs in one string.
NON_LIQUIFAI_CLIS: Tuple[NonLiquifaiCli, ...] = (
    NonLiquifaiCli("aisland", "click"),
    NonLiquifaiCli("streamstudio", "argcomplete"),
)


class CompletionReport(NamedTuple):
    """What :func:`install_completions` did, so the CLI can report it and tests can assert it."""

    rc: Path
    shell: str
    liquifai_apps: List[str]
    non_liquifai: List[str]
    pruned: List[str]


def default_target_rc() -> Path:
    """The workspace-local rc fragment, resolved from the workspace root (never hardcoded)."""
    return get_workspace_root() / DEFAULT_RC_NAME


def _program_exists(prog: str) -> bool:
    """Is ``prog`` installed? Venv-first — see the module docstring for why that matters."""
    if (Path(sys.prefix) / "bin" / prog).exists():
        return True
    return shutil.which(prog) is not None


def _click_block(prog: str, shell: str) -> List[str]:
    """The click completion snippet — ``eval`` of the app's own generated script.

    Click refuses to emit for bash < 4.4 and macOS still ships ``/bin/bash`` 3.2, so the bash
    form is guarded on ``BASH_VERSINFO`` rather than emitting a script that errors on every
    shell start. (A Homebrew bash 5 login shell sails past it; the guard is for the machine
    whose interactive shell is the system one.)
    """
    var = f"_{prog.replace('-', '_').upper()}_COMPLETE"
    emit = f'eval "$({var}={shell}_source {prog} 2>/dev/null)" || true'
    if shell != "bash":
        return [emit]
    return [
        'if [ "${BASH_VERSINFO[0]}" -gt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 4 ]; }; then',
        f"    {emit}",
        "fi",
    ]


def _argcomplete_block(prog: str, shell: str) -> List[str]:
    """The argcomplete snippet. Skipped entirely when the registrar is not installed."""
    if not _program_exists("register-python-argcomplete"):
        return []
    return [f'eval "$(register-python-argcomplete --shell {shell} {prog} 2>/dev/null)" || true']


def _render_non_liquifai(shell: str) -> Tuple[str, List[str]]:
    """The whole non-liquifai block, plus the programs it covers."""
    lines = [
        _NON_LIQUIFAI_START,
        "# AUTO-GENERATED — DO NOT EDIT BY HAND.",
        f"# Generated by: {REGENERATE_COMMAND}",
        "# Source template: aisland/aisland/completion.py (_render_non_liquifai)",
        f"# To regenerate:  {REGENERATE_COMMAND}",
    ]
    covered: List[str] = []
    for cli in NON_LIQUIFAI_CLIS:
        if not _program_exists(cli.prog):
            continue
        body = _click_block(cli.prog, shell) if cli.kind == "click" else _argcomplete_block(cli.prog, shell)
        if not body:
            continue
        lines.extend(body)
        covered.append(cli.prog)
    lines.append(_NON_LIQUIFAI_END)
    return "\n".join(lines) + "\n", covered


def _strip_block(text: str, start: str, end: str) -> str:
    """Drop a marker-delimited block. Absent markers leave ``text`` untouched."""
    if start not in text or end not in text:
        return text
    head = text[: text.index(start)]
    tail = text[text.index(end) + len(end) :]
    return head + tail.lstrip("\n")


def _installed_liquifai_progs(text: str) -> List[str]:
    """Every program with a liquifai block in ``text``, in file order."""
    progs = []
    for line in text.splitlines():
        if line.startswith(_LIQUIFAI_BLOCK_PREFIX) and line.endswith(" >>>"):
            progs.append(line[len(_LIQUIFAI_BLOCK_PREFIX) : -len(" >>>")].strip())
    return progs


def _prune_stale(rc: Path) -> List[str]:
    """Drop liquifai blocks whose program is gone (a rename, an uninstall).

    liquifai's installer merges by marker and leaves blocks it did not write alone, so
    without this a removed app keeps a dead block forever.
    """
    text = rc.read_text() if rc.exists() else ""
    pruned = []
    for prog in _installed_liquifai_progs(text):
        if _program_exists(prog):
            continue
        text = _strip_block(text, f"{_LIQUIFAI_BLOCK_PREFIX}{prog} >>>", f"# <<< liquifai completion for {prog} <<<")
        pruned.append(prog)
    if pruned:
        rc.write_text(text)
    return pruned


def install_completions(
    target_rc: Optional[Path] = None,
    shell: Optional[str] = None,
    apps: Optional[Sequence[str]] = None,
    prune: bool = True,
) -> CompletionReport:
    """Install completion for every workspace CLI into ``target_rc``. Idempotent.

    Args:
        target_rc: Where to write. Defaults to the workspace ``.project.bashrc.completion``,
            which ``project.bashrc`` sources — pass ``~/.bashrc`` (or ``project.bashrc``
            itself) to install somewhere else.
        shell: ``bash`` / ``zsh`` / ``fish``. Defaults to liquifai's ``$SHELL`` detection.
        apps: liquifai apps to install. ``None`` auto-discovers every one in the active venv.
        prune: Drop blocks for programs that no longer exist. Skipped when NO liquifai app
            was found, since that means discovery is broken rather than every app removed —
            pruning then would empty the file.
    """
    from liquifai.completion import detect_shell, install_for_apps

    rc = Path(target_rc).expanduser() if target_rc is not None else default_target_rc()
    shell = shell or detect_shell()
    rc.parent.mkdir(parents=True, exist_ok=True)

    installed = list(install_for_apps(target_rc=rc, apps=apps, shell=shell))

    pruned = _prune_stale(rc) if (prune and installed) else []

    block, covered = _render_non_liquifai(shell)
    text = _strip_block(rc.read_text() if rc.exists() else "", _NON_LIQUIFAI_START, _NON_LIQUIFAI_END)
    if text and not text.endswith("\n"):
        text += "\n"
    rc.write_text(text + block)

    return CompletionReport(rc=rc, shell=shell, liquifai_apps=installed, non_liquifai=covered, pruned=pruned)


def shell_snippet(rc: Path) -> str:
    """The line a user adds to their own rc to pick the fragment up."""
    home = str(Path.home())
    shown = f"$HOME{str(rc)[len(home):]}" if str(rc).startswith(home) else str(rc)
    return f'[ -f "{shown}" ] && source "{shown}"'


def is_sourced_by(rc: Path, candidate: Path) -> bool:
    """Does ``candidate`` already source ``rc``? Used to decide whether to print the hint."""
    if not candidate.exists():
        return False
    try:
        text = candidate.read_text()
    except OSError:
        return False
    return rc.name in text


def verify_click_completion(prog: str = "aisland") -> bool:
    """Does ``prog`` actually emit a completion script? A cheap post-install smoke check.

    Cheap enough to run every install and worth it: the block is an ``eval`` of this output,
    so an app that emits nothing installs a silently inert block — exactly the failure this
    whole module exists to make visible.
    """
    var = f"_{prog.replace('-', '_').upper()}_COMPLETE"
    exe = Path(sys.prefix) / "bin" / prog
    if not exe.exists():
        return False
    try:
        out = subprocess.run(
            [str(exe)],
            env={**os.environ, var: "bash_source"},
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and "complete" in out.stdout
