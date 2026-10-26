"""The VS Code workspace file is generated per machine (`aisland.manager.write_code_workspace`).

Covers what the file holds — the workspace, every project checked out on THIS machine in dependency order,
loggair's log folder when it exists, then the template's settings and extensions — the header's regenerate line,
the commands that rewrite it, and the real workspace keeping it out of git.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import pytest

from aisland import manager
from aisland import sources as s


def _project(root: Path, folder: str, name: Optional[str] = None, deps: Optional[List[str]] = None) -> None:
    (root / folder).mkdir(parents=True, exist_ok=True)
    dependencies = ", ".join(f'"{dep}"' for dep in deps or [])
    (root / folder / "pyproject.toml").write_text(
        f'[project]\nname = "{name or folder}"\nversion = "0.1.0"\ndependencies = [{dependencies}]\n'
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`acme/` holding loggair <- confluid, aisland and an empty `vendorcli/`."""
    root = tmp_path / "acme"
    _project(root, "loggair")
    _project(root, "confluid", deps=["loggair>=0.2.0"])
    _project(root, "aisland", deps=["click>=8.0.0"])
    (root / "vendorcli").mkdir()  # an inactive submodule: its folder is empty
    monkeypatch.setattr(manager, "_loggair_log_dir", lambda workspace_root: None)
    return root


def _read(root: Path) -> Dict[str, Any]:
    """The written file, parsed: everything after the `//` header is JSON."""
    text = manager.code_workspace_path(root).read_text()
    document: Dict[str, Any] = json.loads("\n".join(line for line in text.splitlines() if not line.startswith("//")))
    return document


def _paths(root: Path) -> List[str]:
    return [folder["path"] for folder in _read(root)["folders"]]


# --- the folder list ----------------------------------------------------------------------


def test_the_folders_are_the_workspace_then_each_checkout_dependencies_first(workspace: Path) -> None:
    manager.write_code_workspace(workspace)
    assert _read(workspace)["folders"] == [
        {"name": "acme", "path": "."},
        {"name": "aisland", "path": "aisland"},
        {"name": "loggair", "path": "loggair"},
        {"name": "confluid", "path": "confluid"},
    ]


def test_an_empty_submodule_folder_is_left_out(workspace: Path) -> None:
    """An inactive submodule, or one detached to PyPI or GitHub, has no checkout to show."""
    manager.write_code_workspace(workspace)
    assert "vendorcli" not in _paths(workspace)


def test_a_project_whose_checkout_is_removed_leaves_the_list(workspace: Path) -> None:
    """What `aisland workspace configure source set loggair pypi` does to the folder: its checkout is gone, so is its entry."""
    (workspace / "loggair" / "pyproject.toml").unlink()
    manager.write_code_workspace(workspace)
    assert _paths(workspace) == [".", "aisland", "confluid"]


