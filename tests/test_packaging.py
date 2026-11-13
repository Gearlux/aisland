"""The wheel carries every file the package reads or runs, not only its modules.

setuptools puts only ``*.py`` files into a wheel unless ``[tool.setuptools.package-data]`` names
the others. A file the package reads at run time (the workspace-file template) or runs (the git
alias scripts) that is missing from the wheel works in the editable checkout and fails only once
aisland is installed from a wheel — so the wheel is built and its members checked here.
"""

import email
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.message import Message
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

from aisland.setup import editor_files

PROJECT = Path(__file__).resolve().parents[1]
PACKAGE = PROJECT / "aisland"


def _package_files_that_are_not_modules() -> List[str]:
    """Every file of the package a wheel needs that is not a module, as its path inside the wheel."""
    return sorted(
        path.relative_to(PROJECT).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.suffix not in {".py", ".pyc"} and "__pycache__" not in path.parts
    )


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel from a copy of the checkout (a build in place leaves `build/` behind)."""
    source = tmp_path_factory.mktemp("source")
    for name in ("pyproject.toml", "setup.py", "project.bashrc"):
        shutil.copy(PROJECT / name, source / name)
    shutil.copytree(PACKAGE, source / "aisland", ignore=shutil.ignore_patterns("__pycache__"))
    dist = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", str(dist), str(source)],
        check=True,
        capture_output=True,
    )
    (built,) = dist.glob("*.whl")
    return built


@pytest.fixture(scope="module")
def wheel_members(wheel: Path) -> List[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def test_the_package_holds_the_templates_and_the_git_scripts() -> None:
    assert editor_files.CODE_WORKSPACE_TEMPLATE.parent == PACKAGE
    assert _package_files_that_are_not_modules() == [
        "aisland/code-workspace.json",
        "aisland/git/git_folder_all.sh",
        "aisland/git/git_pull_all.sh",
        "aisland/git/git_push_all.sh",
        "aisland/git/setup_git.sh",
        "aisland/harness/AGENTS.md",
        "aisland/harness/commands/commit-all.md",
        "aisland/harness/commands/commit-session.md",
        "aisland/harness/commands/list-actions.md",
        "aisland/harness/commands/pull-all.md",
        "aisland/harness/commands/push-all.md",
        "aisland/harness/commands/ship.md",
        "aisland/harness/harness.toml",
        "aisland/py.typed",
        "aisland/templates/ci/Jenkinsfile.jinja",
        "aisland/templates/ci/Jenkinsfile.local.jinja",
        "aisland/templates/ci/_gh_node_job.yml.jinja",
        "aisland/templates/ci/_gh_preinstall.yml.jinja",
        "aisland/templates/ci/_gh_slow_job.yml.jinja",
        "aisland/templates/ci/_gh_token.yml.jinja",
        "aisland/templates/ci/_jenkins_preinstall.groovy.jinja",
        "aisland/templates/ci/_limited.groovy.jinja",
        "aisland/templates/ci/_local_node_stage.groovy.jinja",
        "aisland/templates/ci/_local_slow_param.groovy.jinja",
        "aisland/templates/ci/_local_slow_stage.groovy.jinja",
        "aisland/templates/ci/_node_stage.groovy.jinja",
        "aisland/templates/ci/_slow_stage.groovy.jinja",
        "aisland/templates/ci/_venv_check.groovy.jinja",
        "aisland/templates/ci/ci.yml.jinja",
        "aisland/templates/ci/template.toml",
        "aisland/templates/jenkins/local_job.xml.jinja",
        "aisland/templates/jenkins/multibranch.xml.jinja",
        "aisland/templates/jenkins/nightly_check.xml.jinja",
        "aisland/templates/jenkins/orchestrator.xml.jinja",
        "aisland/templates/swiftbar/gh_status.sh",
        "aisland/templates/swiftbar/jenkins_status.sh",
        "aisland/templates/test_env_example.py.template",
        "aisland/templates/vscode-init-bash.sh",
    ]


def test_the_wheel_carries_every_file_of_the_package(wheel_members: List[str]) -> None:
    missing = [name for name in _package_files_that_are_not_modules() if name not in wheel_members]
    assert missing == []


def test_the_wheel_carries_the_harness_script_though_its_folder_is_no_package(wheel_members: List[str]) -> None:
    """`harness/` has no `__init__.py`, so its `.py` helper reaches the wheel only as package data."""
    assert "aisland/harness/scripts/stage_hunks.py" in wheel_members


def test_the_wheel_carries_the_shell_init_beside_the_package(wheel_members: List[str]) -> None:
    """`project.bashrc` stays in the checkout's `aisland/` (its place there defines the workspace root), so the
    build copies it into the package for a wheel."""
    assert "aisland/project.bashrc" in wheel_members
    assert "aisland/project.bashrc" not in _package_files_that_are_not_modules()


@pytest.fixture(scope="module")
def release_build(tmp_path_factory: pytest.TempPathFactory) -> Tuple[Path, str]:
    """The source package and the wheel built the way a release builds them: ``python -m build`` makes the source
    package, then the wheel FROM it — so a file the build needs that the source package leaves out fails here, not
    in the release workflow. Built from the files git would release (tracked, or new and not ignored). Returns the
    folder holding both and everything the build printed."""
    listed = subprocess.run(
        ["git", "-C", str(PROJECT), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    source = tmp_path_factory.mktemp("release-source")
    for name in listed:
        if (PROJECT / name).is_file():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(PROJECT / name, source / name)
    dist = tmp_path_factory.mktemp("release-dist")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(dist), str(source)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return dist, result.stdout + result.stderr


@pytest.fixture(scope="module")
def released(release_build: Tuple[Path, str]) -> Path:
    return release_build[0]


@pytest.fixture(scope="module")
def release_metadata(released: Path) -> Message:
    """The wheel's ``METADATA``: what PyPI shows for the release."""
    (built,) = released.glob("*.whl")
    with zipfile.ZipFile(built) as archive:
        (name,) = [member for member in archive.namelist() if member.endswith(".dist-info/METADATA")]
        return email.message_from_bytes(archive.read(name))


