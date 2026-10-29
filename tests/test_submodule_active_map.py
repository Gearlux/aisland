"""`projects.submodule_active_map`: every submodule `.gitmodules` declares, with its path and whether it is active.

Every `aisland` command reads it (discovery lists the projects through `workspace_folders`), so it is read with ONE
`git config` process, never one per submodule: a workspace of 35 submodules spent 13.9 s in 36 of them on a loaded
machine, for every command.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from aisland import projects

GITMODULES = """\
[submodule "loggair"]
\tpath = loggair
\turl = https://example.com/loggair.git
[submodule "acme-tools"]
\tpath = tools/acme
\turl = https://example.com/acme.git
\tactive = false
[submodule "models.v2"]
\tpath = models
\turl = https://example.com/models.git
\tactive = true
[submodule "viewer"]
\tpath = viewer
\turl = https://example.com/viewer.git
\tactive = FALSE
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".gitmodules").write_text(GITMODULES)
    return tmp_path


def test_every_submodule_in_file_order_with_its_path_and_whether_it_is_active(root: Path) -> None:
    assert projects.submodule_active_map(root) == [
        ("loggair", "loggair", True),  # no `active` key: git's default, active
        ("acme-tools", "tools/acme", False),
        ("models.v2", "models", True),  # a name holding a dot
        ("viewer", "viewer", False),  # the value is read case-blind
    ]


def test_the_map_is_read_with_one_git_process(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: List[Any] = []
    real_run = subprocess.run

    def counting_run(*args: Any, **kwargs: Any) -> Any:
        started.append(args[0])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    assert len(projects.submodule_active_map(root)) == 4
    assert len(started) == 1, started


def test_a_workspace_without_gitmodules_has_no_submodules(tmp_path: Path) -> None:
    assert projects.submodule_active_map(tmp_path) == []