def test_the_order_does_not_follow_the_order_the_disk_lists_folders_in(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two machines with the same checkouts write the same list, whatever order `iterdir` returns."""
    manager.write_code_workspace(workspace)
    expected = _paths(workspace)
    found = manager.discover_projects(workspace)
    monkeypatch.setattr(manager, "discover_projects", lambda root: list(reversed(found)))
    manager.code_workspace_path(workspace).unlink()
    manager.write_code_workspace(workspace)
    assert _paths(workspace) == expected


# --- loggair's log folder -----------------------------------------------------------------


def test_the_log_folder_loggair_resolves_is_listed_last(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    logs = workspace.parent / "logs"
    logs.mkdir()
    monkeypatch.setattr(manager, "_loggair_log_dir", lambda workspace_root: logs)
    manager.write_code_workspace(workspace)
    assert _read(workspace)["folders"][-1] == {"name": "logs", "path": "../logs"}


def test_a_log_folder_that_does_not_exist_is_left_out(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager, "_loggair_log_dir", lambda workspace_root: workspace.parent / "logs")
    manager.write_code_workspace(workspace)
    assert "../logs" not in _paths(workspace)


def _fake_python(root: Path, script: str) -> None:
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n" + script)
    python.chmod(0o755)


def test_loggair_is_asked_from_the_workspace_root_with_the_workspace_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run in the workspace sources `.env` and starts at the root; the question is asked the same way."""
    monkeypatch.delenv("LOGGAIR_DIR", raising=False)
    (tmp_path / ".env").write_text("# loggair\nLOGGAIR_DIR=run-logs\n")
    _fake_python(tmp_path, 'printf \'{"resolved": {"log_dir": "%s/%s"}}\' "$PWD" "$LOGGAIR_DIR"\n')
    assert manager._loggair_log_dir(tmp_path) == tmp_path.resolve() / "run-logs"


def test_the_real_loggair_report_names_its_log_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the contract with loggair's own `python -m loggair --json`, not a stand-in for it."""
    pytest.importorskip("loggair")
    (tmp_path / ".env").write_text("LOGGAIR_DIR=run-logs\n")
    _fake_python(tmp_path, f'exec "{sys.executable}" "$@"\n')
    assert manager._loggair_log_dir(tmp_path) == tmp_path.resolve() / "run-logs"


@pytest.mark.parametrize(
    "script",
    [
        "exit 1\n",  # loggair missing, or its report failed
        "echo not json\n",
        "echo '{\"resolved\": {}}'\n",
    ],
)
def test_no_answer_from_loggair_lists_no_log_folder(tmp_path: Path, script: str) -> None:
    _fake_python(tmp_path, script)
    assert manager._loggair_log_dir(tmp_path) is None


def test_no_venv_lists_no_log_folder(tmp_path: Path) -> None:
    assert manager._loggair_log_dir(tmp_path) is None


# --- the rest of the file -----------------------------------------------------------------


def test_settings_and_extensions_come_from_the_template(workspace: Path) -> None:
    manager.write_code_workspace(workspace)
    written = _read(workspace)
    written.pop("folders")
    assert written == json.loads(manager.CODE_WORKSPACE_TEMPLATE.read_text())


def test_another_workspace_s_settings_name_its_own_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The template is written for `acme`; in `acme` every `${workspaceFolder:acme}` names `acme`."""
    root = tmp_path / "acme"
    _project(root, "models")
    monkeypatch.setattr(manager, "_loggair_log_dir", lambda workspace_root: None)
    manager.write_code_workspace(root)
    text = manager.code_workspace_path(root).read_text()
    settings = _read(root)["settings"]
    assert "acme" not in text.split("\n", 6)[-1]  # past the header
    assert settings["python.defaultInterpreterPath"] == "${workspaceFolder:acme}/.venv/bin/python"
    assert settings["terminal.integrated.defaultProfile.osx"] == "acme-bash"
    assert settings["terminal.integrated.profiles.osx"]["acme-bash"]["args"] == [
        "--rcfile",
        "${workspaceFolder:acme}/vscode-init-bash.sh",
    ]


def test_a_terminal_shell_missing_on_this_machine_is_the_bash_on_its_path(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The template names Homebrew's bash on macOS; a Mac without it gets the bash it has. Only this platform's
    profile is looked at: the other platform's shell is not on this machine either way."""
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin")
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda self: False if str(self) == "/opt/homebrew/bin/bash" else real_exists(self)
    )
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/bash" if name == "bash" else None)
    manager.write_code_workspace(workspace)
    profiles = _read(workspace)["settings"]["terminal.integrated.profiles.osx"]
    assert profiles["acme-bash"]["path"] == "/usr/local/bin/bash"
    linux = _read(workspace)["settings"]["terminal.integrated.profiles.linux"]
    assert linux["acme-bash"]["path"] == "/bin/bash"


def test_a_terminal_shell_this_machine_has_is_kept_as_written(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager.platform, "system", lambda: "Darwin")
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda self: True if str(self) == "/opt/homebrew/bin/bash" else real_exists(self)
    )
    monkeypatch.setattr(manager.shutil, "which", lambda name: "/usr/local/bin/bash")
    manager.write_code_workspace(workspace)
    profiles = _read(workspace)["settings"]["terminal.integrated.profiles.osx"]
    assert profiles["acme-bash"]["path"] == "/opt/homebrew/bin/bash"


