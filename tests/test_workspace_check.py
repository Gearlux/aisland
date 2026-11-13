"""A key of `workspace.toml` that no part of aisland reads is named, with its line and the closest key it reads.

`aisland setup`, `aisland workspace show` and `aisland workspace configure` print the warnings; none fails.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from aisland import cli, locations
from aisland.workspace import check as workspace_check


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace whose `app` project defines the `framework` selection."""
    root = tmp_path / "acme"
    (root / "app").mkdir(parents=True)
    (root / "app" / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "app"
            version = "0.1.0"

            [project.optional-dependencies]
            torch = []

            [tool.aisland.selection.framework]
            question = "Which frameworks?"
            choices = ["torch"]
            """
        )
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    return root


def _write(root: Path, text: str) -> Path:
    path = root / "workspace.toml"
    path.write_text(textwrap.dedent(text))
    return path


def test_a_typo_in_a_projects_table_is_named_with_its_line_and_the_key_meant(ws: Path) -> None:
    path = _write(
        ws,
        """\
        [project."app"]
        url = "https://example.com/acme/app.git"
        sorce = "pypi"
        """,
    )
    assert workspace_check.unknown_keys(ws) == [
        f"Warning: {path}:3: project 'app': sorce is no setting aisland reads (did you mean source?)"
    ]


def test_a_typo_in_the_workspace_table_is_named_too(ws: Path) -> None:
    path = _write(ws, '[workspace]\nframeworks = ["torch"]\n')
    assert workspace_check.unknown_keys(ws) == [
        f"Warning: {path}:2: [workspace] frameworks is no setting aisland reads (did you mean framework?)"
    ]


def test_a_key_close_to_none_lists_the_keys_aisland_reads(ws: Path) -> None:
    _write(ws, '[project."app"]\nurl = "u"\ncolour = "red"\n')
    (warning,) = workspace_check.unknown_keys(ws)
    assert warning.endswith(
        "colour is no setting aisland reads (it reads active, branch, ci, distribution, mirror, path, source, test-stats, "
        "track, url, version)"
    )


def test_every_key_aisland_reads_and_every_selection_pass_without_a_word(ws: Path) -> None:
    _write(
        ws,
        """\
        [workspace]
        format = 1
        name = "acme"
        track = false
        test-stats = false
        extras = ["dev"]
        framework = ["torch"]

        [project."app"]
        path = "app"
        url = "https://example.com/acme/app.git"
        branch = "dev/main"
        source = "pypi"
        version = "1.0"
        distribution = "acme-app"
        mirror = "https://example.com/mirror/app.git"
        active = true
        track = false
        test-stats = false
        """,
    )
    assert workspace_check.unknown_keys(ws) == []


def test_a_workspace_without_the_file_or_with_an_unreadable_one_gets_no_warning(ws: Path) -> None:
    assert workspace_check.unknown_keys(ws) == []
    _write(ws, "not = valid = toml")
    assert workspace_check.unknown_keys(ws) == []


@pytest.mark.parametrize("args", [["workspace", "show"], ["workspace", "configure"]])
def test_the_commands_print_the_warning_and_still_succeed(ws: Path, args: list) -> None:
    path = _write(ws, '[project."app"]\nurl = "u"\nsorce = "pypi"\n')
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    assert f"Warning: {path}:3: project 'app': sorce is no setting aisland reads (did you mean source?)" in (
        result.stderr
    )
