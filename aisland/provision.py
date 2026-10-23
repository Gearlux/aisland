"""The system tools a workspace needs: ``aisland workspace provision [install]``, and the step of ``aisland setup``.

A project declares the tools it needs from outside Python in its own ``pyproject.toml``, read by
:mod:`aisland.discovery` (which refuses a declaration it cannot use, naming the file and the line)::

    [tool.aisland.sys-dependencies]
    brew = ["node"]          # macOS, installed through Homebrew
    userspace = ["node"]     # Linux, from aisland's own download list (manager.USERSPACE_TOOLS)

aisland's own tools are :data:`AISLAND_TOOLS`, in the package rather than in aisland's ``pyproject.toml``: a workspace
whose aisland came from a wheel has no such file to read, and would get no service supervisor.

On macOS ``install`` runs Homebrew for every ``brew`` tool, then installs the Java the Jenkins WAR accepts; on Linux it
downloads every ``userspace`` tool into aisland's ``bin/``; elsewhere it installs nothing and says so.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import click

from aisland import discovery, manager
from aisland.discovery import ToolKind
from aisland.manager import BIN_DIR, USERSPACE_TOOLS

#: aisland's own tools, by how they are installed:
#:
#: - ``f1bonacc1/tap/process-compose``: the service supervisor (``aisland/supervisor.py``), tap-qualified because
#:   homebrew-core has no process-compose formula; Linux gets it from :data:`aisland.manager.USERSPACE_TOOLS`.
#: - ``docker`` and ``colima``: the container engine the Docker-run services use; ``act``: running GitHub's CI locally;
#:   ``swiftbar``: the menu-bar status of the services.
#: - No ``openjdk``: that formula follows the newest Java, which Jenkins refuses — the Java the Jenkins WAR names is
#:   installed instead (``aisland/services/jenkins_java.py``).
AISLAND_TOOLS: Dict[ToolKind, Tuple[str, ...]] = {
    "brew": ("swiftbar", "act", "docker", "colima", "f1bonacc1/tap/process-compose"),
    "userspace": ("process-compose",),
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


def tools(root: Path) -> List[Tool]:
    """The tools this machine needs, by name — aisland's own and every checked-out project's, each once.

    Raises :class:`aisland.discovery.DeclarationError` for a declaration aisland cannot use."""
    kind = this_platform()
    if kind is None:
        return []
    needed: Dict[str, List[str]] = {name: ["aisland"] for name in AISLAND_TOOLS[kind]}
    for tool in discovery.discover(root).system_tools:
        if tool.kind == kind and tool.project not in needed.setdefault(tool.name, []):
            needed[tool.name].append(tool.project)
    return [Tool(name, kind, tuple(projects)) for name, projects in sorted(needed.items())]


def _brew_installed() -> Set[str]:
    """The formulae and casks Homebrew has installed (empty without Homebrew)."""
    names: Set[str] = set()
    for kind in ("--formula", "--cask"):
        try:
            listed = subprocess.run(["brew", "list", kind, "-1"], capture_output=True, text=True)
        except FileNotFoundError:
            return set()
        names.update(listed.stdout.split())
    return names


def _installed(tool: Tool, brewed: Set[str]) -> bool:
    if tool.kind == "brew":
        return tool.name.rsplit("/", 1)[-1] in brewed  # a tap-qualified formula is listed by its own name
    return all((BIN_DIR / binary).exists() for binary in USERSPACE_TOOLS[tool.name]["binaries"])


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


def install(root: Path) -> None:
    """Install every tool this machine needs (``aisland workspace provision install`` and ``aisland setup``)."""
    click.echo(f"==> Installing the system tools the workspace needs ({root})...")
    needed = tools(root)
    kind = this_platform()
    if kind is None:
        click.echo(f"  - {nothing_here()}")
        return
    names = {tool.name for tool in needed}
    if kind == "brew":
        click.echo(f"  - Homebrew: {', '.join(sorted(names))}")
        manager._install_brew_packages(names)
        manager._maybe_offer_shell_switch(names)
        # Jenkins' Java is no declared formula: the WAR names the Java it runs on (services/jenkins_java.py).
        # Imported here because services.jenkins imports aisland.manager, which imports this module lazily.
        from aisland.services.jenkins import JenkinsService

        JenkinsService().provision_java()
        return
    click.echo(f"  - aisland's downloads: {', '.join(sorted(names))}")
    for name in sorted(names):
        manager._install_userspace_tool(name)
