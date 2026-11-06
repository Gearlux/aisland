"""A workspace combines harness sets: aisland's own, a project's, a folder of the workspace, an installed package's and
a repository elsewhere, listed in `workspace.toml`'s `[harness] sets` (`aisland/setup/harness.py`)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict

import pytest
from click.testing import CliRunner

from aisland import cli
from aisland.cli import workspace as cli_workspace
from aisland.setup import harness
from aisland.workspace import check as workspace_check
from aisland.workspace import file as workspace_file


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty workspace, aisland's own set a small stand-in, git sets cloned into a scratch folder."""
    folder = tmp_path / "acme"
    folder.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
    builtin = tmp_path / "builtin"
    _write(builtin / "harness.toml", 'rules = "AGENTS.md"\ncommands = ["commands/*.md"]\nscripts = ["scripts/*.py"]\n')
    _write(builtin / "AGENTS.md", "# aisland rules\n")
    _write(builtin / "commands" / "ship.md", "ship\n")
    _write(builtin / "scripts" / "stage_hunks.py", "# stage\n")
    monkeypatch.setattr(harness, "HARNESS_DIR", builtin)
    monkeypatch.setattr(harness, "clones_dir", lambda: tmp_path / "clones")
    monkeypatch.setattr(harness, "registered", lambda: {})
    return folder


def _sets(root: Path, value: str) -> Path:
    return _write(root / "workspace.toml", f"[workspace]\nformat = 1\n\n[harness]\nsets = {value}\n")


def _index(root: Path) -> list:
    return [line for line in (root / ".agents/rules.md").read_text().splitlines() if line.startswith("@")]


def _agents_set(root: Path) -> Path:
    """A folder of the workspace holding a set described by its harness.toml: rules, a command, a Cursor rule."""
    folder = root / "agents"
    _write(
        folder / "harness.toml",
        'rules = "rules/AGENTS.md"\ncommands = ["commands/*.md"]\n\n[files]\n"cursor/*.mdc" = ".cursor/rules/"\n',
    )
    _write(folder / "rules" / "AGENTS.md", "# our rules\n")
    _write(folder / "commands" / "review.md", "review\n")
    _write(folder / "cursor" / "style.mdc", "style\n")
    return folder


# --- which sets ---------------------------------------------------------------------------------------------------


def test_without_a_harness_table_the_workspace_takes_aislands_own_set(root: Path) -> None:
    harness.install_harness(root, link=False)
    assert (root / ".agents/workflows/ship.md").read_text() == "ship\n"
    assert _index(root) == ["@.agents/aisland/AGENTS.md"]
    assert (root / "AGENTS.md").read_text().splitlines()[0] == "@.agents/rules.md"


def test_an_empty_list_installs_nothing_and_takes_away_what_an_earlier_install_put(root: Path) -> None:
    harness.install_harness(root, link=False)
    (root / "AGENTS.md").unlink()
    _sets(root, "[]")
    assert harness.install_harness(root, link=False) == []
    assert not (root / ".agents/workflows/ship.md").exists()
    assert not (root / ".agents/rules.md").exists()
    assert not (root / "AGENTS.md").exists()


def test_a_folder_of_the_workspace_with_its_harness_toml_lands_beside_aislands(root: Path) -> None:
    _agents_set(root)
    _sets(root, '["aisland", "agents"]')
    assert harness.install_harness(root, link=False) == []
    assert (root / ".agents/workflows/review.md").read_text() == "review\n"
    assert (root / ".agents/workflows/ship.md").read_text() == "ship\n"
    assert (root / ".cursor/rules/style.mdc").read_text() == "style\n"
    assert (root / ".agents/agents/AGENTS.md").read_text() == "# our rules\n"
    assert _index(root) == ["@.agents/aisland/AGENTS.md", "@.agents/agents/AGENTS.md"]


