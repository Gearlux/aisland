"""aisland names no domain project, private project or company: its code, tests and docs illustrate with a general
project (`app`, `acme`).

aisland serves every domain and runs from a wheel in any workspace, where one domain's projects mean nothing, and it is
published, where a private project's or a company's name must not appear. The names are kept as digests
(:mod:`tests.private_names`), so this file never spells them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import FrozenSet, Iterable, List

from tests.private_names import DOMAIN, PRIVATE, words_named

PROJECT = Path(__file__).resolve().parents[1]


def _project_files() -> List[Path]:
    """Every file git tracks in aisland, and every new one it does not ignore (data/ and logs/ are ignored)."""
    listed = subprocess.run(
        ["git", "-C", str(PROJECT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [PROJECT / name for name in listed if (PROJECT / name).is_file()]


def _hits(paths: Iterable[Path], digests: FrozenSet[str]) -> List[str]:
    """``file:line: the words`` for each line of ``paths`` naming one of ``digests``."""
    hits = []
    for path in paths:
        if path.suffix in (".pyc", ".png", ".zip", ".gz", ".whl"):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            named = words_named(line, digests)
            if named:
                hits.append(f"{path.relative_to(PROJECT)}:{number}: {', '.join(named)}")
    return hits


def test_no_file_of_aisland_names_a_domain_project() -> None:
    hits = _hits(_project_files(), DOMAIN)
    assert not hits, "aisland names a domain project — use a general project (`app`, `acme`) instead:\n" + "\n".join(
        hits
    )


#: The address a fallback once hard-coded.
_OWNER_ADDRESS = re.compile("gearlux@", re.IGNORECASE)


def test_no_file_of_the_package_names_a_private_project_or_a_company() -> None:
    """What the wheel ships — code, comments, help, templates — goes to PyPI, read by anyone."""
    package = [path for path in _project_files() if path.relative_to(PROJECT).parts[0] == "aisland"]
    hits = _hits(package, PRIVATE) + [
        f"{path.relative_to(PROJECT)}:{number}: {line.strip()}"
        for path in package
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1)
        if _OWNER_ADDRESS.search(line)
    ]
    assert not hits, "the published package names a private project or a company:\n" + "\n".join(hits)


def test_no_published_file_names_a_private_project() -> None:
    """The README, CHANGELOG, contributor rules, tasks, docs, examples and tests are read by anyone who opens the
    repository: they illustrate with `app` / `acme`."""
    top = ("README.md", "CHANGELOG.md", "AGENTS.md", "TASKS.md", "tasks.json")
    published = [PROJECT / name for name in top if (PROJECT / name).is_file()]
    published += [
        path for path in _project_files() if path.relative_to(PROJECT).parts[0] in ("docs", "examples", "tests")
    ]
    hits = _hits(published, PRIVATE)
    assert not hits, "a published file names a private project — use `app` / `acme` instead:\n" + "\n".join(hits)


def test_no_file_of_the_package_carries_a_users_dated_ruling() -> None:
    """The wheel runs in other workspaces: a "user ruling 2026-…" is one person's decision in the workspace aisland is
    developed in. A comment states the reason; the decision's date stays in `docs/architecture.md`."""
    dated = re.compile(r"\buser (ruling|request|choice|pick|correction|decision)s?:? \d", re.IGNORECASE)
    package = [path for path in _project_files() if path.relative_to(PROJECT).parts[0] == "aisland"]
    hits = [
        f"{path.relative_to(PROJECT)}:{number}: {line.strip()}"
        for path in package
        if path.suffix in (".py", ".md", ".toml", ".jinja", ".sh", ".json", ".template")
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1)
        if dated.search(line)
    ]
    assert not hits, "the package carries a dated ruling of one workspace:\n" + "\n".join(hits)
