"""The system tools a workspace needs: ``aisland workspace provision [install]``, and the step of ``aisland setup``.

A project declares the tools it needs from outside Python in its own ``pyproject.toml``, read by
:mod:`aisland.workspace.discovery` (which refuses a declaration it cannot use, naming the file and the line)::

    [tool.aisland.sys-dependencies]
    brew = ["node"]          # macOS, installed through Homebrew
    userspace = ["node"]     # Linux, from aisland's own download list (installers.USERSPACE_TOOLS)

aisland's own tools are :data:`AISLAND_TOOLS`, in the package rather than in aisland's ``pyproject.toml``: a workspace
whose aisland came from a wheel has no such file to read, and would get no service supervisor.

On macOS ``install`` runs Homebrew for every ``brew`` tool, then installs the Java the Jenkins WAR accepts; on Linux it
downloads every ``userspace`` tool into aisland's ``bin/``; elsewhere it installs nothing and says so.

``aisland setup`` installs only what the PROJECTS declare (``install(root, aislands_own=False)``). aisland's own tools
are installed when a feature first needs one (:func:`ask_to_install`): in a terminal the user is asked, and without
one — or on a "no" — the command refuses with the line that installs it, having installed nothing.
``aisland workspace provision install``, which the user asks for by name, still installs everything.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from aisland import console, paths, probe
from aisland.errors import AislandError
from aisland.setup import installers
from aisland.userspace_tools import USERSPACE_TOOLS
from aisland.workspace import discovery
from aisland.workspace.discovery import ToolKind

#: aisland's own tools, by how they are installed:
#:
#: - ``f1bonacc1/tap/process-compose``: the service supervisor (``aisland/services/supervisor.py``), tap-qualified because
#:   homebrew-core has no process-compose formula; Linux gets it from :data:`aisland.setup.installers.USERSPACE_TOOLS`.
#: - ``docker`` and ``colima``: the container engine the Docker-run services use; ``act``: running GitHub's CI locally;
#:   ``swiftbar``: the menu-bar status of the services.
#: - No ``openjdk``: that formula follows the newest Java, which Jenkins refuses — the Java the Jenkins WAR names is
#:   installed instead (``aisland/services/jenkins_java.py``).
AISLAND_TOOLS: Dict[ToolKind, Tuple[str, ...]] = {
    "brew": ("swiftbar", "act", "docker", "colima", "f1bonacc1/tap/process-compose"),
    "userspace": ("process-compose",),
}


#: Who needs each of aisland's own tools, the subject of the question :func:`ask_to_install` asks.
NEEDED_BY: Dict[str, str] = {
    "process-compose": "aisland's services need",
    "colima": "aisland docker needs",
    "docker": "aisland docker needs",
    "swiftbar": "aisland swiftbar needs",
    "act": "aisland check's act gate needs",
}


@dataclass(frozen=True)
class Tool:
    """One tool this machine needs: its name, how it is installed and the projects needing it (``aisland`` first)."""

    name: str
    kind: ToolKind
    projects: Tuple[str, ...]


def this_platform() -> Optional[ToolKind]:
    """How this machine installs system tools: ``brew`` on macOS, ``userspace`` on Linux, ``None`` elsewhere."""
    system = platform.system()
    if system == "Darwin":
        return "brew"
    if system == "Linux":
        return "userspace"
    return None


def tools(root: Path, aislands_own: bool = True) -> List[Tool]:
    """The tools this machine needs, by name — aisland's own (unless ``aislands_own`` is False) and every
    checked-out project's, each once.

    Raises :class:`aisland.workspace.discovery.DeclarationError` for a declaration aisland cannot use."""
    kind = this_platform()
    if kind is None:
        return []
    needed: Dict[str, List[str]] = {name: ["aisland"] for name in AISLAND_TOOLS[kind]} if aislands_own else {}
    for tool in discovery.discover(root).system_tools:
        if tool.kind == kind and tool.project not in needed.setdefault(tool.name, []):
            needed[tool.name].append(tool.project)
    return [Tool(name, kind, tuple(projects)) for name, projects in sorted(needed.items())]


def _brew_installed() -> Set[str]:
    """The formulae and casks Homebrew has installed (empty without Homebrew)."""
    names: Set[str] = set()
    for kind in ("--formula", "--cask"):
        listed = probe.run(["brew", "list", kind, "-1"])
        if listed is None:
            return set()
        names.update(listed.stdout.split())
    return names


def _installed(tool: Tool, brewed: Set[str]) -> bool:
    if tool.kind == "brew":
        return tool.name.rsplit("/", 1)[-1] in brewed  # a tap-qualified formula is listed by its own name
    return all((paths.bin_dir() / binary).exists() for binary in USERSPACE_TOOLS[tool.name]["binaries"])


def rows(root: Path) -> List[Dict[str, str]]:
    """``aisland workspace provision``: one row per tool — how it is installed, who needs it, whether it is there."""
    needed = tools(root)
    brewed = _brew_installed() if any(tool.kind == "brew" for tool in needed) else set()
    return [
        {
            "tool": tool.name,
            "how": tool.kind,
            "projects": ", ".join(tool.projects),
            "installed": "yes" if _installed(tool, brewed) else "no",
        }
        for tool in needed
    ]


def nothing_here() -> str:
    """What a machine aisland installs nothing on is told."""
    return f"aisland installs no system tools on {platform.system()}."


def install(root: Path, aislands_own: bool = True) -> None:
    """Install every tool this machine needs: ``aisland workspace provision install`` (everything) and ``aisland
    setup`` (``aislands_own=False``: the projects' declarations only — aisland's own come when a feature needs one).
    Jenkins' Java is no tool of this list (its WAR names the Java it runs on): the provision command installs it after
    this, through :meth:`aisland.services.jenkins.JenkinsService.provision_java`."""
    console.echo(f"==> Installing the system tools the workspace needs ({root})...")
    needed = tools(root, aislands_own)
    kind = this_platform()
    if kind is None:
        console.echo(f"  - {nothing_here()}")
        return
    names = {tool.name for tool in needed}
    if not names:
        console.echo("  - none: no project declares a system tool")
        return
    if kind == "brew":
        console.echo(f"  - Homebrew: {', '.join(sorted(names))}")
        installers.install_brew_packages(names)
        installers.offer_shell_switch(names)
        return
    console.echo(f"  - aisland's downloads: {', '.join(sorted(names))}")
    for name in sorted(names):
        installers.install_userspace_tool(name)


def _can_ask() -> bool:
    """Whether a person is there to answer: both standard input and output are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def agree(question: str, default: bool = True) -> bool:
    """``question`` asked in a terminal (``default`` the answer a bare Enter gives); False, without asking, when no
    one is there to answer."""
    return _can_ask() and console.confirm(question, default=default)