def test_from_a_checkout_a_sets_file_is_a_link_to_the_sets_own(root: Path) -> None:
    _agents_set(root)
    _sets(root, '["agents"]')
    harness.install_harness(root, link=True)
    target = root / ".agents/workflows/review.md"
    assert target.is_symlink() and target.resolve() == (root / "agents/commands/review.md").resolve()


def test_a_project_brings_the_folder_its_pyproject_names(root: Path) -> None:
    _write(root / "models" / "pyproject.toml", '[project]\nname = "models"\n\n[tool.aisland]\nharness = "harness"\n')
    _write(root / "models" / "harness" / "AGENTS.md", "# models rules\n")
    _write(root / "models" / "harness" / ".claude" / "commands" / "train.md", "train\n")
    _sets(root, '["models"]')
    harness.install_harness(root, link=False)
    assert (root / ".agents/workflows/train.md").read_text() == "train\n"
    assert _index(root) == ["@.agents/models/AGENTS.md"]


def test_a_project_without_a_harness_toml_is_read_where_agents_read_it(root: Path) -> None:
    """A project that knows nothing of aisland: its CLAUDE.md, its .claude/commands, its Cursor and Copilot files."""
    project = root / "viewer"
    _write(project / "pyproject.toml", '[project]\nname = "viewer"\n')
    _write(project / "CLAUDE.md", "# viewer rules\n")
    _write(project / ".claude" / "commands" / "render.md", "render\n")
    _write(project / ".cursor" / "rules" / "ts.mdc", "ts\n")
    _write(project / ".github" / "copilot-instructions.md", "copilot\n")
    _sets(root, '["aisland", "viewer"]')
    assert harness.install_harness(root, link=False) == []
    assert (root / ".agents/viewer/AGENTS.md").read_text() == "# viewer rules\n"
    assert (root / ".agents/workflows/render.md").read_text() == "render\n"
    assert (root / ".cursor/rules/ts.mdc").read_text() == "ts\n"
    assert (root / ".github/copilot-instructions.md").read_text() == "copilot\n"
    # its own commands folder is the set's: kept, said nothing about
    assert (project / ".claude" / "commands").is_dir() and not (project / ".claude" / "commands").is_symlink()


def test_a_projects_claude_commands_link_is_the_workspaces_never_the_sets(root: Path) -> None:
    """The link aisland makes in a project points at the workspace's commands: read as the project's, it would make
    every command a clash."""
    project = root / "viewer"
    _write(project / "pyproject.toml", '[project]\nname = "viewer"\n')
    _write(project / "AGENTS.md", "# viewer rules\n")
    harness.install_harness(root, link=False)  # aisland's alone: the project gets its link
    assert (project / ".claude" / "commands").is_symlink()
    _sets(root, '["aisland", "viewer"]')
    assert harness.install_harness(root, link=False) == []
    assert [row["files"] for row in harness.rows(root) if row["set"] == "viewer"] == [[]]


