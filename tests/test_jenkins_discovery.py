"""Tests for the auto-discovery helpers that replaced the hardcoded lists of internal projects."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aisland import ci_scaffold
from aisland.ci_scaffold import discover_github_projects, is_private_repo
from aisland.github import GitHubRepository


def _write_submodule(workspace: Path, name: str, url: str, pyproject_name: str | None = "pkg") -> None:
    """Add a submodule entry to .gitmodules and (optionally) create its pyproject.toml."""
    gitmodules = workspace / ".gitmodules"
    entry = f'[submodule "{name}"]\n\tpath = {name}\n\turl = {url}\n'
    if gitmodules.exists():
        gitmodules.write_text(gitmodules.read_text() + entry)
    else:
        gitmodules.write_text(entry)
    if pyproject_name is not None:
        sub = workspace / name
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "pyproject.toml").write_text(f'[project]\nname = "{pyproject_name}"\nversion = "0.1.0"\n')


class TestDiscoverGithubProjects:
    def test_returns_empty_when_no_gitmodules(self, tmp_path: Path) -> None:
        assert list(discover_github_projects(tmp_path)) == []

    def test_returns_empty_when_gitmodules_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".gitmodules").write_text("")
        assert list(discover_github_projects(tmp_path)) == []

    def test_includes_ssh_github_submodule(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "confluid", "git@github.com:acme/confluid.git", "confluid")
        assert list(discover_github_projects(tmp_path)) == ["confluid"]

    def test_includes_https_github_submodule(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "purifai", "https://github.com/acme/purifai.git", "purifai")
        assert list(discover_github_projects(tmp_path)) == ["purifai"]

    def test_a_submodule_of_any_github_owner_is_included_with_its_repository(self, tmp_path: Path) -> None:
        """No organisation is written into aisland: the owner is the remote's."""
        _write_submodule(tmp_path, "other", "git@github.com:Someone/other.git", "other")
        assert discover_github_projects(tmp_path) == {"other": GitHubRepository("Someone", "other")}

    def test_excludes_bitbucket_submodule(self, tmp_path: Path) -> None:
        """A submodule whose remote is Bitbucket, not GitHub, is excluded."""
        _write_submodule(
            tmp_path,
            "navigaitor",
            "https://bitbucket.example.com/scm/x/navigaitor.git",
            "navigaitor",
        )
        assert list(discover_github_projects(tmp_path)) == []

    def test_skips_submodule_without_pyproject(self, tmp_path: Path) -> None:
        _write_submodule(
            tmp_path,
            "confluid",
            "git@github.com:acme/confluid.git",
            pyproject_name=None,
        )
        assert list(discover_github_projects(tmp_path)) == []

    def test_a_distribution_named_unlike_its_folder_is_listed_by_its_distribution(self, tmp_path: Path) -> None:
        """What a consumer lists as a dependency is the distribution, so that is the name returned."""
        _write_submodule(tmp_path, "acme", "git@github.com:acme/acme.git", "acme-tools")
        assert list(discover_github_projects(tmp_path)) == ["acme-tools"]

    def test_returns_sorted(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "zzz", "git@github.com:acme/zzz.git", "zzz")
        _write_submodule(tmp_path, "aaa", "git@github.com:acme/aaa.git", "aaa")
        _write_submodule(tmp_path, "mmm", "git@github.com:acme/mmm.git", "mmm")
        assert list(discover_github_projects(tmp_path)) == ["aaa", "mmm", "zzz"]

    def test_url_without_dot_git_suffix(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "confluid", "git@github.com:acme/confluid", "confluid")
        assert list(discover_github_projects(tmp_path)) == ["confluid"]

    def test_skips_malformed_pyproject_silently(self, tmp_path: Path) -> None:
        """Garbage TOML is ignored, not raised — other submodules still discovered."""
        _write_submodule(tmp_path, "good", "git@github.com:acme/good.git", "good")
        _write_submodule(tmp_path, "bad", "git@github.com:acme/bad.git", pyproject_name=None)
        (tmp_path / "bad").mkdir(exist_ok=True)
        (tmp_path / "bad" / "pyproject.toml").write_text("not = valid = toml = at all")
        assert list(discover_github_projects(tmp_path)) == ["good"]

    def test_skips_pyproject_missing_project_name(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "noname", "git@github.com:acme/noname.git", pyproject_name=None)
        (tmp_path / "noname").mkdir(exist_ok=True)
        (tmp_path / "noname" / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools"]\n')
        assert list(discover_github_projects(tmp_path)) == []


class TestIsPrivateRepo:
    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        """lru_cache leaks across tests; clear before each."""
        is_private_repo.cache_clear()

    def test_returns_true_when_gh_says_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="true\n", stderr="")

        monkeypatch.setattr(ci_scaffold.subprocess, "run", fake_run)
        assert is_private_repo(GitHubRepository("acme", "navigaitor")) is True

    def test_returns_false_when_gh_says_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="false\n", stderr="")

        monkeypatch.setattr(ci_scaffold.subprocess, "run", fake_run)
        assert is_private_repo(GitHubRepository("acme", "purifai")) is False

    def test_returns_true_when_gh_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Conservative fallback: assume private so the Mint-GitHub-App-token step survives."""

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("gh not installed")

        monkeypatch.setattr(ci_scaffold.subprocess, "run", fake_run)
        assert is_private_repo(GitHubRepository("acme", "anything")) is True

    def test_returns_true_on_subprocess_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Conservative fallback: assume private on probe failure."""

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.CalledProcessError(returncode=1, cmd=args[0])

        monkeypatch.setattr(ci_scaffold.subprocess, "run", fake_run)
        assert is_private_repo(GitHubRepository("acme", "nonexistent")) is True

    def test_returns_true_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Conservative fallback: assume private on probe timeout."""

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

        monkeypatch.setattr(ci_scaffold.subprocess, "run", fake_run)
        assert is_private_repo(GitHubRepository("acme", "slow")) is True

    def test_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two calls for the same name only invoke gh once (lru_cache)."""
        call_count = {"n": 0}

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="true\n", stderr="")

        monkeypatch.setattr(ci_scaffold.subprocess, "run", fake_run)
        is_private_repo(GitHubRepository("acme", "xx"))
        is_private_repo(GitHubRepository("acme", "xx"))
        is_private_repo(GitHubRepository("acme", "xx"))
        assert call_count["n"] == 1
