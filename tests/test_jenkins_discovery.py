"""Tests for the auto-discovery helpers that replaced the hardcoded Gearlux project lists."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aisland.services import jenkins as jenkins_mod
from aisland.services.jenkins import discover_gearlux_projects, is_private_gearlux_repo


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


class TestDiscoverGearluxProjects:
    def test_returns_empty_when_no_gitmodules(self, tmp_path: Path) -> None:
        assert discover_gearlux_projects(tmp_path) == []

    def test_returns_empty_when_gitmodules_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".gitmodules").write_text("")
        assert discover_gearlux_projects(tmp_path) == []

    def test_includes_ssh_gearlux_submodule(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "confluid", "git@github.com:Gearlux/confluid.git", "confluid")
        assert discover_gearlux_projects(tmp_path) == ["confluid"]

    def test_includes_https_gearlux_submodule(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "purifai", "https://github.com/Gearlux/purifai.git", "purifai")
        assert discover_gearlux_projects(tmp_path) == ["purifai"]

    def test_excludes_non_gearlux_github_submodule(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "other", "git@github.com:Someone/other.git", "other")
        assert discover_gearlux_projects(tmp_path) == []

    def test_excludes_bitbucket_submodule(self, tmp_path: Path) -> None:
        """A submodule whose remote is Bitbucket, not GitHub Gearlux, is excluded."""
        _write_submodule(
            tmp_path,
            "navigaitor",
            "https://bitbucket.example.com/scm/x/navigaitor.git",
            "navigaitor",
        )
        assert discover_gearlux_projects(tmp_path) == []

    def test_skips_submodule_without_pyproject(self, tmp_path: Path) -> None:
        _write_submodule(
            tmp_path,
            "confluid",
            "git@github.com:Gearlux/confluid.git",
            pyproject_name=None,
        )
        assert discover_gearlux_projects(tmp_path) == []

    def test_strips_manager_suffix(self, tmp_path: Path) -> None:
        """aisland's pyproject distribution is ``aisland-manager`` but the repo is ``aisland``."""
        _write_submodule(tmp_path, "aisland", "git@github.com:Gearlux/aisland.git", "aisland-manager")
        assert discover_gearlux_projects(tmp_path) == ["aisland"]

    def test_returns_sorted(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "zzz", "git@github.com:Gearlux/zzz.git", "zzz")
        _write_submodule(tmp_path, "aaa", "git@github.com:Gearlux/aaa.git", "aaa")
        _write_submodule(tmp_path, "mmm", "git@github.com:Gearlux/mmm.git", "mmm")
        assert discover_gearlux_projects(tmp_path) == ["aaa", "mmm", "zzz"]

    def test_url_without_dot_git_suffix(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "confluid", "git@github.com:Gearlux/confluid", "confluid")
        assert discover_gearlux_projects(tmp_path) == ["confluid"]

    def test_skips_malformed_pyproject_silently(self, tmp_path: Path) -> None:
        """Garbage TOML is ignored, not raised — other submodules still discovered."""
        _write_submodule(tmp_path, "good", "git@github.com:Gearlux/good.git", "good")
        _write_submodule(tmp_path, "bad", "git@github.com:Gearlux/bad.git", pyproject_name=None)
        (tmp_path / "bad").mkdir(exist_ok=True)
        (tmp_path / "bad" / "pyproject.toml").write_text("not = valid = toml = at all")
        assert discover_gearlux_projects(tmp_path) == ["good"]

    def test_skips_pyproject_missing_project_name(self, tmp_path: Path) -> None:
        _write_submodule(tmp_path, "noname", "git@github.com:Gearlux/noname.git", pyproject_name=None)
        (tmp_path / "noname").mkdir(exist_ok=True)
        (tmp_path / "noname" / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools"]\n')
        assert discover_gearlux_projects(tmp_path) == []


class TestIsPrivateGearluxRepo:
    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        """lru_cache leaks across tests; clear before each."""
        is_private_gearlux_repo.cache_clear()

    def test_returns_true_when_gh_says_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="true\n", stderr="")

        monkeypatch.setattr(jenkins_mod.subprocess, "run", fake_run)
        assert is_private_gearlux_repo("navigaitor") is True

    def test_returns_false_when_gh_says_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="false\n", stderr="")

        monkeypatch.setattr(jenkins_mod.subprocess, "run", fake_run)
        assert is_private_gearlux_repo("purifai") is False

    def test_returns_true_when_gh_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Conservative fallback: assume private so the Mint-GitHub-App-token step survives."""

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("gh not installed")

        monkeypatch.setattr(jenkins_mod.subprocess, "run", fake_run)
        assert is_private_gearlux_repo("anything") is True

    def test_returns_true_on_subprocess_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Conservative fallback: assume private on probe failure."""

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.CalledProcessError(returncode=1, cmd=args[0])

        monkeypatch.setattr(jenkins_mod.subprocess, "run", fake_run)
        assert is_private_gearlux_repo("nonexistent") is True

    def test_returns_true_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Conservative fallback: assume private on probe timeout."""

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=5)

        monkeypatch.setattr(jenkins_mod.subprocess, "run", fake_run)
        assert is_private_gearlux_repo("slow") is True

    def test_result_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two calls for the same name only invoke gh once (lru_cache)."""
        call_count = {"n": 0}

        def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
            call_count["n"] += 1
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="true\n", stderr="")

        monkeypatch.setattr(jenkins_mod.subprocess, "run", fake_run)
        is_private_gearlux_repo("xx")
        is_private_gearlux_repo("xx")
        is_private_gearlux_repo("xx")
        assert call_count["n"] == 1
