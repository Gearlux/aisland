"""A workspace's projects are its own by their GitHub remote, whoever owns it — no organisation is written into aisland.

A project is one of the workspace's when its remote (``.gitmodules``, or a clone's ``origin``) is on github.com; its
owner and repository name come from that remote. A PyPI release counts as the project's when its metadata names a
``github.com/<that owner>/`` URL. The workspace below belongs to ``acme``.
"""

from __future__ import annotations

import contextlib
import io
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Set

import click
import pytest

from aisland import locations
from aisland.ci import ci_scaffold
from aisland.ci.ci_scaffold import discover_github_projects, is_private_repo
from aisland.errors import AislandError
from aisland.pypi import released_on_pypi
from aisland.services.jenkins import JenkinsService
from aisland.setup import source_commands
from aisland.wheels import published
from aisland.workspace import projects, source_apply
from aisland.workspace import sources as s
from aisland.workspace.github import GitHubRepository, github_repository, project_repositories


def _project(root: Path, name: str, deps: list[str]) -> None:
    (root / name / name).mkdir(parents=True, exist_ok=True)
    (root / name / name / "__init__.py").touch()
    listed = ", ".join(f'"{d}"' for d in deps)
    (root / name / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "0.0.0"\ndependencies = [{listed}]\n'
    )


def _gitmodules(root: Path, urls: Dict[str, str]) -> None:
    (root / ".gitmodules").write_text(
        "".join(f'[submodule "{name}"]\n\tpath = {name}\n\turl = {url}\n' for name, url in urls.items())
    )


@pytest.fixture
def acme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``app`` needs ``lib``; both are acme's, ``lib`` by its SSH remote. ``tools`` lives on a Bitbucket server."""
    _project(tmp_path, "lib", [])
    _project(tmp_path, "app", ["lib>=0.0.0"])
    _project(tmp_path, "tools", [])
    _gitmodules(
        tmp_path,
        {
            "lib": "git@github.com:acme/lib.git",
            "app": "https://github.com/acme/app.git",
            "tools": "https://bitbucket.example.com/scm/team/tools.git",
        },
    )

    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr("aisland.ci.ci_scaffold.is_private_repo", lambda repository: False)
    return tmp_path


@pytest.fixture
def pypi(monkeypatch: pytest.MonkeyPatch) -> Iterator[Dict[str, Dict[str, Any]]]:
    """PyPI's JSON answers by distribution; a name left out is not on PyPI."""
    answers: Dict[str, Dict[str, Any]] = {}

    @contextlib.contextmanager
    def _open(url: Any, timeout: int = 5) -> Any:
        if not isinstance(url, str) or "pypi.org/pypi/" not in url:  # the scaffold's Jenkins linter: no server here
            raise urllib.error.URLError("offline")
        name = url.split("/pypi/")[1].split("/")[0]
        if name not in answers:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]
        yield io.BytesIO(json.dumps(answers[name]).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _open)
    # The scaffold asks the REAL question here, of this fake PyPI (conftest stubs it for every other test).
    monkeypatch.setattr("aisland.ci.ci_scaffold.released_on_pypi", released_on_pypi)
    released_on_pypi.cache_clear()
    yield answers
    released_on_pypi.cache_clear()


def _release(repository: str) -> Dict[str, Any]:
    """PyPI's answer for version 1.0 of a package whose metadata names ``repository``."""
    return {
        "info": {"version": "1.0", "project_urls": {"Repository": repository}},
        "releases": {"1.0": [{"filename": "x.whl", "yanked": False}]},
    }


def _ci(root: Path, project: str) -> Dict[str, str]:
    base = root / project
    return {
        "Jenkinsfile": (base / "Jenkinsfile").read_text(),
        "ci.yml": (base / ".github" / "workflows" / "ci.yml").read_text(),
    }


