"""The agent harness: the workspace commands and the generic agent rules aisland ships and installs.

`aisland setup` and `aisland workspace init` put aisland's commands into `.agents/workflows/` (`.claude/commands`
links there), the helper script `/commit-session` runs into `.agents/scripts/` and the generic rules into
`.agents/aisland/AGENTS.md`, which the workspace's `AGENTS.md` imports. From a checkout each file is a link into the
package, so an edit is aisland's; from a wheel it is a copy, replaced on the next setup unless someone edited it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest
import yaml

from aisland import locations
from aisland.setup import harness, manager
from tests.private_names import DOMAIN, PRIVATE, words_named


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An empty workspace: a git repository with nothing in it."""
    folder = tmp_path / "acme"
    folder.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
    return folder


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A harness of two commands, one script and the rules, standing in for aisland's own so a test can change it."""
    folder = tmp_path / "harness"
    _write(folder / "commands" / "ship.md", "---\ndescription: ship\n---\nship v1\n")
    _write(folder / "commands" / "list-actions.md", "---\ndescription: list\n---\nlist v1\n")
    _write(folder / "scripts" / "stage_hunks.py", "# stage v1\n")
    _write(folder / "AGENTS.md", "# rules v1\n")
    _write(folder / "harness.toml", 'rules = "AGENTS.md"\ncommands = ["commands/*.md"]\nscripts = ["scripts/*.py"]\n')
    monkeypatch.setattr(harness, "HARNESS_DIR", folder)
    return folder


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _targets(root: Path) -> Dict[str, Path]:
    return {name: root / name for _source, name in harness.shipped_files()}


TARGETS = [
    ".agents/workflows/list-actions.md",
    ".agents/workflows/ship.md",
    ".agents/scripts/stage_hunks.py",
    ".agents/aisland/AGENTS.md",
]


# --- what is installed where ----------------------------------------------------------------------------------


def test_the_shipped_files_land_in_the_agents_folders(shipped: Path) -> None:
    assert [name for _source, name in harness.shipped_files()] == TARGETS


def test_from_a_checkout_every_file_is_a_relative_link_into_the_package(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=True)
    for source, name in harness.shipped_files():
        target = root / name
        assert target.is_symlink(), name
        assert not os.path.isabs(os.readlink(target)), name
        assert target.resolve() == source.resolve(), name