def test_a_release_build_puts_the_shell_init_into_the_wheel(released: Path) -> None:
    """`setup.py` copies `project.bashrc` into the wheel, so the source package must carry it (`MANIFEST.in`)."""
    (source_package,) = released.glob("*.tar.gz")
    (built,) = released.glob("*.whl")
    with zipfile.ZipFile(built) as archive:
        assert archive.read("aisland/project.bashrc") == (PROJECT / "project.bashrc").read_bytes()
    assert source_package.name.startswith("aisland-")
    assert built.name.startswith("aisland-")


def test_a_release_build_prints_no_deprecation_warning(release_build: Tuple[Path, str]) -> None:
    """A deprecated spelling in `pyproject.toml` (the `license = {text = ...}` table) builds today and stops building
    on the date setuptools names."""
    _, log = release_build
    assert "Deprecat" not in log


def test_twine_accepts_the_release_strictly(released: Path) -> None:
    """PyPI's own check: a warning (a missing long description, an unknown content type) fails the upload step."""
    result = subprocess.run(
        [sys.executable, "-m", "twine", "check", "--strict", *sorted(str(path) for path in released.iterdir())],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_release_page_shows_the_readme_as_markdown(release_metadata: Message) -> None:
    assert release_metadata["Description-Content-Type"] == "text/markdown"
    description = release_metadata.get_payload(decode=True)
    assert isinstance(description, bytes)
    assert description.decode("utf-8").strip() == (PROJECT / "README.md").read_text(encoding="utf-8").strip()


def test_the_licence_is_an_spdx_expression_and_no_licence_classifier(release_metadata: Message) -> None:
    """PEP 639: the `License-Expression` field replaces the `License ::` classifiers, which setuptools then warns on."""
    assert release_metadata["License-Expression"] == "MIT"
    assert [c for c in release_metadata.get_all("Classifier", []) if c.startswith("License ::")] == []


def test_the_release_names_its_author_and_where_it_lives(release_metadata: Message) -> None:
    assert release_metadata["Author"] == "Gert Behiels"
    urls = dict(entry.split(", ", 1) for entry in release_metadata.get_all("Project-URL", []))
    assert urls == {
        "Homepage": "https://github.com/Gearlux/aisland",
        "Repository": "https://github.com/Gearlux/aisland.git",
        "Issues": "https://github.com/Gearlux/aisland/issues",
        "Changelog": f"https://github.com/Gearlux/aisland/blob/v{release_metadata['Version']}/CHANGELOG.md",
    }


def test_the_mcp_sdk_is_held_below_its_next_major_version() -> None:
    """`aisland-mcp` drops the SDK's resources and prompts handlers through a private attribute (`server.py`): a
    3.x SDK may move it, so neither the `mcp` extra nor `dev` admits one."""
    extras = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]
    for extra in ("mcp", "dev"):
        [spec] = [Requirement(r).specifier for r in extras[extra] if Requirement(r).name == "mcp"]
        assert spec.contains("2.9") and not spec.contains("3.0"), (extra, str(spec))


