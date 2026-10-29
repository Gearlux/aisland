"""aisland names neither bridge nor signals: its code, tests and docs illustrate with a general project.

aisland serves every domain and runs from a wheel in any workspace, where the signal domain (signals) and a
private bridge (bridge) mean nothing. Root ``AGENTS.md`` → "General Projects Name Neither Bridge Nor Signals"
(user ruling 2026-10-19).
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Iterable, List, Set

PROJECT = Path(__file__).resolve().parents[1]
FORBIDDEN = re.compile("bridge|signals", re.IGNORECASE)


def _project_files() -> List[Path]:
    """Every file git tracks in aisland, and every new one it does not ignore (data/ and logs/ are ignored)."""
    listed = subprocess.run(
        ["git", "-C", str(PROJECT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [PROJECT / name for name in listed if (PROJECT / name).is_file()]


def test_no_file_of_aisland_names_bridge_or_signals() -> None:
    this_file = Path(__file__).resolve()
    hits = []
    for path in _project_files():
        if path.resolve() == this_file:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if FORBIDDEN.search(line):
                hits.append(f"{path.relative_to(PROJECT)}:{number}: {line.strip()}")
    assert (
        not hits
    ), "aisland names bridge or signals — use a general project (`app`, `acme`) instead:\n" + "\n".join(hits)


#: The private project, the platform and the company names the published package must not carry (root `AGENTS.md`:
#: "The private and company names no project's user docs carry"), and the address a fallback once hard-coded.
PRIVATE_NAMES = ("company", "vendorcli", "platform", "acme")
PRIVATE = re.compile("|".join(PRIVATE_NAMES) + "|gearlux@", re.IGNORECASE)


def test_no_file_of_the_package_names_a_private_project_or_a_company() -> None:
    """What the wheel ships — code, comments, help, templates — goes to PyPI, read by anyone."""
    hits = [
        f"{path.relative_to(PROJECT)}:{number}: {line.strip()}"
        for path in _project_files()
        if path.relative_to(PROJECT).parts[0] == "aisland"
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1)
        if PRIVATE.search(line)
    ]
    assert not hits, "the published package names a private project or a company:\n" + "\n".join(hits)


def _assignment_lines(path: Path, names: Iterable[str]) -> Set[int]:
    """The line numbers of the module-level assignments to ``names`` in ``path``, found by parsing."""
    wanted = set(names)
    lines: Set[int] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id in wanted for t in targets):
                lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


def test_no_published_file_names_a_private_project() -> None:
    """The README, CHANGELOG, tasks, docs, examples and tests are read by anyone: they illustrate with `app` / `acme`."""
    exempt = {
        (PROJECT / "tests" / "test_no_private_names.py").resolve(): _assignment_lines(
            Path(__file__), ("PRIVATE_NAMES", "PRIVATE")
        ),
        (PROJECT / "tests" / "test_docs.py").resolve(): _assignment_lines(
            PROJECT / "tests" / "test_docs.py", ("WORKSPACE_NAMES",)
        ),
    }
    pattern = re.compile("|".join(PRIVATE_NAMES), re.IGNORECASE)
    published = [PROJECT / name for name in ("README.md", "CHANGELOG.md", "tasks.json")]
    published += [
        path for path in _project_files() if path.relative_to(PROJECT).parts[0] in ("docs", "examples", "tests")
    ]
    hits = []
    for path in published:
        if not path.is_file() or path.suffix in (".pyc", ".png", ".zip", ".gz"):
            continue
        skipped = exempt.get(path.resolve(), set())
        text = path.read_text(encoding="utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if number not in skipped and pattern.search(line):
                hits.append(f"{path.relative_to(PROJECT)}:{number}: {line.strip()}")
    assert not hits, "a published file names a private project — use `app` / `acme` instead:\n" + "\n".join(hits)