def test_an_installed_packages_set_is_found_by_its_registered_name(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "site" / "acme_harness"
    _write(package / "AGENTS.md", "# team rules\n")
    monkeypatch.setattr(harness, "registered", lambda: {"acme-harness": package})
    _sets(root, '["acme-harness"]')
    harness.install_harness(root, link=False)
    assert _index(root) == ["@.agents/acme-harness/AGENTS.md"]
    assert harness.rows(root)[0]["kind"] == "package"


def test_a_name_that_is_no_set_is_refused_naming_its_line(root: Path) -> None:
    _sets(root, '["nowhere"]')
    with pytest.raises(harness.HarnessError, match=r"workspace\.toml:5: 'nowhere' is no project or folder"):
        harness.install_harness(root, link=False)


# --- a repository elsewhere ---------------------------------------------------------------------------------------


def _remote(tmp_path: Path, files: Dict[str, str], tag: str) -> str:
    """A repository holding `files`, tagged `tag`; its file:// address."""
    work = tmp_path / "remote"
    for name, text in files.items():
        _write(work / name, text)
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(["git", "add", "."], cwd=work, check=True)
    commit = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", tag]
    subprocess.run(commit, cwd=work, check=True)
    subprocess.run(["git", "tag", "-f", tag], cwd=work, check=True)
    return work.as_uri()


def test_a_git_set_is_cloned_at_its_ref_and_fetched_again_only_when_the_ref_changes(root: Path, tmp_path: Path) -> None:
    url = _remote(tmp_path, {"AGENTS.md": "# v1 rules\n"}, "v1")
    _sets(root, f'[{{ git = "{url}", ref = "v1", name = "team" }}]')
    harness.install_harness(root, link=False)
    assert (root / ".agents/team/AGENTS.md").read_text() == "# v1 rules\n"

    _write(tmp_path / "remote" / "AGENTS.md", "# v2 rules\n")
    _remote(tmp_path, {}, "v2")
    harness.install_harness(root, link=False)  # same ref: the clone is not fetched again
    assert (root / ".agents/team/AGENTS.md").read_text() == "# v1 rules\n"

    _sets(root, f'[{{ git = "{url}", ref = "v2", name = "team" }}]')
    harness.install_harness(root, link=False)
    assert (root / ".agents/team/AGENTS.md").read_text() == "# v2 rules\n"


def test_a_git_set_without_a_ref_is_refused(root: Path) -> None:
    _sets(root, '[{ git = "https://example.invalid/rules.git" }]')
    with pytest.raises(harness.HarnessError, match=r"workspace\.toml:5: a git set names its url and a ref"):
        harness.install_harness(root, link=False)


def test_an_unreachable_repository_fails_naming_its_line(root: Path, tmp_path: Path) -> None:
    missing = (tmp_path / "no-such-repo").as_uri()
    _sets(root, f'[{{ git = "{missing}", ref = "v1" }}]')
    with pytest.raises(harness.HarnessError, match=rf"workspace\.toml:5: could not clone {missing}"):
        harness.install_harness(root, link=False)


def test_a_url_that_looks_like_an_option_is_read_as_a_repository(root: Path) -> None:
    """`workspace.toml` is another person's file once a workspace is cloned: its url reaches git after `--`, so
    `--template=…` or `--upload-pack=…` names a repository that does not exist and never becomes an option."""
    _sets(root, '[{ git = "--template=/nowhere", ref = "v1", name = "team" }]')
    # git's own reason names the url as the repository: read as an option, the repository would be `team`.
    with pytest.raises(harness.HarnessError, match=r"could not clone --template=/nowhere: .*'--template=/nowhere'"):
        harness.install_harness(root, link=False)
    assert not (harness.clones_dir() / "team").exists()


@pytest.mark.parametrize("entry", ['name = ".."', 'name = "../../.."', 'name = "/"', 'name = "a/b"'])
def test_a_git_set_name_that_is_no_plain_folder_name_is_refused_before_anything_is_removed(
    root: Path, entry: str
) -> None:
    """The name is the clone's folder under aisland's state: `..` would make setup remove the state folder itself,
    `/` the disk. It is refused before any folder is touched or any clone begins."""
    keep = harness.clones_dir().parent / "jenkins" / "config.xml"
    _write(keep, "<jenkins/>\n")
    _sets(root, f'[{{ git = "/nowhere/team.git", ref = "v1", {entry} }}]')
    with pytest.raises(harness.HarnessError, match="cannot name a harness set"):
        harness.install_harness(root, link=False)
    assert keep.is_file()


def test_a_url_whose_last_part_is_no_plain_name_is_refused(root: Path) -> None:
    _sets(root, '[{ git = "https://example.com/team/..", ref = "v1" }]')
    with pytest.raises(harness.HarnessError, match=r"'\.\.' cannot name a harness set"):
        harness.install_harness(root, link=False)


def test_a_ref_the_repository_lacks_is_refused(root: Path, tmp_path: Path) -> None:
    url = _remote(tmp_path, {"AGENTS.md": "# rules\n"}, "v1")
    _sets(root, f'[{{ git = "{url}", ref = "v9", name = "team" }}]')
    with pytest.raises(harness.HarnessError, match=r"has no tag, branch or commit 'v9'"):
        harness.install_harness(root, link=False)


# --- combining ------------------------------------------------------------------------------------------------------


def test_two_sets_shipping_one_file_are_refused_before_anything_is_installed(root: Path) -> None:
    _write(root / "agents" / "harness.toml", 'commands = ["commands/*.md"]\n')
    _write(root / "agents" / "commands" / "ship.md", "our ship\n")
    _sets(root, '["aisland", "agents"]')
    with pytest.raises(harness.HarnessError) as refused:
        harness.install_harness(root, link=False)
    assert refused.value.message == (
        f"{root / 'workspace.toml'}:5: two harness sets ship .agents/workflows/ship.md: aisland and agents — "
        "exclude it from one of them in [harness] sets"
    )
    assert not (root / ".agents").exists()


def test_an_excluded_file_leaves_the_other_sets_one_in_its_place(root: Path) -> None:
    _write(root / "agents" / "harness.toml", 'commands = ["commands/*.md"]\n')
    _write(root / "agents" / "commands" / "ship.md", "our ship\n")
    _sets(root, '[{ set = "aisland", exclude = ["commands/ship.md"] }, "agents"]')
    harness.install_harness(root, link=False)
    assert (root / ".agents/workflows/ship.md").read_text() == "our ship\n"
    assert (root / ".agents/scripts/stage_hunks.py").is_file()


def test_an_exclude_that_names_no_file_is_refused(root: Path) -> None:
    _sets(root, '[{ set = "aisland", exclude = ["commands/shp.md"] }]')
    with pytest.raises(harness.HarnessError, match=r"exclude 'commands/shp\.md' names no file of the 'aisland'"):
        harness.install_harness(root, link=False)


def test_a_dropped_set_takes_its_files_and_its_import_with_it(root: Path) -> None:
    _agents_set(root)
    _sets(root, '["aisland", "agents"]')
    harness.install_harness(root, link=False)
    _sets(root, '["aisland"]')
    assert harness.install_harness(root, link=False) == []
    assert not (root / ".agents/workflows/review.md").exists()
    assert not (root / ".cursor/rules/style.mdc").exists()
    assert _index(root) == ["@.agents/aisland/AGENTS.md"]


def test_two_sets_of_one_name_are_refused(root: Path) -> None:
    _agents_set(root)
    _sets(root, '["agents", { set = "aisland", name = "agents" }]')
    with pytest.raises(harness.HarnessError, match=r"two harness sets are named 'agents'"):
        harness.install_harness(root, link=False)


# --- a set's harness.toml -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, message",
    [
        ('rule = "AGENTS.md"\n', r"harness\.toml:1: rule is no setting of a harness set"),
        ('rules = "MISSING.md"\n', r"harness\.toml:1: rules must name a file of the set"),
        ('commands = "commands/*.md"\n', r"harness\.toml:1: commands must be a list of patterns"),
        ('[files]\n"x.md" = "../outside.md"\n', r"harness\.toml:2: '\.\./outside\.md' is no place in the workspace"),
        ('[files]\n"x.md" = ".agents/rules.md"\n', r"harness\.toml:2: '\.agents/rules\.md' is no place"),
        ('[files]\n"x.md" = ".GIT/hooks/pre-commit"\n', r"harness\.toml:2: '\.GIT/hooks/pre-commit' is no place"),
        ('[files]\n"x.md" = "sub/.git/config"\n', r"harness\.toml:2: 'sub/\.git/config' is no place"),
        (
            'format = 2\nrules = "x.md"\n',
            r"harness\.toml:1: harness\.toml is format 2, written for a newer aisland; this one reads format 1\. "
            r"Upgrade it: pip install --upgrade aisland",
        ),
        ("format = 1.5\n", r"harness\.toml:1: format must be a whole number, as format = 1"),
    ],
    ids=[
        "unknown key",
        "missing rules",
        "not a list",
        "outside the workspace",
        "aisland's own file",
        "git's folder in another case",
        "a project's git folder",
        "a newer format",
        "no whole number",
    ],
)
def test_a_harness_toml_aisland_cannot_use_is_refused_naming_its_line(root: Path, body: str, message: str) -> None:
    _write(root / "agents" / "harness.toml", body)
    _write(root / "agents" / "x.md", "x\n")
    _sets(root, '["agents"]')
    with pytest.raises(harness.HarnessError, match=message):
        harness.install_harness(root, link=False)