def test_the_development_status_follows_the_version() -> None:
    """The workspace's convention: a pre-release (`a`, `b`, `rc`) is Alpha, a final release Beta."""
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    status = [c for c in project["classifiers"] if c.startswith("Development Status ::")]
    is_prerelease = any(marker in project["version"] for marker in ("a", "b", "rc"))
    assert status == ["Development Status :: " + ("3 - Alpha" if is_prerelease else "4 - Beta")]


def test_the_python_classifiers_follow_requires_python() -> None:
    """Every `Programming Language :: Python :: 3.N` classifier is a version `requires-python` admits, the floor
    included."""
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    floor = project["requires-python"].removeprefix(">=")
    versions = [
        c.rsplit(" :: ", 1)[1] for c in project["classifiers"] if c.startswith("Programming Language :: Python :: 3.")
    ]
    assert floor in versions
    assert all(tuple(map(int, v.split("."))) >= tuple(map(int, floor.split("."))) for v in versions)


# --- the wheel, run in a workspace that is not this one --------------------------------------------------------


@pytest.fixture(scope="module")
def installed(wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The wheel unpacked into a folder of its own — a pure-Python wheel installs by unpacking."""
    site = tmp_path_factory.mktemp("site")
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    return site


def _run(installed: Path, cwd: Path, *args: str, env: Optional[Dict[str, str]] = None) -> CompletedProcess[str]:
    """This interpreter (for click and PyYAML) with the unpacked wheel first on the path, never this checkout."""
    environment = {k: v for k, v in os.environ.items() if k != "AISLAND_WORKSPACE"}
    environment.update({"PYTHONPATH": str(installed), **(env or {})})
    return subprocess.run([sys.executable, "-P", *args], cwd=cwd, env=environment, capture_output=True, text=True)


def _aisland(installed: Path, cwd: Path, *args: str) -> CompletedProcess[str]:
    return _run(installed, cwd, "-c", "from aisland.cli import main; main()", *args)


@pytest.fixture
def acme(tmp_path: Path) -> Path:
    root = tmp_path / "acme"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


_REPORT = """
import json
from aisland import locations, paths, pytest_stats
from aisland.services import supervisor
from aisland.setup import editor_files
from aisland.workspace import projects
print(json.dumps({
    "package": locations.PACKAGE_DIR.as_posix(),
    "from_checkout": locations.FROM_CHECKOUT,
    "root": projects.get_workspace_root().as_posix(),
    "data": paths.data_dir().as_posix(),
    "bin": paths.bin_dir().as_posix(),
    "supervisor": supervisor.config_file().parent.as_posix(),
    "stats": pytest_stats.stats_folder().as_posix(),
    "template": editor_files.CODE_WORKSPACE_TEMPLATE.is_file(),
}))
"""


def test_from_the_wheel_the_workspace_is_where_it_runs_and_the_state_goes_into_it(installed: Path, acme: Path) -> None:
    (acme / "models").mkdir()
    result = _run(installed, acme / "models", "-c", _REPORT)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    root = acme.resolve().as_posix()
    assert report == {
        "package": (installed / "aisland").resolve().as_posix(),
        "from_checkout": False,
        "root": root,
        "data": f"{root}/.aisland/data",
        "bin": f"{root}/.aisland/bin",
        "supervisor": f"{root}/.aisland/data",
        "stats": f"{root}/.aisland/data/test-stats",
        "template": True,
    }


def test_from_the_wheel_outside_a_workspace_a_command_is_refused_with_the_way_out(
    installed: Path, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _aisland(installed, plain, "status")
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"Error: No workspace here: {plain.resolve()} is in no git repository and holds no project. Run aisland in "
        "your workspace or one of its projects, set AISLAND_WORKSPACE to it, or create one with "
        "`aisland workspace init <folder>`.\n"
    )


def test_from_the_wheel_outside_a_workspace_the_doctor_checks_this_machine(installed: Path, tmp_path: Path) -> None:
    """The doctor checks the machine and the Python it runs on, so it needs no workspace: a remote box that only
    runs the tunnel has none."""
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _aisland(installed, plain, "doctor")
    assert "No workspace here" not in result.stderr
    assert result.stdout.startswith("Environment doctor"), result.stdout + result.stderr
    assert result.stdout.strip().splitlines()[-1].endswith("checks passed.") or "checks failed." in result.stdout


def test_from_the_wheel_outside_a_workspace_a_notebook_is_cleaned(installed: Path, tmp_path: Path) -> None:
    """`aisland notebooks clean` is a git filter reading standard input: it runs in any repository."""
    plain = tmp_path / "plain"
    plain.mkdir()
    notebook = {
        "cells": [
            {"cell_type": "code", "execution_count": 3, "metadata": {}, "outputs": [{"text": "4"}], "source": []}
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    environment = {k: v for k, v in os.environ.items() if k != "AISLAND_WORKSPACE"}
    environment["PYTHONPATH"] = str(installed)
    result = subprocess.run(
        [sys.executable, "-P", "-c", "from aisland.cli import main; main()", "notebooks", "clean"],
        cwd=plain,
        env=environment,
        input=json.dumps(notebook),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    cell = json.loads(result.stdout)["cells"][0]
    assert (cell["outputs"], cell["execution_count"]) == ([], None)


def test_from_the_wheel_a_workspace_is_created_and_its_aliases_point_into_the_package(
    installed: Path, tmp_path: Path
) -> None:
    result = _aisland(installed, tmp_path, "workspace", "init", "acme")
    assert result.returncode == 0, result.stdout + result.stderr
    root = tmp_path / "acme"
    assert (root / ".git").is_dir()
    alias = subprocess.run(
        ["git", "config", "--local", "alias.pull-all"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    assert str((installed / "aisland" / "git" / "git_pull_all.sh").resolve()) in alias


def test_from_the_wheel_a_new_workspace_gets_copies_of_the_agent_harness(installed: Path, tmp_path: Path) -> None:
    """From a wheel the commands and rules are COPIES: a link would point into site-packages, which a reinstall of
    the venv replaces."""
    result = _aisland(installed, tmp_path, "workspace", "init", "acme")
    assert result.returncode == 0, result.stdout + result.stderr
    root = tmp_path / "acme"
    for name in (".agents/workflows/ship.md", ".agents/scripts/stage_hunks.py", ".agents/aisland/AGENTS.md"):
        assert (root / name).is_file() and not (root / name).is_symlink(), name
    assert (root / ".agents/workflows/ship.md").read_bytes() == (PACKAGE / "harness/commands/ship.md").read_bytes()
    assert (root / ".claude/commands/commit-session.md").is_file()


# --- the release's files: licence, changelog, typing marker, the release workflow ---------------------------------


def test_the_source_package_carries_the_licence_and_the_changelog(released: Path) -> None:
    """`license-files` brings LICENSE; nothing references CHANGELOG.md, so `MANIFEST.in` names it."""
    (source_package,) = released.glob("*.tar.gz")
    with tarfile.open(source_package) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    assert {"LICENSE", "CHANGELOG.md"} <= names


def test_the_source_package_carries_no_part_of_the_test_suite(released: Path) -> None:
    """A partial suite (the test files without `conftest.py`, its helpers, `docs/` or `examples/`) cannot run; the
    source package is for building the wheel."""
    (source_package,) = released.glob("*.tar.gz")
    with tarfile.open(source_package) as archive:
        names = [name.split("/", 1)[1] for name in archive.getnames() if "/" in name]
    assert [name for name in names if name.startswith("tests/")] == []
    assert "aisland/cli/__init__.py" in names  # the package itself is all there


def test_the_wheel_carries_the_licence_and_the_typing_marker(released: Path) -> None:
    (built,) = released.glob("*.whl")
    with zipfile.ZipFile(built) as archive:
        names = archive.namelist()
    assert "aisland/py.typed" in names
    assert [name for name in names if name.endswith(".dist-info/licenses/LICENSE")] != []


def test_a_typed_package_says_so() -> None:
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert (PACKAGE / "py.typed").is_file()
    assert "Typing :: Typed" in project["classifiers"]


def _released_versions(changelog: str) -> List[str]:
    """The `## [<version>]` headings, newest first; an `## [Unreleased]` section is no version."""
    headings = re.findall(r"^## \[([^\]]+)\]", changelog, flags=re.MULTILINE)
    return [heading for heading in headings if heading.lower() != "unreleased"]


def test_an_unreleased_section_is_not_a_version() -> None:
    assert _released_versions("## [Unreleased]\n\n- a change\n\n## [0.1.0] - 2026-10-24\n") == ["0.1.0"]
    assert _released_versions("## [0.2.0] - 2026-11-01\n\n## [0.1.0] - 2026-10-24\n") == ["0.2.0", "0.1.0"]
    assert _released_versions("## [Unreleased]\n\n- a change\n") == []


def test_the_newest_changelog_entry_is_the_declared_version() -> None:
    """The release workflow checks the tag against `pyproject.toml`; this checks the CHANGELOG against both."""
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    versions = _released_versions((PROJECT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert versions, "CHANGELOG.md has no `## [<version>]` entry"
    assert versions[0] == project["version"]


RELEASE_WORKFLOW = PROJECT / ".github" / "workflows" / "release.yml"


# A parsed YAML document: its keys are not all strings (PyYAML reads the bare key `on` as True).
Workflow = Dict[Any, Any]


@pytest.fixture(scope="module")
def release_workflow() -> Workflow:
    loaded: Workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def _step(workflow: Workflow, job: str, name: str) -> Workflow:
    step: Workflow
    (step,) = [step for step in workflow["jobs"][job]["steps"] if step.get("name") == name]
    return step


def test_a_release_runs_on_a_version_tag_only(release_workflow: Workflow) -> None:
    # PyYAML reads the bare key `on` as True.
    assert release_workflow[True] == {"push": {"tags": ["v*"]}}


def test_the_release_publishes_by_trusted_publishing_from_the_pypi_environment(
    release_workflow: Workflow,
) -> None:
    publish = release_workflow["jobs"]["publish"]
    assert publish["needs"] == ["build"]
    assert publish["environment"] == "pypi"
    assert publish["permissions"] == {"id-token": "write"}
    assert [step["uses"] for step in publish["steps"] if "uses" in step][-1] == "pypa/gh-action-pypi-publish@release/v1"
    assert "password" not in str(publish)


@pytest.mark.parametrize("tag, accepted", [("v0.1.0", True), ("v0.1.1", False), ("0.1.0", False)])
def test_the_tag_guard_refuses_a_tag_that_is_not_the_version(
    release_workflow: Workflow, tmp_path: Path, tag: str, accepted: bool
) -> None:
    """The guard step's own script, run against a pyproject.toml declaring 0.1.0."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "aisland"\nversion = "0.1.0"\n')
    script = _step(release_workflow, "guards", "Guard - tag matches pyproject version")["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script.replace("python ", f"{sys.executable} ")],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF_NAME": tag},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