# --- reading a remote ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:acme/lib.git",
        "git@github.com:acme/lib",
        "https://github.com/acme/lib.git",
        "https://github.com/acme/lib",
        "ssh://git@github.com/acme/lib.git",
        "https://user@github.com/acme/lib.git",
        "https://x-access-token@github.com/acme/lib",
    ],
)
def test_every_spelling_of_a_github_remote_names_its_owner_and_repository(url: str) -> None:
    repository = github_repository(url)
    assert repository == GitHubRepository("acme", "lib")
    assert repository is not None and repository.url == "https://github.com/acme/lib.git"


@pytest.mark.parametrize(
    "url",
    [
        "https://bitbucket.example.com/scm/team/tools.git",
        "https://github.com/acme/lib/extra",
        "https://github.com/acme",
        "",
    ],
)
def test_a_remote_that_is_no_github_repository_names_none(url: str) -> None:
    assert github_repository(url) is None


def test_each_project_on_github_is_found_by_its_folder_and_the_others_are_not(acme: Path) -> None:
    assert project_repositories(acme) == {
        "lib": GitHubRepository("acme", "lib"),
        "app": GitHubRepository("acme", "app"),
    }


def test_without_gitmodules_a_clones_origin_is_its_remote(tmp_path: Path) -> None:
    import subprocess

    _project(tmp_path, "lib", [])
    subprocess.run(["git", "init", "-q", str(tmp_path / "lib")], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path / "lib"), "remote", "add", "origin", "git@github.com:acme/lib.git"], check=True
    )
    assert project_repositories(tmp_path) == {"lib": GitHubRepository("acme", "lib")}


def test_a_project_folder_linked_outside_the_workspace_keeps_the_path_it_is_listed_under(tmp_path: Path) -> None:
    """A link that leads outside the root used to stop every caller with a traceback (`relative_to`)."""
    root, elsewhere = tmp_path / "ws", tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    (root / "ext").symlink_to(elsewhere)
    (root / ".gitmodules").write_text('[submodule "ext"]\n\tpath = ext\n\turl = git@github.com:acme/ext.git\n')
    assert project_repositories(root) == {"ext": GitHubRepository("acme", "ext")}


def test_the_scaffold_knows_each_github_project_by_its_distribution(acme: Path) -> None:
    assert discover_github_projects(acme) == {
        "app": GitHubRepository("acme", "app"),
        "lib": GitHubRepository("acme", "lib"),
    }


# --- the generated CI ------------------------------------------------------------------------------------------------


def test_an_unreleased_dependency_is_installed_from_its_owners_repository(acme: Path, pypi: Dict[str, Any]) -> None:
    ci_scaffold.scaffold("app", force=True, jenkins_url=JenkinsService().url())
    files = _ci(acme, "app")
    assert "uv pip install --system --no-deps git+https://github.com/acme/lib.git@main" in files["ci.yml"]
    assert 'uv pip install --no-deps git+https://github.com/acme/lib.git@main"' in files["Jenkinsfile"]
    assert "Gearlux" not in files["ci.yml"] + files["Jenkinsfile"]


def test_a_dependency_released_from_its_owners_repository_is_left_to_pypi(acme: Path, pypi: Dict[str, Any]) -> None:
    pypi["lib"] = _release("https://github.com/acme/lib")
    ci_scaffold.scaffold("app", force=True, jenkins_url=JenkinsService().url())
    assert "acme/lib.git" not in _ci(acme, "app")["ci.yml"]


def test_a_strangers_release_under_the_same_name_is_not_the_dependency(acme: Path, pypi: Dict[str, Any]) -> None:
    pypi["lib"] = _release("https://github.com/someone-else/lib")
    ci_scaffold.scaffold("app", force=True, jenkins_url=JenkinsService().url())
    assert "git+https://github.com/acme/lib.git@main" in _ci(acme, "app")["ci.yml"]