# --- the workspace's own files --------------------------------------------------------------------------------------


def test_the_harness_table_survives_setup_rewriting_the_file(root: Path) -> None:
    _sets(root, '["aisland", { set = "agents", exclude = ["x.md"] }]')
    current = workspace_file.load(root)
    workspace_file.write(root, current)
    assert workspace_file.load(root).harness == {"sets": ["aisland", {"set": "agents", "exclude": ["x.md"]}]}


def test_a_key_of_the_harness_table_aisland_does_not_read_is_named(root: Path) -> None:
    _write(root / "workspace.toml", '[workspace]\nformat = 1\n\n[harness]\nset = ["aisland"]\n')
    assert workspace_check.unknown_keys(root) == [
        f"Warning: {root / 'workspace.toml'}:5: [harness] set is no setting aisland reads (did you mean sets?)"
    ]


def test_the_rules_index_says_where_it_comes_from_and_how_to_write_it_again(root: Path) -> None:
    harness.install_harness(root, link=False)
    text = (root / ".agents/rules.md").read_text()
    assert "Generated by: aisland setup" in text
    assert "To regenerate: aisland workspace harness install" in text
    assert CliRunner().invoke(cli.main, ["workspace", "harness", "install", "--help"]).exit_code == 0


# --- the commands ---------------------------------------------------------------------------------------------------