@pytest.mark.parametrize(
    "heading, accepted",
    [
        ("## [0.1.0] - 2026-11-03", True),
        ("## [0.1.0] - 2026-11-02", False),
        ("## [0.1.0] - Unreleased", False),
        ("## [0.1.0]", False),
        ("## [0.1.01] - 2026-11-03", False),
        ("## [0.2.0] - 2026-11-03", False),
    ],
)
def test_the_changelog_guard_refuses_a_version_the_changelog_does_not_date_as_its_tag(
    release_workflow: Workflow, tmp_path: Path, heading: str, accepted: bool
) -> None:
    """Keep a Changelog gives a release its date: the tag `v0.1.0` is refused until the entry carries the day of the
    commit it tags (here 2026-11-03), so a date written ahead of time, or left from an earlier attempt, never ships."""
    (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n## [Unreleased]\n\n{heading}\n\n- a change\n")
    when = {"GIT_COMMITTER_DATE": "2026-11-03T12:00:00+00:00", "GIT_AUTHOR_DATE": "2026-11-03T12:00:00+00:00"}
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [*git, "commit", "-q", "--allow-empty", "-m", "release"], cwd=tmp_path, check=True, env={**os.environ, **when}
    )
    script = _step(release_workflow, "guards", "Guard - the CHANGELOG dates this version")["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF_NAME": "v0.1.0"},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


