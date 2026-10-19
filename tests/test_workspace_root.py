"""Regression tests: ``get_workspace_root`` must resolve the SUPERPROJECT, not a project.

The 2026-07-26 backlog restructuring made ``TASKS.md`` project-owned, so the old
"walk up to the first TASKS.md" rule started stopping at ``<root>/aisland`` —
which named the Jenkins orchestrator job "aisland" instead of "acme", pointed
the global ``WORKSPACE_ROOT`` at the aisland dir, and made the workspace ``.env``
invisible ("No workspace .env found; skipping injection").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aisland import locations, manager


def _resolve_from(monkeypatch: pytest.MonkeyPatch, module_file: Path) -> Path:
    """Run the checkout's root rule as if manager.py lived at ``module_file``."""
    return locations.checkout_workspace_root(module_file)


def _make_tree(tmp_path: Path, *, gitmodules: bool) -> Path:
    """Build <ws>/aisland/aisland/manager.py with a TASKS.md at BOTH levels."""
    ws = (tmp_path / "acme").resolve()
    pkg = ws / "aisland" / "aisland"
    pkg.mkdir(parents=True)
    (ws / "TASKS.md").write_text("workspace backlog\n")
    (ws / "aisland" / "TASKS.md").write_text("project backlog\n")  # the ambiguity
    if gitmodules:
        (ws / ".gitmodules").write_text('[submodule "confluid"]\n\tpath = confluid\n')
    return pkg / "manager.py"


def test_superproject_wins_over_nested_project_tasks_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The .gitmodules-bearing dir wins even though a nearer TASKS.md exists."""
    module_file = _make_tree(tmp_path, gitmodules=True)
    assert _resolve_from(monkeypatch, module_file) == (tmp_path / "acme").resolve()


def test_project_tasks_md_never_shadows_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact pre-fix failure: <root>/aisland/TASKS.md must NOT be the root."""
    module_file = _make_tree(tmp_path, gitmodules=True)
    assert _resolve_from(monkeypatch, module_file) != (tmp_path / "acme" / "aisland").resolve()


def test_outermost_tasks_md_when_no_submodules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A submodule-less checkout still resolves — outermost TASKS.md, not innermost."""
    module_file = _make_tree(tmp_path, gitmodules=False)
    assert _resolve_from(monkeypatch, module_file) == (tmp_path / "acme").resolve()


def test_outermost_git_when_no_tasks_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No TASKS.md anywhere: fall back to the outermost .git."""
    ws = (tmp_path / "acme").resolve()
    pkg = ws / "aisland" / "aisland"
    pkg.mkdir(parents=True)
    (ws / ".git").mkdir()
    (ws / "aisland" / ".git").write_text("gitdir: ../.git/modules/aisland\n")  # submodule
    assert _resolve_from(monkeypatch, pkg / "manager.py") == ws


def test_live_workspace_root_is_the_superproject() -> None:
    """Unmonkeypatched: the real checkout resolves to the dir holding .gitmodules."""
    root = manager.get_workspace_root()
    assert (root / ".gitmodules").is_file()
    assert (root / "TASKS.md").exists()
    assert root.name != "aisland"
