"""A workspace with one project, and a backup store beside it: what `test_clean.py` and `test_backups.py` build on."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from aisland import backups, clean

NOW = datetime(2026, 8, 4, 12, 0, 0)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def make_project(workspace: Path, name: str = "proj", *, gitignore: str = "build/\n*.log\n") -> Path:
    """A workspace project that is its own git repository, with tracked and ignored content."""
    project = workspace / name
    (project / "build" / "nested").mkdir(parents=True)
    project.joinpath("pyproject.toml").write_text(f'[project]\nname = "{name}"\n')
    project.joinpath(".gitignore").write_text(gitignore)
    project.joinpath("keep.py").write_text("# tracked\n")
    project.joinpath("run.log").write_text("noise\n")
    project.joinpath("build", "a.txt").write_text("a\n")
    project.joinpath("build", "nested", "b.txt").write_text("bb\n")

    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.email", "test@example.com")
    git(project, "config", "user.name", "Test")
    git(project, "add", ".")
    git(project, "commit", "-qm", "initial")
    return project


def make_workspace(root: Path) -> Path:
    """``root`` as a workspace holding one project, `proj`, and an aisland-style data directory."""
    make_project(root)
    (root / "data").mkdir()
    return root


def run_clean(workspace: Path, project: str = "proj", **kwargs: object) -> int:
    """`clean_project` bound to the test workspace, with a frozen clock."""
    kwargs.setdefault("now", NOW)
    return clean.clean_project(
        project,
        workspace_root=workspace,
        data_dir=workspace / "data",
        assume_yes=True,
        **kwargs,  # type: ignore[arg-type]
    )


def tmp_store(workspace: Path) -> Path:
    return backups.backup_root(workspace / "data").resolve()


def stamp(offset_days: float) -> str:
    return (NOW - timedelta(days=offset_days)).strftime(backups.STAMP_FORMAT)


def seed_backup(workspace: Path, project: str, offset_days: float, *, size: int = 1024) -> Path:
    """A pre-existing backup, `offset_days` old, with a manifest recording `size` bytes."""
    path = workspace / "data" / backups.BACKUP_DIRNAME / project / stamp(offset_days)
    (path / backups.PAYLOAD_DIRNAME).mkdir(parents=True)
    (path / backups.MANIFEST_NAME).write_text(json.dumps({"bytes": size}))
    return path