@pytest.mark.parametrize("tagged, accepted", [("main", True), ("dev/main", False)])
def test_the_main_guard_refuses_a_tag_on_a_commit_main_does_not_hold(
    release_workflow: Workflow, tmp_path: Path, tagged: str, accepted: bool
) -> None:
    """A release is made from `main`: the guard's own script, run in a clone whose origin's `main` lags `dev/main`
    by one commit, passes on main's commit and refuses the one only `dev/main` holds."""
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    origin, clone = tmp_path / "origin", tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "released"], cwd=origin, check=True)
    subprocess.run(["git", "switch", "-q", "-c", "dev/main"], cwd=origin, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "not merged"], cwd=origin, check=True)
    subprocess.run(["git", "clone", "-q", "-b", tagged, str(origin), str(clone)], check=True)
    script = _step(release_workflow, "guards", "Guard - the tagged commit is on main")["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=clone,
        env={**os.environ, "GITHUB_REF_NAME": "v0.1.0"},
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr


def test_the_release_tests_every_python_the_classifiers_claim(release_workflow: Workflow) -> None:
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    version = re.compile(r"Programming Language :: Python :: (3\.\d+)")
    claimed = [found[1] for found in map(version.fullmatch, project["classifiers"]) if found]
    assert release_workflow["jobs"]["tests"]["strategy"]["matrix"]["python"] == claimed


def test_the_release_is_built_only_after_the_gates_and_every_test_pass(release_workflow: Workflow) -> None:
    """A tag pushed from a commit CI never checked (or checked red) must not reach PyPI: the build waits on a job
    that runs the quality gates and both test runs, fast and slow, under a time limit."""
    jobs = release_workflow["jobs"]
    assert jobs["build"]["needs"] == ["tests"]
    assert jobs["tests"]["needs"] == ["guards"], "a wrong tag is refused before the tests' 45 minutes"
    tests = jobs["tests"]
    assert 0 < tests["timeout-minutes"] <= 60
    runs = [step["run"].strip() for step in tests["steps"] if "run" in step]
    assert runs == [
        'uv pip install --system -e ".[dev]"',
        "isort --check-only .",
        "black --check .",
        "flake8 .",
        "mypy .",
        "pytest tests -m ''",
    ]


def test_every_release_job_has_a_time_limit(release_workflow: Workflow) -> None:
    """Never GitHub's six hours: the tests as long as the slow run allows, the build and the upload a few minutes."""
    jobs = release_workflow["jobs"]
    assert {name: job.get("timeout-minutes") for name, job in jobs.items()} == {
        "guards": 5,
        "tests": 45,
        "build": 15,
        "publish": 10,
    }


def test_the_release_builds_with_a_toolchain_of_exact_versions(release_workflow: Workflow) -> None:
    """`pip install build twine` and an isolated build took the newest of each, so one tag could build other files."""
    install = _step(release_workflow, "build", "Install packaging toolchain")["run"].split()
    assert install[:3] == ["python", "-m", "pip"] and install[3] == "install"
    pinned = dict(requirement.split("==") for requirement in install[4:])
    assert set(pinned) == {"build", "twine", "setuptools", "wheel"} and all(pinned.values())


def test_the_release_token_only_reads_and_the_tests_keep_none(release_workflow: Workflow) -> None:
    """The tests run every dependency's code for 45 minutes: the workflow's token reads, and checkout leaves it out
    of `.git/config` there."""
    assert release_workflow["permissions"] == {"contents": "read"}
    tests = release_workflow["jobs"]["tests"]
    (checkout,) = [step for step in tests["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
    assert checkout["with"]["persist-credentials"] is False


def test_the_release_tests_run_once_on_macos_where_bash_is_3_2(release_workflow: Workflow) -> None:
    matrix = release_workflow["jobs"]["tests"]["strategy"]["matrix"]
    assert {"python": "3.13", "os": "macos-latest"} in matrix["include"]
    assert release_workflow["jobs"]["tests"]["runs-on"] == "${{ matrix.os }}"


def test_the_release_checks_the_build_strictly_and_smoke_tests_the_wheel(release_workflow: Workflow) -> None:
    assert _step(release_workflow, "build", "Build sdist + wheel")["run"].strip() == "python -m build --no-isolation"
    assert _step(release_workflow, "build", "Check package metadata")["run"].strip() == (
        "python -m twine check --strict dist/*"
    )
    smoke = _step(release_workflow, "build", "Smoke-test the built wheel in a clean venv")["run"]
    assert "cd /tmp" in smoke and "dist/*.whl" in smoke and "aisland --help" in smoke
    assert 'aisland --version | grep -qx "aisland, version ${GITHUB_REF_NAME#v}"' in smoke


# --- the dependency floors ------------------------------------------------------------------------------------------

#: Imports every module of the installed package (none of this checkout: ``-P``, run from a scratch folder) and uses
#: each base dependency the way aisland does; prints the modules that fail.
_FLOOR_SMOKE = """
import importlib, json, pkgutil
import aisland
from aisland.workspace import projects
from aisland import pypi
from aisland.services import supervisor
failures = {}
for module in pkgutil.walk_packages(aisland.__path__, "aisland."):
    if module.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(module.name)
    except Exception as error:
        failures[module.name] = f"{type(error).__name__}: {error}"
assert projects.requirement_name("Lib_X[extra]>=1.0") == "lib-x"
assert pypi.Version("1.2.0") > pypi.Version("1.2.0rc1")
print(json.dumps(failures))
"""


def _reach_pypi() -> None:
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
    except OSError as error:
        pytest.skip(f"PyPI cannot be reached ({error}): the floors are resolved from it")


def _lowest(venv: Path, requirements: List[str], cwd: Path) -> None:
    """Install ``requirements`` into ``venv``, each named one at the LOWEST version its specifier allows
    (``--resolution lowest-direct``; their own dependencies at the newest). From a scratch folder with ``--no-config``,
    so no ``uv.toml`` of a workspace (its indexes, its overrides) is read."""
    uv = str(Path(sys.executable).with_name("uv"))
    environment = {k: v for k, v in os.environ.items() if not k.startswith("UV_") or k == "UV_CACHE_DIR"}
    environment.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        [uv, "pip", "install", "--no-config", "--python", str(venv / "bin" / "python")]
        + ["--resolution", "lowest-direct", *requirements],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.slow
def test_aisland_runs_on_the_lowest_versions_its_dependencies_allow(wheel: Path, tmp_path: Path) -> None:
    """Each floor in ``[project] dependencies`` and the runtime extras is a version aisland was run on: the wheel
    installed with every one of them at its floor imports every module and answers ``--help``.

    The only test that reaches the network (PyPI); it is skipped, saying so, where PyPI cannot be reached. The
    floors' suite run is measured by hand (CHANGELOG, pyproject comments); this keeps them from going stale."""
    _reach_pypi()
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    uv = str(Path(sys.executable).with_name("uv"))
    venv = tmp_path / "lowest"
    subprocess.run([uv, "venv", "--no-config", "-q", "--python", sys.executable, str(venv)], cwd=tmp_path, check=True)
    python = str(venv / "bin" / "python")

    _lowest(venv, [str(wheel), *project["dependencies"]], tmp_path)
    for name in project["dependencies"]:
        requirement = Requirement(name)
        (floor,) = [spec.version for spec in requirement.specifier if spec.operator == ">="]
        assert Version(_installed_version(python, requirement.name, tmp_path)) == Version(floor), requirement
    smoke = subprocess.run([python, "-P", "-c", _FLOOR_SMOKE], cwd=tmp_path, capture_output=True, text=True)
    assert smoke.returncode == 0, smoke.stderr
    assert json.loads(smoke.stdout.strip().splitlines()[-1]) == {}
    helped = subprocess.run([str(venv / "bin" / "aisland"), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert helped.returncode == 0 and "Usage: aisland" in helped.stdout, helped.stderr

    extras = [spec for extra in ("connect", "mcp", "prune") for spec in project["optional-dependencies"][extra]]
    _lowest(venv, [str(wheel), *extras], tmp_path)
    served = subprocess.run(
        [python, "-P", "-c", "import msgpack, git_filter_repo; from aisland import server; assert server.mcp_server"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert served.returncode == 0, served.stderr


def _installed_version(python: str, distribution: str, cwd: Path) -> str:
    result = subprocess.run(
        [python, "-P", "-c", f"import importlib.metadata as m; print(m.version({distribution!r}))"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()
