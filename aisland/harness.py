"""The agent harness: the workspace commands and the generic agent rules, installed into every aisland workspace.

aisland ships, in ``aisland/harness/``:

* ``commands/*.md`` — the workspace slash commands (``/commit-session``, ``/ship``, ...), installed into
  ``.agents/workflows/``; ``.claude/commands`` is a link to that folder — at the root and in every project folder,
  as Claude Code reads only the folder a session starts in — so Claude Code finds them;
* ``scripts/*.py`` — the helpers a command runs (``stage_hunks.py`` for ``/commit-session``), into
  ``.agents/scripts/``;
* ``AGENTS.md`` — the generic agent rules, into ``.agents/aisland/AGENTS.md``. The workspace's own ``AGENTS.md``
  imports it with its first line, ``@.agents/aisland/AGENTS.md`` (Claude Code and Gemini CLI follow ``@`` imports),
  and keeps only the rules of that workspace below it.

How a file is installed follows how aisland is (:data:`aisland.locations.FROM_CHECKOUT`): from a checkout each file
is a relative LINK into the package, so an edit made through the workspace is an edit of aisland's file, committed
in aisland; from a wheel it is a COPY. A copy is replaced on the next install only while it is still the copy aisland
wrote — :data:`RECORD` holds each copy's fingerprint —, so a copy someone edited is kept and named, never
overwritten. A file under a shipped name that aisland never wrote is kept the same way, and any other file in those
folders (a command of the workspace's own) is never touched.

The installed files are per machine: each is listed in the root ``.gitignore`` by its own path, so a command of the
workspace's own in ``.agents/workflows/`` can still be committed. A workspace without an ``AGENTS.md`` gets one that
imports the rules, with ``CLAUDE.md`` and ``GEMINI.md`` linking to it; an existing ``AGENTS.md`` is never edited —
the report names the missing import line instead.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from aisland import locations

#: aisland's harness, in the package (a wheel carries it: ``[tool.setuptools.package-data]``).
HARNESS_DIR = locations.PACKAGE_DIR / "harness"
#: Where the workspace's agents read the commands, the helper scripts and the rules.
WORKFLOWS = ".agents/workflows"
SCRIPTS = ".agents/scripts"
RULES = ".agents/aisland/AGENTS.md"
#: The fingerprint of every copy aisland wrote, so an edited copy is told apart from aisland's.
RECORD = ".agents/aisland/installed.json"
#: The first line of a workspace ``AGENTS.md`` that takes the generic rules.
IMPORT_LINE = f"@{RULES}"

_STARTER_AGENTS = f"""{IMPORT_LINE}

# Workspace rules

The line above imports aisland's generic agent rules (`aisland setup` installs `{RULES}`; change them in aisland,
never in the installed file). This workspace's own rules go below; a rule for one project goes in that project's
`AGENTS.md`. `CLAUDE.md` and `GEMINI.md` link to this file.
"""


def shipped_files() -> List[Tuple[Path, str]]:
    """Each file aisland ships with the workspace path it is installed at, commands first, then scripts, then rules."""
    pairs = [(path, f"{WORKFLOWS}/{path.name}") for path in sorted((HARNESS_DIR / "commands").glob("*.md"))]
    pairs += [(path, f"{SCRIPTS}/{path.name}") for path in sorted((HARNESS_DIR / "scripts").glob("*.py"))]
    pairs.append((HARNESS_DIR / "AGENTS.md", RULES))
    return pairs


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_record(root: Path) -> Dict[str, str]:
    record = root / RECORD
    if not record.is_file():
        return {}
    try:
        data = json.loads(record.read_text())
    except (OSError, ValueError):
        return {}  # an unreadable record only means no copy counts as aisland's: each is kept, none lost
    return {str(name): str(value) for name, value in data.items()} if isinstance(data, dict) else {}


def _write_record(root: Path, record: Dict[str, str]) -> None:
    path = root / RECORD
    if not record:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(record.items())), indent=2) + "\n")


def _is_aislands(target: Path, name: str, source: Path, record: Dict[str, str]) -> bool:
    """Whether ``target`` may be replaced: a link (aisland's or a stale one), a copy aisland wrote, or a file that
    already equals aisland's."""
    if target.is_symlink():
        return True
    fingerprint = _fingerprint(target)
    return fingerprint == record.get(name) or (source.is_file() and fingerprint == _fingerprint(source))


def _install_one(root: Path, source: Path, name: str, link: bool, record: Dict[str, str]) -> Optional[str]:
    """Install one shipped file; returns the line to report, or None when there is nothing to say."""
    target = root / name
    relative = os.path.relpath(source, target.parent)
    if link and target.is_symlink() and os.readlink(target) == relative:
        record.pop(name, None)
        return None
    if not link and target.is_file() and not target.is_symlink() and _fingerprint(target) == _fingerprint(source):
        record[name] = _fingerprint(target)
        return None
    if os.path.lexists(target):
        if not _is_aislands(target, name, source, record):
            return f"kept your edited {name} — delete it to take aisland's"
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    if link:
        os.symlink(relative, target)
        record.pop(name, None)
    else:
        target.write_bytes(source.read_bytes())
        record[name] = _fingerprint(target)
    return None


def _remove_dropped(root: Path, shipped: List[str], record: Dict[str, str]) -> List[str]:
    """Take away what an earlier aisland installed and this one no longer ships: a link into the harness, or a copy
    still as aisland wrote it. An edited copy stays, named."""
    report = []
    candidates = set(record)
    for folder in (WORKFLOWS, SCRIPTS):
        if (root / folder).is_dir():
            candidates.update(f"{folder}/{path.name}" for path in (root / folder).iterdir() if path.is_symlink())
    for name in sorted(candidates - set(shipped)):
        target = root / name
        if target.is_symlink():
            if Path(os.path.realpath(target)).is_relative_to(os.path.realpath(HARNESS_DIR)):
                target.unlink()
            continue
        if not target.is_file():
            record.pop(name, None)
            continue
        if _fingerprint(target) == record.get(name):
            target.unlink()
        else:
            report.append(f"kept your edited {name} — aisland no longer ships it")
        record.pop(name, None)
    return report


def _link_claude_commands(root: Path, folder: Path) -> Optional[str]:
    """``<folder>/.claude/commands`` → the workspace's ``.agents/workflows``, where Claude Code looks for commands.

    Claude Code reads ``.claude/commands`` of the folder a session starts in, not of the workspace above it (measured
    2026-10-20: started in a project folder it listed none of the workspace's commands), so the root and every project
    folder get the link."""
    commands = folder / ".claude" / "commands"
    wanted = os.path.relpath(root / WORKFLOWS, commands.parent)
    if commands.is_symlink():
        if os.readlink(commands) == wanted:
            return None
        commands.unlink()
    elif commands.exists():
        where = commands.relative_to(root).as_posix()
        return (
            f"{where} is a folder of its own — move its files into .agents/workflows and delete it, "
            "so Claude finds aisland's commands"
        )
    commands.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(wanted, commands)
    return None


def _project_folders(root: Path) -> List[Path]:
    """The workspace's project folders: each direct, non-hidden subfolder holding a ``pyproject.toml`` (a submodule
    whose source is detached has an empty folder and gets nothing)."""
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and not child.name.startswith(".") and (child / "pyproject.toml").is_file()
    )


