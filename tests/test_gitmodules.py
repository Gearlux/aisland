"""``aisland.workspace.gitmodules``: every submodule's keys from ONE ``git config`` process, and the three readers that are
views over it (``projects.submodule_active_map``, ``workspace_file.submodules``, ``local_workspace.submodule_names``).
"""

import subprocess
from pathlib import Path
from typing import Any, List

import pytest

from aisland.workspace import file as workspace_file
from aisland.workspace import gitmodules
from aisland.workspace import local as local_workspace
from aisland.workspace import projects
from aisland.workspace.gitmodules import Submodule

GITMODULES = """\
[submodule "loggair"]
\tpath = loggair
\turl = https://example.com/loggair.git
\tbranch = main
[submodule "acme-tools"]
\tpath = tools/acme
\turl = https://example.com/acme.git
\tactive = false
[submodule "models.v2"]
\tpath = models
\tactive = true
[submodule "viewer"]
\turl = https://example.com/viewer.git
\tpath = viewer
\tactive = FALSE
[submodule "no-path"]
\turl = https://example.com/nowhere.git
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / gitmodules.FILE_NAME).write_text(GITMODULES)
    return tmp_path


def test_every_submodule_with_a_path_in_file_order_with_its_keys(root: Path) -> None:
    assert gitmodules.read(root) == [
        Submodule("loggair", "loggair", "https://example.com/loggair.git", "main", True),
        Submodule("acme-tools", "tools/acme", "https://example.com/acme.git", None, False),
        Submodule("models.v2", "models", None, None, True),  # a name holding a dot; no url line
        Submodule("viewer", "viewer", "https://example.com/viewer.git", None, False),  # active read case-blind
    ]


def test_the_keys_are_read_with_one_git_process(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: List[Any] = []
    real_run = subprocess.run

    def counting_run(*args: Any, **kwargs: Any) -> Any:
        started.append(args[0])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    assert len(gitmodules.read(root)) == 4
    assert len(started) == 1, started


def test_without_the_file_there_are_no_submodules_and_no_git_is_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("git was run"))
    assert gitmodules.read(tmp_path) == []
    (tmp_path / gitmodules.FILE_NAME).mkdir()  # a folder by that name is no file either
    assert gitmodules.read(tmp_path) == []


def test_a_file_git_cannot_read_has_no_submodules(tmp_path: Path) -> None:
    (tmp_path / gitmodules.FILE_NAME).write_text('[submodule "x"\n\tpath = x\n')
    assert gitmodules.read(tmp_path) == []


def test_a_key_written_twice_is_read_as_git_reads_it(tmp_path: Path) -> None:
    (tmp_path / gitmodules.FILE_NAME).write_text(
        '[submodule "a"]\n\tpath = first\n\turl = one\n'
        '[submodule "b"]\n\tpath = b\n'
        '[submodule "a"]\n\tpath = second\n\turl = two\n\tactive = false\n\tactive = true\n'
    )
    got = gitmodules.read(tmp_path)
    assert got == [Submodule("a", "second", "two", None, True), Submodule("b", "b", None, None, True)]
    for key, value in (("path", "second"), ("url", "two"), ("active", "true")):
        answer = subprocess.run(
            ["git", "config", "-f", gitmodules.FILE_NAME, "--get", f"submodule.a.{key}"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert answer.stdout.strip() == value


def test_the_three_readers_are_views_of_it(root: Path) -> None:
    assert projects.submodule_active_map(root) == [
        ("loggair", "loggair", True),
        ("acme-tools", "tools/acme", False),
        ("models.v2", "models", True),
        ("viewer", "viewer", False),
    ]
    assert workspace_file.submodules(root) == [
        ("loggair", "https://example.com/loggair.git", "main"),
        ("tools/acme", "https://example.com/acme.git", None),
        ("models", "", None),  # no url line: an empty url, as before
        ("viewer", "https://example.com/viewer.git", None),
    ]
    assert local_workspace.submodule_names(root) == {
        "loggair": "loggair",
        "tools/acme": "acme-tools",
        "models": "models.v2",
        "viewer": "viewer",
    }


def test_each_view_runs_one_git_process(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started: List[Any] = []
    real_run = subprocess.run

    def counting_run(*args: Any, **kwargs: Any) -> Any:
        started.append(args[0])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    for view in (projects.submodule_active_map, workspace_file.submodules, local_workspace.submodule_names):
        started.clear()
        assert len(view(root)) == 4
        assert len(started) == 1, (view.__name__, started)
