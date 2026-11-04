"""`aisland workspace code-workspace` and `aisland workspace bashrc`: each of setup's two steps run alone.

What is pinned: the VS Code file is written, and an unchanged one is left and said to be up to date; the root's
`project.bashrc` is linked to aisland's copy, a correct link is left and said, a link pointing elsewhere replaced,
and a regular file refused with one `Error:` line, untouched.
"""

import os
import subprocess
from pathlib import Path

import click.testing
import pytest
from click.testing import CliRunner

from aisland import cli, editor_files, locations, paths


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace `acme/` with aisland's copy of `project.bashrc` in `acme/aisland/`."""
    root = tmp_path / "acme"
    (root / "aisland").mkdir(parents=True)
    (root / "aisland" / "project.bashrc").write_text("# aisland's copy\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(paths, "state_dir", lambda: root / "aisland")
    monkeypatch.setattr(locations, "FROM_CHECKOUT", True)  # the layout above is a checkout's
    monkeypatch.setattr(editor_files, "_loggair_log_dir", lambda workspace_root: None)
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    return root


def _run(*args: str) -> click.testing.Result:
    return CliRunner().invoke(cli.main, ["workspace", *args])


def test_code_workspace_writes_the_file_and_leaves_an_unchanged_one(repo: Path) -> None:
    first = _run("code-workspace")
    assert first.exit_code == 0, first.output
    assert first.output == "==> Wrote acme.code-workspace (1 folders)\n"
    assert (repo / "acme.code-workspace").is_file()
    before = (repo / "acme.code-workspace").stat().st_mtime_ns
    again = _run("code-workspace")
    assert again.exit_code == 0 and again.output == "==> acme.code-workspace is up to date\n"
    assert (repo / "acme.code-workspace").stat().st_mtime_ns == before


def test_code_workspace_follows_a_new_checkout(repo: Path) -> None:
    assert _run("code-workspace").exit_code == 0
    (repo / "lib").mkdir()
    (repo / "lib" / "pyproject.toml").write_text('[project]\nname = "lib"\nversion = "0.1.0"\n')
    result = _run("code-workspace")
    assert result.output == "==> Wrote acme.code-workspace (2 folders)\n"


def test_bashrc_links_the_root_to_aislands_copy(repo: Path) -> None:
    result = _run("bashrc")
    assert result.exit_code == 0, result.output
    assert result.output == f"==> Linked {repo / 'project.bashrc'} -> aisland/project.bashrc\n"
    assert os.readlink(repo / "project.bashrc") == "aisland/project.bashrc"
    assert (repo / "project.bashrc").read_text() == "# aisland's copy\n"


def test_bashrc_leaves_a_correct_link_and_says_so(repo: Path) -> None:
    os.symlink("aisland/project.bashrc", repo / "project.bashrc")
    result = _run("bashrc")
    assert result.exit_code == 0
    assert result.output == "==> project.bashrc already links to aisland/project.bashrc\n"


def test_bashrc_replaces_a_link_pointing_elsewhere(repo: Path) -> None:
    (repo / "other.bashrc").write_text("# another\n")
    os.symlink("other.bashrc", repo / "project.bashrc")
    result = _run("bashrc")
    assert result.exit_code == 0
    assert result.output == f"==> Linked {repo / 'project.bashrc'} -> aisland/project.bashrc\n"
    assert os.readlink(repo / "project.bashrc") == "aisland/project.bashrc"


def test_bashrc_refuses_a_regular_file_and_leaves_it(repo: Path) -> None:
    (repo / "project.bashrc").write_text("# my own lines\n")
    result = _run("bashrc")
    assert result.exit_code == 1
    assert result.output == (
        f"Error: {repo / 'project.bashrc'} is a regular file, not a link — move your lines to "
        f"{repo / 'user.bashrc'}, delete it, and run `aisland workspace bashrc` again\n"
    )
    assert (repo / "project.bashrc").read_text() == "# my own lines\n"
    assert not (repo / "project.bashrc").is_symlink()


def test_both_are_workspace_commands() -> None:
    help_text = _run("--help").output
    assert "bashrc" in help_text and "code-workspace" in help_text