def test_the_template_holds_no_folders_and_no_extra_paths() -> None:
    """Folders are this machine's; `python.analysis.extraPaths` is ignored while `pyrightconfig.json` exists."""
    template = json.loads(manager.CODE_WORKSPACE_TEMPLATE.read_text())
    assert set(template) == {"settings", "extensions"}
    assert "python.analysis.extraPaths" not in template["settings"]


def test_the_regenerate_header_is_a_command_the_cli_accepts(workspace: Path) -> None:
    from aisland.cli import main

    manager.write_code_workspace(workspace)
    text = manager.code_workspace_path(workspace).read_text()
    line = next(ln for ln in text.splitlines() if "To regenerate:" in ln)
    argv = line.split("To regenerate:")[1].split()
    assert argv == ["aisland", "workspace", "code-workspace"], argv
    group = main.get_command(click.Context(main), argv[1])
    assert isinstance(group, click.Group), f"`aisland {argv[1]}` is not a command group"
    command = group.get_command(click.Context(group), argv[2])
    assert command is not None, f"`aisland {argv[1]} {argv[2]}` is not a command"
    # The command's own parser is the authority on whatever follows its name.
    with command.make_context(argv[2], argv[3:], resilient_parsing=False):
        pass


def test_an_unchanged_file_is_not_rewritten(workspace: Path) -> None:
    assert manager.write_code_workspace(workspace) is not None
    assert manager.write_code_workspace(workspace) is None


# --- the commands that rewrite it ---------------------------------------------------------


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> List[Path]:
    calls: List[Path] = []
    monkeypatch.setattr(manager, "write_code_workspace", lambda root: calls.append(root))
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(s, "project_path", lambda root, project: root / project)
    monkeypatch.setattr(s, "known_projects", lambda root: ["loggair"])
    monkeypatch.setattr(s, "local_override", lambda root, project: None)
    monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
    monkeypatch.setattr(s, "apply_source", lambda *a, **k: True)
    monkeypatch.setattr(s, "apply_all", lambda *a, **k: None)
    return calls


@pytest.fixture
def venv_root(tmp_path: Path) -> Path:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    return tmp_path


def test_a_machine_only_source_change_rewrites_the_file(venv_root: Path, written: List[Path]) -> None:
    """It used to print `Skipped editor regeneration`, because the file was tracked."""
    assert s.set_project_source("loggair", "local", machine_only=True, workspace_root=venv_root) == 0
    assert written == [venv_root]


def test_a_committed_source_change_rewrites_the_file(venv_root: Path, written: List[Path]) -> None:
    assert s.set_project_source("loggair", "local", workspace_root=venv_root) == 0
    assert written == [venv_root]


def test_a_failed_source_change_rewrites_nothing(
    venv_root: Path, written: List[Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(s, "apply_source", lambda *a, **k: False)
    assert s.set_project_source("loggair", "local", workspace_root=venv_root) == 1
    assert written == []


def test_unset_rewrites_the_file(venv_root: Path, written: List[Path]) -> None:
    s.write_committed_sources(venv_root, {"loggair": s.ProjectSource(kind="pypi")})
    assert s.unset_project_source("loggair", workspace_root=venv_root) == 0
    assert written == [venv_root]


@pytest.mark.parametrize("project", [None, "loggair"])
def test_apply_rewrites_the_file(venv_root: Path, written: List[Path], project: Optional[str]) -> None:
    s.write_committed_sources(venv_root, {"loggair": s.ProjectSource(kind="local", ref="dev/main")})
    assert s.apply_configured(project, workspace_root=venv_root) == 0
    assert written == [venv_root]


# --- the real workspace -------------------------------------------------------------------


def test_the_real_workspace_keeps_the_file_out_of_git() -> None:
    root = manager.get_workspace_root()
    if not (root / ".gitmodules").exists():
        pytest.skip("not run inside the workspace")
    path = manager.code_workspace_path(root)
    ignored = subprocess.run(["git", "check-ignore", "--no-index", "-q", str(path)], cwd=root)
    assert ignored.returncode == 0, f"{path.name} is not ignored by {root / '.gitignore'}"
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", path.name], cwd=root, capture_output=True)
    assert tracked.returncode != 0, f"{path.name} is still tracked: git rm --cached {path.name}"