@pytest.fixture
def at_root(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cli_workspace, "_configure_root", lambda: root)
    return root


def test_the_listing_names_each_set_and_what_it_installs(at_root: Path) -> None:
    _agents_set(at_root)
    _sets(at_root, '["aisland", "agents"]')
    result = CliRunner().invoke(cli.main, ["workspace", "harness", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)["sets"]
    assert [(row["set"], row["kind"], row["rules"]) for row in rows] == [
        ("aisland", "aisland", ".agents/aisland/AGENTS.md"),
        ("agents", "folder", ".agents/agents/AGENTS.md"),
    ]
    assert rows[1]["files"] == [".agents/workflows/review.md", ".cursor/rules/style.mdc"]


def test_install_installs_and_says_what(at_root: Path) -> None:
    result = CliRunner().invoke(cli.main, ["workspace", "harness", "install"])
    assert result.exit_code == 0, result.output
    assert "==> Harness: aisland (1 command, 1 script, rules)" in result.stdout
    assert (at_root / ".agents/workflows/ship.md").exists()


def test_an_exported_copy_of_aislands_set_is_a_set_of_its_own(at_root: Path) -> None:
    result = CliRunner().invoke(cli.main, ["workspace", "harness", "export", str(at_root / "agents")])
    assert result.exit_code == 0, result.output
    _sets(at_root, '["agents"]')
    harness.install_harness(at_root, link=False)
    assert (at_root / ".agents/workflows/ship.md").read_text() == "ship\n"
    assert _index(at_root) == ["@.agents/agents/AGENTS.md"]


def test_export_refuses_a_folder_that_holds_files(at_root: Path) -> None:
    _write(at_root / "agents" / "mine.md", "mine\n")
    result = CliRunner().invoke(cli.main, ["workspace", "harness", "export", str(at_root / "agents")])
    assert result.exit_code == 1
    assert "is not empty" in result.stderr


def test_aislands_own_set_describes_itself() -> None:
    """The set in the package says what it installs, so its export is a set that works on its own."""
    assert (harness.locations.PACKAGE_DIR / "harness" / harness.SET_FILE).is_file()
    assert os.path.basename(harness.RULES) == "AGENTS.md"