def test_a_private_dependency_gets_a_token_for_its_owner(
    acme: Path, pypi: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aisland.ci.ci_scaffold.is_private_repo", lambda repository: True)
    ci_scaffold.scaffold("app", force=True, jenkins_url=JenkinsService().url())
    ci = _ci(acme, "app")["ci.yml"]
    assert "        owner: acme\n" in ci
    assert "${{ secrets.ACME_APP_ID }}" in ci and "${{ secrets.ACME_APP_PRIVATE_KEY }}" in ci
    assert "Mint GitHub App token for private acme deps" in ci


def test_private_dependencies_of_two_owners_are_refused_naming_both(
    acme: Path, pypi: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(acme, "util", [])
    _project(acme, "app", ["lib>=0.0.0", "util>=0.0.0"])
    _gitmodules(
        acme,
        {
            "lib": "git@github.com:acme/lib.git",
            "util": "git@github.com:other-org/util.git",
            "app": "git@github.com:acme/app.git",
        },
    )
    monkeypatch.setattr("aisland.ci.ci_scaffold.is_private_repo", lambda repository: True)
    with pytest.raises(click.ClickException, match="acme, other-org"):
        ci_scaffold.scaffold("app", force=True, jenkins_url=JenkinsService().url())


def test_a_project_off_github_is_never_installed_from_github(acme: Path, pypi: Dict[str, Any]) -> None:
    _project(acme, "app", ["lib>=0.0.0", "tools>=0.0.0"])
    ci_scaffold.scaffold("app", force=True, jenkins_url=JenkinsService().url())
    assert "tools.git" not in _ci(acme, "app")["ci.yml"]


def _wheels_extra(root: Path, project: str, deps: list[str], covered: list[str]) -> None:
    """``project``'s pyproject with the block `aisland wheels build --extra wheels` writes, covering ``covered``."""
    from aisland.wheels.extra import BEGIN, END

    listed = ", ".join(f'"{d}"' for d in deps)
    wheels = ", ".join(f'"{d}"' for d in covered)
    (root / project / "pyproject.toml").write_text(
        f'[project]\nname = "{project}"\nversion = "0.0.0"\ndependencies = [{listed}]\n\n'
        f"[project.optional-dependencies]\n{BEGIN}: the `wheels` extra\nwheels = [{wheels}]\n{END}\n\n"
        '[tool.aisland]\nmanual-extras = ["wheels"]\n'
    )


def test_a_project_off_github_installs_its_unreleased_dependencies_from_its_wheels_extra(
    acme: Path, pypi: Dict[str, Any]
) -> None:
    """A project that does not live on GitHub never names one in its CI files (a company's repository must not
    reveal the workspace it is developed in): its unreleased dependencies come from the wheels it commits."""
    _wheels_extra(acme, "tools", ["lib>=0.0.0"], ["lib"])
    ci_scaffold.scaffold("tools", force=True, jenkins_url=JenkinsService().url())
    files = _ci(acme, "tools")
    for name, text in files.items():
        assert "github.com" not in text and "acme" not in text, name
    assert 'uv pip install --system -e ".[dev,wheels]"' in files["ci.yml"]
    assert 'uv pip install -e .[dev,wheels]"' in files["Jenkinsfile"]


def test_a_project_off_github_without_wheels_for_an_unreleased_dependency_is_refused_and_writes_nothing(
    acme: Path, pypi: Dict[str, Any]
) -> None:
    _project(acme, "tools", ["lib>=0.0.0"])
    with pytest.raises(click.ClickException) as refused:
        ci_scaffold.scaffold("tools", force=True, jenkins_url=JenkinsService().url())
    assert refused.value.message == (
        "tools does not live on GitHub, so its CI files never name a project that does, and lib is not released on "
        "PyPI. Ship it as wheels with the project — aisland wheels build tools --extra wheels --commit (the extra listed "
        "in [tool.aisland] manual-extras) — then scaffold again."
    )
    assert not (acme / "tools" / "Jenkinsfile").exists()


def test_a_wheels_extra_missing_an_unreleased_dependency_is_refused_naming_it(acme: Path, pypi: Dict[str, Any]) -> None:
    _project(acme, "util", [])

    (acme / ".gitmodules").write_text(
        (acme / ".gitmodules").read_text() + '[submodule "util"]\n\tpath = util\n\turl = git@github.com:acme/util.git\n'
    )
    _wheels_extra(acme, "tools", ["lib>=0.0.0", "util>=0.0.0"], ["lib"])
    with pytest.raises(click.ClickException, match="and util is not released on PyPI"):
        ci_scaffold.scaffold("tools", force=True, jenkins_url=JenkinsService().url())
    assert projects.get_workspace_root() == acme


def test_a_project_off_github_needing_only_released_dependencies_needs_no_wheels(
    acme: Path, pypi: Dict[str, Any]
) -> None:
    pypi["lib"] = _release("https://github.com/acme/lib")
    _project(acme, "tools", ["lib>=0.0.0"])
    ci_scaffold.scaffold("tools", force=True, jenkins_url=JenkinsService().url())
    assert "github.com" not in _ci(acme, "tools")["ci.yml"]


def test_the_private_check_asks_github_about_the_owners_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    asked = []

    def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        asked.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="false\n")

    monkeypatch.setattr(subprocess, "run", _run)
    is_private_repo.cache_clear()
    assert is_private_repo(GitHubRepository("acme", "lib")) is False
    assert asked == [["gh", "api", "repos/acme/lib", "--jq", ".private"]]


# --- a pypi source ---------------------------------------------------------------------------------------------------


def _set_pypi(root: Path, project: str, monkeypatch: pytest.MonkeyPatch) -> int:
    (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (root / ".venv" / "bin" / "uv").touch(mode=0o755)
    monkeypatch.setattr(s, "local_override", lambda root, name: None)
    monkeypatch.setattr(s, "set_local_override", lambda *a, **k: None)
    monkeypatch.setattr(source_apply, "apply_source", lambda *a, **k: True)
    monkeypatch.setattr("aisland.setup.source_commands.write_code_workspace", lambda *a, **k: None)
    return source_commands.set_project_source(project, "pypi", workspace_root=root)


def test_a_release_from_the_projects_own_owner_may_be_its_source(
    acme: Path, pypi: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pypi["lib"] = _release("https://github.com/acme/lib")
    assert _set_pypi(acme, "lib", monkeypatch) == 0
    assert "Verified lib 1.0 on PyPI (published from github.com/acme)." in capsys.readouterr().out


def test_a_strangers_release_may_not(
    acme: Path, pypi: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pypi["lib"] = _release("https://github.com/someone-else/lib")
    with pytest.raises(AislandError, match="does not point at github.com/acme/"):
        _set_pypi(acme, "lib", monkeypatch)


def test_a_project_off_github_cannot_be_matched_to_a_release(
    acme: Path, pypi: Dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pypi["tools"] = _release("https://bitbucket.example.com/scm/team/tools")
    with pytest.raises(AislandError, match="its remote is not on GitHub"):
        _set_pypi(acme, "tools", monkeypatch)


# --- wheels ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repository, owner, state",
    [
        ("https://github.com/acme/lib", "acme", "published"),
        ("https://github.com/ACME/lib", "acme", "published"),
        ("https://github.com/someone-else/lib", "acme", "foreign"),
        ("https://github.com/acme/lib", None, "foreign"),
    ],
)
def test_a_wheel_counts_as_on_pypi_only_from_its_projects_owner(
    pypi: Dict[str, Any], repository: str, owner: Optional[str], state: str
) -> None:
    pypi["lib"] = _release(repository)
    assert published.pypi_state("lib", owner)[0] == state


def test_released_on_pypi_asks_for_the_owner(pypi: Dict[str, Any]) -> None:
    pypi["lib"] = _release("https://github.com/acme/lib")
    released: Set[bool] = {released_on_pypi("lib", "acme"), released_on_pypi("lib", "someone-else")}
    assert released == {True, False}
