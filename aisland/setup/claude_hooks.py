"""Claude Code's hook entries for workspace worktrees, in the workspace's ``.claude/settings.json``.

In a workspace with a root project (:func:`aisland.locations.rooted`) setup writes three. ``WorktreeCreate`` and
``WorktreeRemove`` run ``aisland workspace worktree create|remove --hook`` (:mod:`aisland.setup.worktrees`) in place of
Claude Code's ``git worktree`` — of the root's repository, which records no project, it would hold no code —, and
``SessionStart`` runs ``aisland workspace worktree env --hook``, which puts a worktree's projects first on the session's
``PYTHONPATH``. A desktop session in a worktree reads the workspace's settings and a CLI one the worktree's, which
links to this same file.

Only aisland's own entries are written or taken away: a matcher group whose one hook runs
``aisland workspace worktree <verb> --hook``. Every other key and hook of the file is the person's and kept as it is,
and a file that is no JSON object aisland can read is left untouched and named. In a workspace without a root project
aisland's entries are taken away.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from aisland import atomic, locations
from aisland.venv import venv_bin

#: The file, relative to the workspace root.
SETTINGS = Path(".claude") / "settings.json"
#: Each hook event aisland writes, with the ``aisland workspace worktree`` verb it runs.
VERBS: Dict[str, str] = {"WorktreeCreate": "create", "WorktreeRemove": "remove", "SessionStart": "env"}
_OURS = re.compile(r'aisland"? workspace worktree (create|remove|env) --hook$')


def command(root: Path, verb: str) -> str:
    """The hook command for ``verb``: the workspace venv's ``aisland``, by its full path (a hook runs without it)."""
    return f'"{venv_bin(root) / "aisland"}" workspace worktree {verb} --hook'


def _ours(group: object) -> bool:
    """Whether a matcher group of the file is aisland's: its one hook runs ``aisland workspace worktree … --hook``."""
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        return False
    run = hooks[0].get("command")
    return isinstance(run, str) and _OURS.search(run) is not None


def _read(path: Path) -> Optional[Dict[str, object]]:
    try:
        data: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        return None
    return {str(key): value for key, value in data.items()}


def write_hooks(root: Path) -> List[str]:
    """Bring aisland's entries in ``<root>/.claude/settings.json`` in line (the module docstring); one line per change,
    none when the file already says it."""
    path = root / SETTINGS
    wanted = locations.rooted(root)
    if not path.exists():
        if not wanted:
            return []
        data: Dict[str, object] = {}
    else:
        read = _read(path)
        if read is None:
            return [f"{SETTINGS} is no JSON aisland can read: the worktree hooks are not written there"]
        data = read
    before = json.dumps(data, sort_keys=True)
    old = data.get("hooks", {})
    hooks: Dict[str, object] = dict(old) if isinstance(old, dict) else {}
    for event, verb in VERBS.items():
        groups = hooks.get(event, [])
        kept = [group for group in groups if not _ours(group)] if isinstance(groups, list) else groups
        if wanted and isinstance(kept, list):
            kept = [*kept, {"hooks": [{"type": "command", "command": command(root, verb)}]}]
        if kept == []:
            hooks.pop(event, None)
        else:
            hooks[event] = kept
    if hooks:
        data["hooks"] = hooks
    elif old:
        data.pop("hooks", None)  # the last entries were aisland's
    if json.dumps(data, sort_keys=True) == before:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, json.dumps(data, indent=2) + "\n")
    if wanted:
        return [f"{SETTINGS}: the worktree hooks"]
    return [f"{SETTINGS}: the worktree hooks taken away (no root project)"]