def test_from_a_wheel_every_file_is_a_copy(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    for source, name in harness.shipped_files():
        target = root / name
        assert not target.is_symlink(), name
        assert target.read_bytes() == source.read_bytes(), name


def test_by_default_a_checkout_links_and_a_wheel_copies(
    root: Path, shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    harness.install_harness(root)
    assert not (root / ".agents/workflows/ship.md").is_symlink()
    monkeypatch.setattr(locations, "FROM_CHECKOUT", True)
    harness.install_harness(root)
    assert (root / ".agents/workflows/ship.md").is_symlink()


def test_claude_finds_the_commands_through_a_link_to_the_workflows(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    assert os.readlink(root / ".claude" / "commands") == "../.agents/workflows"
    assert (root / ".claude" / "commands" / "ship.md").read_text() == "---\ndescription: ship\n---\nship v1\n"


def test_a_claude_commands_folder_of_the_users_own_is_left_and_named(root: Path, shipped: Path) -> None:
    _write(root / ".claude" / "commands" / "mine.md", "mine\n")
    report = harness.install_harness(root, link=False)
    assert (root / ".claude" / "commands").is_dir() and not (root / ".claude" / "commands").is_symlink()
    assert (
        ".claude/commands is a folder of its own — move its files into .agents/workflows and delete it, "
        "so Claude finds the harness's commands" in report
    )


def _project(root: Path, name: str, own_repository: bool = True) -> Path:
    """A project folder of the workspace: its `pyproject.toml`, and (like a submodule) a repository of its own."""
    project = _write(root / name / "pyproject.toml", f'[project]\nname = "{name}"\n').parent
    if own_repository:
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return project


def test_a_session_started_in_a_project_finds_the_commands_too(root: Path, shipped: Path) -> None:
    """Claude Code reads `.claude/commands` of the folder it starts in, not of the workspace above it (measured
    2026-10-20: started in a project folder it listed none of the six), so every project links to the workflows."""
    project = _project(root, "models")
    harness.install_harness(root, link=False)
    assert os.readlink(project / ".claude" / "commands") == "../../.agents/workflows"
    assert (project / ".claude" / "commands" / "ship.md").read_text().endswith("ship v1\n")


def test_a_projects_link_is_excluded_in_its_own_repository_and_changes_no_tracked_file(
    root: Path, shipped: Path
) -> None:
    project = _project(root, "models")
    harness.install_harness(root, link=False)
    harness.install_harness(root, link=False)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=project, capture_output=True, text=True).stdout
    assert status == "?? pyproject.toml\n"  # the project's own file, never committed in this test; no `.claude/`
    assert not (project / ".gitignore").exists()
    exclude = (project / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count("/.claude/commands") == 1


def test_a_project_folder_inside_the_workspaces_repository_is_excluded_by_its_path_there(
    root: Path, shipped: Path
) -> None:
    _project(root, "models", own_repository=False)
    harness.install_harness(root, link=False)
    assert subprocess.run(["git", "check-ignore", "-q", "models/.claude/commands"], cwd=root).returncode == 0


def test_a_project_with_a_claude_commands_folder_of_its_own_keeps_it_and_is_named(root: Path, shipped: Path) -> None:
    project = _project(root, "models")
    _write(project / ".claude" / "commands" / "mine.md", "mine\n")
    report = harness.install_harness(root, link=False)
    assert not (project / ".claude" / "commands").is_symlink()
    assert (
        "models/.claude/commands is a folder of its own — move its files into .agents/workflows and delete it, "
        "so Claude finds the harness's commands" in report
    )


def test_a_folder_that_is_no_project_gets_no_link(root: Path, shipped: Path) -> None:
    (root / "notes").mkdir()
    (root / ".hidden").mkdir()
    _write(root / ".hidden" / "pyproject.toml", "")
    harness.install_harness(root, link=False)
    assert not os.path.lexists(root / "notes" / ".claude")
    assert not os.path.lexists(root / ".hidden" / ".claude")


def test_a_second_install_with_nothing_changed_reports_nothing(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    assert harness.install_harness(root, link=False) == []
    harness.install_harness(root, link=True)
    assert harness.install_harness(root, link=True) == []


# --- copies: replaced when aisland's, kept when edited ------------------------------------------------------------


def test_a_newer_aisland_replaces_an_unedited_copy(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    (shipped / "commands" / "ship.md").write_text("---\ndescription: ship\n---\nship v2\n")
    report = harness.install_harness(root, link=False)
    assert (root / ".agents/workflows/ship.md").read_text().endswith("ship v2\n")
    assert not any(line.startswith("kept") for line in report)


def test_an_edited_copy_is_kept_and_named(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    _write(root / ".agents/workflows/ship.md", "my own ship\n")
    (shipped / "commands" / "ship.md").write_text("---\ndescription: ship\n---\nship v2\n")
    report = harness.install_harness(root, link=False)
    assert (root / ".agents/workflows/ship.md").read_text() == "my own ship\n"
    assert report == ["kept your edited .agents/workflows/ship.md — delete it to take aisland's"]


def test_a_deleted_copy_is_installed_again(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    (root / ".agents/workflows/ship.md").unlink()
    harness.install_harness(root, link=False)
    assert (root / ".agents/workflows/ship.md").read_text().endswith("ship v1\n")


def test_a_file_aisland_never_wrote_under_a_shipped_name_is_kept(root: Path, shipped: Path) -> None:
    _write(root / ".agents/workflows/ship.md", "a ship of my own\n")
    report = harness.install_harness(root, link=True)
    assert not (root / ".agents/workflows/ship.md").is_symlink()
    assert report == ["kept your edited .agents/workflows/ship.md — delete it to take aisland's"]


def test_a_file_equal_to_aislands_is_taken_over(root: Path, shipped: Path) -> None:
    _write(root / ".agents/workflows/ship.md", (shipped / "commands" / "ship.md").read_text())
    assert harness.install_harness(root, link=True) == []
    assert (root / ".agents/workflows/ship.md").is_symlink()


def test_a_command_of_the_users_own_is_never_touched(root: Path, shipped: Path) -> None:
    mine = _write(root / ".agents/workflows/mine.md", "mine\n")
    harness.install_harness(root, link=False)
    harness.install_harness(root, link=True)
    assert mine.read_text() == "mine\n" and not mine.is_symlink()


def test_the_record_of_the_copies_holds_their_fingerprints(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    record = json.loads((root / harness.RECORD).read_text())
    assert record == {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in TARGETS if name != harness.RECORD
    }


def test_the_record_holds_each_link_too_so_a_dropped_one_is_found(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=True)
    assert json.loads((root / harness.RECORD).read_text()) == {name: "link" for name in TARGETS}


# --- switching between a checkout and a wheel ------------------------------------------------------------------


def test_a_checkouts_links_become_copies_from_a_wheel(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=True)
    assert harness.install_harness(root, link=False) == []
    assert not (root / ".agents/workflows/ship.md").is_symlink()


def test_a_wheels_unedited_copies_become_links_from_a_checkout(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    assert harness.install_harness(root, link=True) == []
    assert (root / ".agents/workflows/ship.md").is_symlink()
    assert set(json.loads((root / harness.RECORD).read_text()).values()) == {"link"}


def test_an_edited_copy_is_kept_from_a_checkout_too(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    _write(root / ".agents/workflows/ship.md", "my own ship\n")
    report = harness.install_harness(root, link=True)
    assert (root / ".agents/workflows/ship.md").read_text() == "my own ship\n"
    assert report == ["kept your edited .agents/workflows/ship.md — delete it to take aisland's"]


# --- a command aisland no longer ships ------------------------------------------------------------------------


def test_a_link_to_a_command_aisland_dropped_is_removed(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=True)
    (shipped / "commands" / "list-actions.md").unlink()
    harness.install_harness(root, link=True)
    assert not os.path.lexists(root / ".agents/workflows/list-actions.md")


def test_an_unedited_copy_of_a_command_aisland_dropped_is_removed_an_edited_one_kept(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    _write(root / ".agents/workflows/ship.md", "my own ship\n")
    (shipped / "commands" / "list-actions.md").unlink()
    (shipped / "commands" / "ship.md").unlink()
    report = harness.install_harness(root, link=False)
    assert not (root / ".agents/workflows/list-actions.md").exists()
    assert (root / ".agents/workflows/ship.md").read_text() == "my own ship\n"
    assert report == ["kept your edited .agents/workflows/ship.md — no harness set of the workspace ships it any more"]


# --- the workspace's own AGENTS.md ----------------------------------------------------------------------------


def test_a_workspace_without_agents_md_gets_one_importing_the_rules(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    text = (root / "AGENTS.md").read_text()
    assert text.splitlines()[0] == "@.agents/rules.md"
    assert (root / text.splitlines()[0][1:]).is_file()  # the import names a file that exists
    assert "@.agents/aisland/AGENTS.md" in (root / ".agents/rules.md").read_text().splitlines()
    assert os.readlink(root / "CLAUDE.md") == "AGENTS.md"
    assert os.readlink(root / "GEMINI.md") == "AGENTS.md"


def test_an_agents_md_without_the_import_is_left_and_named(root: Path, shipped: Path) -> None:
    _write(root / "AGENTS.md", "# our rules\n")
    report = harness.install_harness(root, link=False)
    assert (root / "AGENTS.md").read_text() == "# our rules\n"
    assert not os.path.lexists(root / "CLAUDE.md")
    assert report == ['AGENTS.md does not import the harness\'s rules — add "@.agents/rules.md" as its first line']


def test_an_agents_md_with_the_import_is_left_and_says_nothing(root: Path, shipped: Path) -> None:
    _write(root / "AGENTS.md", "@.agents/rules.md\n\n# our rules\n")
    assert harness.install_harness(root, link=False) == []
    assert (root / "AGENTS.md").read_text() == "@.agents/rules.md\n\n# our rules\n"


def test_a_claude_md_of_its_own_is_kept(root: Path, shipped: Path) -> None:
    _write(root / "CLAUDE.md", "claude's own\n")
    harness.install_harness(root, link=False)
    assert (root / "CLAUDE.md").read_text() == "claude's own\n"
    assert os.readlink(root / "GEMINI.md") == "AGENTS.md"


# --- git -----------------------------------------------------------------------------------------------------


def _ignored(root: Path, name: str) -> bool:
    return subprocess.run(["git", "check-ignore", "-q", name], cwd=root).returncode == 0


def test_the_installed_files_are_ignored_and_the_users_own_are_not(root: Path, shipped: Path) -> None:
    _write(root / ".agents/workflows/mine.md", "mine\n")
    harness.install_harness(root, link=False)
    for name in TARGETS:
        assert _ignored(root, name), name
    assert _ignored(root, harness.RECORD)
    assert _ignored(root, harness.RULES_INDEX)
    assert not _ignored(root, ".agents/workflows/mine.md")
    assert not _ignored(root, "AGENTS.md")


def test_the_ignore_lines_are_added_once(root: Path, shipped: Path) -> None:
    harness.install_harness(root, link=False)
    harness.install_harness(root, link=False)
    lines = (root / ".gitignore").read_text().splitlines()
    assert len(lines) == len(set(lines))


# --- aisland's own harness ------------------------------------------------------------------------------------

#: Names that mean something only in the workspace aisland was written in: the harness runs in any workspace. The
#: private and domain names (`tests.private_names`, kept as digests) are checked beside these.
WORKSPACE_PROJECTS = re.compile(
    r"gearlux|annotaide|navigaitor|confluid|recordstream|matrainer"
    r"|liquifai|loggair|streamstudio|sonair|raidar|deltaid|curaitor|torpaido|distilaide|torchsig",
    re.IGNORECASE,
)


def _real_harness_files() -> List[Path]:
    return sorted(path for path in harness.HARNESS_DIR.rglob("*") if path.is_file() and "__pycache__" not in path.parts)


def test_the_harness_names_no_project_of_the_workspace_it_was_written_in() -> None:
    hits = [
        f"{path.relative_to(harness.HARNESS_DIR)}:{number}: {line.strip()}"
        for path in _real_harness_files()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if WORKSPACE_PROJECTS.search(line) or words_named(line, PRIVATE | DOMAIN)
    ]
    assert hits == []


def test_the_harness_carries_no_dated_ruling_of_the_workspace_it_was_written_in() -> None:
    """Every workspace installs these rules: a "user ruling 2026-…" is one person's decision in one workspace, which
    another workspace's user never made. A rule states itself; its date stays in that workspace's own records."""
    dated = re.compile(r"\b20\d\d-\d\d-\d\d\b|user (ruling|request|choice|pick|correction)s? \d")
    hits = [
        f"{path.relative_to(harness.HARNESS_DIR)}:{number}: {line.strip()}"
        for path in _real_harness_files()
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if dated.search(line)
    ]
    assert hits == []


def test_aisland_ships_the_six_commands_the_helper_and_the_rules() -> None:
    assert [name for _source, name in harness.shipped_files()] == [
        ".agents/workflows/commit-all.md",
        ".agents/workflows/commit-session.md",
        ".agents/workflows/list-actions.md",
        ".agents/workflows/pull-all.md",
        ".agents/workflows/push-all.md",
        ".agents/workflows/ship.md",
        ".agents/scripts/stage_hunks.py",
        ".agents/aisland/AGENTS.md",
    ]


@pytest.mark.parametrize("command", sorted((harness.HARNESS_DIR / "commands").glob("*.md")), ids=lambda p: p.stem)
def test_every_command_opens_with_frontmatter_naming_what_it_does(command: Path) -> None:
    text = command.read_text()
    assert text.startswith("---\n")
    front = yaml.safe_load(text.split("---\n")[1])
    assert isinstance(front["description"], str) and front["description"]


@pytest.mark.parametrize("command", ["commit-all", "commit-session", "pull-all", "push-all", "ship"])
def test_a_command_that_commits_runs_on_sonnet(command: str) -> None:
    text = (harness.HARNESS_DIR / "commands" / f"{command}.md").read_text()
    assert yaml.safe_load(text.split("---\n")[1])["model"] == "sonnet"


@pytest.mark.parametrize("command", ["commit-all", "commit-session"])
def test_a_commit_command_checks_the_branch_before_it_stages(command: str) -> None:
    """The rules say these two stop on a checkout outside the `dev/*` branches; the command must say how, first."""
    text = (harness.HARNESS_DIR / "commands" / f"{command}.md").read_text()
    front = yaml.safe_load(text.split("---\n")[1])
    assert "Bash(git -C * branch:*)" in front["allowed-tools"]
    steps = text.split("---\n", 2)[2]
    check = steps.index("git -C <repo> branch --show-current")
    assert check < steps.index("1. ")
    rule = steps[check : steps.index("\n", check)]
    assert "any `dev/*` branch" in rule and "outside `dev/*` — `main`" in rule and "detached HEAD" in rule
    assert "other than `dev/main`" not in rule


def test_push_all_pushes_each_repository_on_the_dev_branch_it_is_on() -> None:
    """A repository on `dev/<name>` is pushed on that branch, not skipped for differing from the root's; one outside
    `dev/*` is skipped and reported."""
    text = (harness.HARNESS_DIR / "commands" / "push-all.md").read_text()
    step2 = text[text.index("2. **What to push.**") : text.index("3. **Push the submodules**")]
    assert "`dev/*` branch it is on" in step2 and "branch outside `dev/*`" in step2
    assert "`BRANCH` = the root's branch" not in step2 and "A submodule not on `$BRANCH`" not in step2
    step4 = text[text.index("4. **Push the root last**") : text.index("5. **Mirrors.**")]
    assert "origin/<the submodule's own branch>" in step4 and "origin/$BRANCH` (a push" not in step4


@pytest.mark.parametrize("command", ["commit-all", "commit-session"])
def test_a_commit_command_reads_which_submodules_are_untracked_before_it_stages_a_pointer(command: str) -> None:
    """In a workspace whose submodules are untracked (`aisland workspace track unset`), `git add <submodule>` fails with
    `Skipping submodule due to ignore=all`: the command reads each submodule's `ignore` first, this machine's
    setting before `.gitmodules`, leaves an untracked one's pointer out, and says the root has nothing to record."""
    text = (harness.HARNESS_DIR / "commands" / f"{command}.md").read_text()
    front = yaml.safe_load(text.split("---\n")[1])
    assert "Bash(git -C * config:*)" in front["allowed-tools"]
    steps = text.split("---\n", 2)[2]
    read = steps.index('git -C <root> config --get "submodule.<name>.ignore"')
    assert read < steps.index("git add <submodule_path>")
    assert steps.index(".gitmodules", read) > read
    assert "nothing to record" in steps
    assert "never `git add -f`" in steps


def test_commit_all_stops_on_files_another_session_already_staged() -> None:
    """`/commit-all` once committed another session's staged, unfinished files while both were on `dev/main`: it now
    reads every index before it stages anything, and a non-empty one stops it until the user answers."""
    steps = (harness.HARNESS_DIR / "commands" / "commit-all.md").read_text().split("---\n", 2)[2]
    check = steps.index("git -C <repo> diff --cached --name-only")
    assert check < steps.index("\n1. ") and check < steps.index("git add")
    assert "stage and commit NOTHING anywhere" in steps[check : steps.index("\n1. ")]


def test_every_section_a_command_cites_is_in_the_rules() -> None:
    """A command cites a rule as `AGENTS.md` → "<its heading>"; the rules file must hold that heading."""
    rules = (harness.HARNESS_DIR / "AGENTS.md").read_text()
    cited = {
        title
        for path in sorted((harness.HARNESS_DIR / "commands").glob("*.md"))
        for title in re.findall(r'aisland rules → "([^"]+)"', path.read_text())
    }
    assert cited, "the commands cite no rule — the pattern this test reads has changed"
    assert sorted(title for title in cited if f"**{title}" not in rules) == []


def test_the_rules_say_how_they_are_installed_and_changed() -> None:
    first = (harness.HARNESS_DIR / "AGENTS.md").read_text().splitlines()[:6]
    assert any("aisland setup" in line for line in first)


# --- setup and init install it --------------------------------------------------------------------------------


class _Stop(Exception):
    """Ends `setup_workspace` right after the steps under test; provisioning and the installs need a real machine."""


def test_setup_installs_the_harness_before_provisioning(
    tmp_path: Path, shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(manager, "link_project_bashrc", lambda root: None)

    def provision(root: Path, aislands_own: bool = True) -> None:
        raise _Stop

    monkeypatch.setattr("aisland.setup.provision.install", provision)
    with pytest.raises(_Stop):
        manager.setup_workspace(non_interactive=True)
    assert (tmp_path / ".agents/workflows/ship.md").is_file()
    assert (tmp_path / "AGENTS.md").read_text().startswith("@.agents/rules.md\n")


def test_setup_prints_what_the_harness_reports(
    tmp_path: Path, shipped: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    _write(tmp_path / "AGENTS.md", "# ours\n")
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(manager, "link_project_bashrc", lambda root: None)
    monkeypatch.setattr(
        "aisland.setup.provision.install", lambda root, aislands_own=True: (_ for _ in ()).throw(_Stop())
    )
    with pytest.raises(_Stop):
        manager.setup_workspace(non_interactive=True)
    assert (
        '  - AGENTS.md does not import the harness\'s rules — add "@.agents/rules.md" as its first line'
        in capsys.readouterr().out.splitlines()
    )