def _exclude_in_its_repository(path: Path) -> None:
    """Keep ``path`` out of git in the repository that holds it, through that repository's ``.git/info/exclude``: the
    link is per machine, and a line in each project's tracked ``.gitignore`` would change every project."""
    top = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True
    ).stdout.strip()
    exclude = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not top or not exclude:
        return  # in no repository: nothing could commit it
    # The parent resolved, never the link itself: resolving `.claude/commands` would follow it to the workflows.
    line = "/" + (path.parent.resolve() / path.name).relative_to(Path(top).resolve()).as_posix()
    target = Path(exclude)
    content = target.read_text() if target.is_file() else ""
    if line in (entry.strip() for entry in content.splitlines()):
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    sep = "" if (not content or content.endswith("\n")) else "\n"
    target.write_text(content + sep + line + "\n")


def _workspace_agents_md(root: Path) -> Optional[str]:
    """Write ``AGENTS.md`` (and ``CLAUDE.md`` / ``GEMINI.md`` linking to it) when there is none; name a missing import
    line in an existing one, never editing it."""
    agents = root / "AGENTS.md"
    if os.path.lexists(agents):
        lines = agents.read_text().splitlines() if agents.is_file() else []
        if IMPORT_LINE in (line.strip() for line in lines):
            return None
        return f'AGENTS.md does not import the aisland rules — add "{IMPORT_LINE}" as its first line'
    agents.write_text(_STARTER_AGENTS)
    for name in ("CLAUDE.md", "GEMINI.md"):
        if not os.path.lexists(root / name):
            os.symlink("AGENTS.md", root / name)
    return None


def _ignore(root: Path, names: List[str]) -> None:
    """List each installed file in the root ``.gitignore`` by its own path (append-only, each line once)."""
    gitignore = root / ".gitignore"
    content = gitignore.read_text() if gitignore.exists() else ""
    present = {line.strip() for line in content.splitlines()}
    missing = [f"/{name}" for name in names if f"/{name}" not in present]
    if not missing:
        return
    header = "# Installed by `aisland setup` (aisland/harness.py): aisland's agent commands and rules, per machine."
    block = ([header] if header not in present else []) + missing
    sep = "" if (not content or content.endswith("\n")) else "\n"
    gitignore.write_text(content + sep + "\n".join(block) + "\n")


def install_harness(root: Path, link: Optional[bool] = None) -> List[str]:
    """Install aisland's commands, helper scripts and rules into the workspace at ``root``.

    ``link`` chooses links into the package (default: when aisland runs from a checkout) or copies. Returns the lines
    to show the user — a file kept because someone edited it, an ``AGENTS.md`` without the import —, empty when all
    is in place.
    """
    if link is None:
        link = locations.FROM_CHECKOUT
    record = _read_record(root)
    shipped = shipped_files()
    report: List[str] = []
    for source, name in shipped:
        line = _install_one(root, source, name, link, record)
        if line:
            report.append(line)
    report += _remove_dropped(root, [name for _source, name in shipped], record)
    _write_record(root, record)
    for line in (_link_claude_commands(root, root), _workspace_agents_md(root)):
        if line:
            report.append(line)
    for project in _project_folders(root):
        line = _link_claude_commands(root, project)
        if line:
            report.append(line)
        elif (project / ".claude" / "commands").is_symlink():
            _exclude_in_its_repository(project / ".claude" / "commands")
    _ignore(root, [name for _source, name in shipped] + [RECORD])
    return report