def _formula(name: str) -> str:
    """The Homebrew formula of aisland's tool ``name`` (``process-compose`` -> ``f1bonacc1/tap/process-compose``)."""
    return next((formula for formula in AISLAND_TOOLS["brew"] if formula.rsplit("/", 1)[-1] == name), name)


def install_command(name: str) -> str:
    """The line that installs aisland's tool ``name`` on this machine."""
    kind = this_platform()
    if kind == "brew":
        return f"brew install {_formula(name)}"
    if kind == "userspace" and name in AISLAND_TOOLS["userspace"]:
        return "aisland workspace provision install"
    return f"install {name} with this system's package manager"


def ask_to_install(name: str) -> None:
    """A feature found aisland's tool ``name`` missing: install it once the user agrees, else refuse.

    The refusal names the line that installs it, and nothing is installed — without a terminal no one is asked (a
    script or CI never installs software as a side effect). A tool aisland cannot install here is refused at once.
    """
    kind = this_platform()
    missing = f"{NEEDED_BY[name]} {name}, which is not installed."
    if kind == "brew":
        how: Optional[str] = install_command(name)
    elif kind == "userspace" and name in AISLAND_TOOLS["userspace"]:
        how = "aisland downloads it into its bin folder"
    else:
        how = None
    if how is None or not agree(f"{missing} Install it ({how})?"):
        raise AislandError(f"{missing} Install it: {install_command(name)}")
    if kind == "brew":
        installers.install_brew_packages({_formula(name)})
    else:
        installers.install_userspace_tool(name)
