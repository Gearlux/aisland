"""Tests for `aisland wheels` (aisland/wheels.py): the wheels a project installed outside the
workspace needs, written into its `wheels/` folder, and the check of what it takes from PyPI.

Two groups:
- unit tests over hand-made workspaces and hand-made wheels (no uv, no network);
- an end-to-end run over a fake workspace of git repositories, built and installed by the real
  `uv`, with a local folder standing in for PyPI (`UV_OFFLINE=1` proves nothing reaches the
  network — the folder holds a setuptools wheel repackaged from this interpreter for the builds).
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import click
import pytest
from click.testing import CliRunner

from aisland import wheels as w
from aisland import wheels_install as wi
from aisland.cli import main

# --- helpers -------------------------------------------------------------------------------


def _project(
    root: Path,
    directory: str,
    *,
    name: Optional[str] = None,
    version: str = "0.1.0",
    deps: Sequence[str] = (),
    extras: Optional[Dict[str, List[str]]] = None,
    modules: Optional[Dict[str, str]] = None,
) -> Path:
    """Write a setuptools project: its pyproject and its module files (default: one empty package)."""
    path = root / directory
    path.mkdir(parents=True, exist_ok=True)
    package = (name or directory).replace("-", "_")
    lines = [
        "[build-system]",
        'requires = ["setuptools>=61"]',
        'build-backend = "setuptools.build_meta"',
        "[project]",
        f'name = "{name or directory}"',
        f'version = "{version}"',
        f"dependencies = {json.dumps(list(deps))}",
        "[tool.setuptools.packages.find]",
        f'include = ["{package}*"]',
    ]
    if extras:
        lines.append("[project.optional-dependencies]")
        lines += [f"{extra} = {json.dumps(reqs)}" for extra, reqs in extras.items()]
    (path / "pyproject.toml").write_text("\n".join(lines) + "\n")
    for relative, text in (modules if modules is not None else {f"{package}/__init__.py": ""}).items():
        (path / relative).parent.mkdir(parents=True, exist_ok=True)
        (path / relative).write_text(text)
    return path


def _workspace(root: Path, *directories: str) -> Path:
    """The superproject markers and a `.gitmodules` naming each directory as a submodule."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "TASKS.md").write_text("")
    entries = [f'[submodule "{d}"]\n\tpath = {d}\n\turl = git@github.com:Gearlux/{d}.git\n' for d in directories]
    (root / ".gitmodules").write_text("".join(entries))
    for directory in directories:
        (root / directory).mkdir(exist_ok=True)
    return root


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _commit(path: Path, message: str = "commit") -> None:
    if not (path / ".git").exists():
        _git(path, "init", "-q", "-b", "dev/main")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", message)


def _wheel(path: Path, files: Dict[str, str], *, requires: Sequence[str] = (), version: str = "0.1.0") -> Path:
    """A wheel written by hand: its files plus a dist-info whose METADATA carries `requires`."""
    distribution = path.name.split("-")[0]
    info = f"{distribution}-{version}.dist-info"
    metadata = ["Metadata-Version: 2.1", f"Name: {distribution}", f"Version: {version}"]
    metadata += [f"Requires-Dist: {r}" for r in requires]
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
        archive.writestr(f"{info}/METADATA", "\n".join(metadata) + "\n")
        archive.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\n")
        archive.writestr(f"{info}/RECORD", f"{version}\n")
    return path


_SHIM = '"""Namespace shim."""\n\n__path__ = __import__("pkgutil").extend_path(__path__, __name__)\n'
_GEARLUX = {"project_urls": {"Repository": "https://github.com/Gearlux/x.git"}}


def _published(version: str, wheel: Optional[Path] = None) -> Tuple[str, Dict[str, Any]]:
    """PyPI's JSON answer for a Gearlux project whose latest version is `version`."""
    urls = []
    if wheel is not None:
        urls = [{"packagetype": "bdist_wheel", "filename": wheel.name, "url": wheel.as_uri(), "yanked": False}]
    files = [{"filename": "x.whl", "yanked": False}]
    return "ok", {"info": {"version": version, **_GEARLUX}, "releases": {version: files}, "urls": urls}


_ABSENT: Tuple[str, Any] = ("missing", None)


def _pypi(monkeypatch: pytest.MonkeyPatch, answers: Dict[str, Tuple[str, Any]]) -> None:
    """Answer PyPI's JSON from `answers`; any other name is absent."""
    monkeypatch.setattr(w, "_pypi_json", lambda distribution: answers.get(distribution, _ABSENT))


# --- the workspace's projects and what a project needs -----------------------------------------


class TestDependencyClosure:
    """What a project needs: every workspace project its dependencies reach, extras followed."""

    def test_follows_dependencies_and_the_extras_a_requirement_names(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, "app", "lib", "conf", "plug", "other")
        _project(root, "app", deps=["lib>=0.1.0", "numpy>=1"])
        _project(root, "lib", deps=["conf[extra]>=0.1.0"])
        _project(root, "conf", extras={"extra": ["plug"], "unused": ["other"]})
        _project(root, "plug")
        _project(root, "other")

        projects = w.workspace_projects(root)
        closure = w.dependency_closure(projects, projects["app"])

        assert [p.name for p in closure] == ["conf", "lib", "plug"]

    def test_a_projects_own_extra_named_in_its_dependencies_is_followed(self, tmp_path: Path) -> None:
        """A project's `dev` extra names one of its own extras (`base[sig]`)."""
        root = _workspace(tmp_path, "app", "base", "sig")
        _project(root, "app", deps=["base[dev]"])
        _project(root, "base", extras={"dev": ["base[sig]"], "sig": ["sig"]})
        _project(root, "sig")

        projects = w.workspace_projects(root)

        assert [p.name for p in w.dependency_closure(projects, projects["app"])] == ["base", "sig"]

    def test_extras_and_markers_never_drop_a_project(self, tmp_path: Path) -> None:
        """The wheels serve another machine, so a platform marker is no reason to leave one out."""
        root = _workspace(tmp_path, "app", "winonly")
        _project(root, "app", deps=["winonly ; sys_platform == 'win32'"])
        _project(root, "winonly")

        projects = w.workspace_projects(root)

        assert [p.name for p in w.dependency_closure(projects, projects["app"])] == ["winonly"]

    def test_the_project_itself_is_not_in_its_closure(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, "app", "lib")
        _project(root, "app", deps=["lib"])
        _project(root, "lib", deps=["app"])

        projects = w.workspace_projects(root)

        assert [p.name for p in w.dependency_closure(projects, projects["app"])] == ["lib"]

    def test_a_distribution_name_unlike_its_folder_is_matched(self, tmp_path: Path) -> None:
        """`aisland` the folder is `aisland-manager` the distribution; requirements use the latter."""
        root = _workspace(tmp_path, "app", "aisland")
        _project(root, "app", deps=["Aisland_Manager"])
        _project(root, "aisland", name="aisland-manager")

        projects = w.workspace_projects(root)

        assert [p.name for p in w.dependency_closure(projects, projects["app"])] == ["aisland"]

    def test_an_empty_folder_is_a_project_without_a_checkout(self, tmp_path: Path) -> None:
        """A detached project keeps its distribution name in config.toml; an inactive one, its folder name."""
        root = _workspace(tmp_path, "app", "aisland", "vendorcli")
        (root / "config.toml").write_text('[sources.aisland]\nsource = "pypi"\ndistribution = "aisland-manager"\n')
        _project(root, "app", deps=["aisland-manager", "vendorcli"])

        projects = w.workspace_projects(root)
        closure = w.dependency_closure(projects, projects["app"])

        assert [(p.name, p.distribution, p.checked_out) for p in closure] == [
            ("aisland", "aisland-manager", False),
            ("vendorcli", "vendorcli", False),
        ]

    def test_a_folder_whose_pyproject_names_no_project_is_not_one(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, "app", "tools")
        _project(root, "app")
        (root / "tools" / "pyproject.toml").write_text("[tool.black]\nline-length = 120\n")

        assert sorted(w.workspace_projects(root)) == ["app"]

    def test_an_unreadable_requirement_is_named_with_its_project(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, "app")
        _project(root, "app", deps=["lib >>> 1"])

        projects = w.workspace_projects(root)
        with pytest.raises(click.ClickException, match=r"app/pyproject.toml: cannot read the requirement 'lib >>> 1'"):
            w.dependency_closure(projects, projects["app"])


class TestPypiState:
    """On PyPI = a version published from github.com/Gearlux/, pre-releases included."""

    def test_a_gearlux_pre_release_is_published(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _pypi(monkeypatch, {"recordstream": _published("0.1.0a2")})
        assert w.pypi_state("recordstream")[0] == "published"

    def test_a_name_nobody_published_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _pypi(monkeypatch, {})
        assert w.pypi_state("torpaido") == ("absent", None)

    def test_a_strangers_package_under_the_same_name_is_foreign(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stranger = ("ok", {"info": {"version": "0.1.3", "project_urls": {"Home": "https://example.com"}}})
        _pypi(monkeypatch, {"purifai": stranger})
        assert w.pypi_state("purifai")[0] == "foreign"

    def test_a_gearlux_name_whose_every_file_is_yanked_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        yanked = ("ok", {"info": {"version": "0.1.0", **_GEARLUX}, "releases": {"0.1.0": [{"yanked": True}]}})
        _pypi(monkeypatch, {"liquifai": yanked})
        assert w.pypi_state("liquifai")[0] == "absent"

    def test_an_unreachable_pypi_stops_naming_the_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _pypi(monkeypatch, {"confluid": ("unreachable", "could not reach PyPI (timed out)")})
        with pytest.raises(click.ClickException, match="confluid.*could not reach PyPI"):
            w.pypi_state("confluid")


# --- what a wheel holds ----------------------------------------------------------------------


class TestWheelModules:
    """The packages to import: the outermost real ones — a namespace shim is not one."""

    def test_a_plain_package_and_a_top_level_module(self, tmp_path: Path) -> None:
        wheel = _wheel(
            tmp_path / "lib-0.1.0-py3-none-any.whl",
            {"lib/__init__.py": "", "lib/sub/__init__.py": "", "lib/sub/x.py": "", "helper.py": ""},
        )
        assert w.wheel_modules(wheel) == ["helper", "lib"]

    def test_a_child_under_a_shim_is_imported_by_its_dotted_name(self, tmp_path: Path) -> None:
        """A wheel that still carries the shim (a child without the build step)."""
        wheel = _wheel(
            tmp_path / "child-0.1.0-py3-none-any.whl",
            {"raidar/__init__.py": _SHIM, "raidar/ultralytics/__init__.py": "X = 1\n"},
        )
        assert w.wheel_modules(wheel) == ["raidar.ultralytics"]

    def test_a_child_without_its_parents_init_is_imported_by_its_dotted_name(self, tmp_path: Path) -> None:
        """raidar-ultralytics's wheel: the build step left the shim out."""
        wheel = _wheel(tmp_path / "ultralytics-0.1.0-py3-none-any.whl", {"raidar/ultralytics/__init__.py": ""})
        assert w.wheel_modules(wheel) == ["raidar.ultralytics"]

    def test_a_package_inside_a_plain_folder_of_a_package_is_not_a_second_root(self, tmp_path: Path) -> None:
        wheel = _wheel(
            tmp_path / "lib-0.1.0-py3-none-any.whl",
            {"lib/__init__.py": "", "lib/assets/nested/__init__.py": ""},
        )
        assert w.wheel_modules(wheel) == ["lib"]

    def test_an_init_that_does_more_than_extend_its_path_is_a_real_package(self, tmp_path: Path) -> None:
        wheel = _wheel(
            tmp_path / "lib-0.1.0-py3-none-any.whl",
            {"lib/__init__.py": _SHIM + "VERSION = 1\n", "lib/sub/__init__.py": ""},
        )
        assert w.wheel_modules(wheel) == ["lib"]

    def test_an_init_python_cannot_parse_is_still_a_package_to_import(self, tmp_path: Path) -> None:
        """Importing it is what reports the error."""
        wheel = _wheel(tmp_path / "lib-0.1.0-py3-none-any.whl", {"lib/__init__.py": "def (:\n"})
        assert w.wheel_modules(wheel) == ["lib"]


class TestWheelDifferences:
    """PyPI's wheel against the one this checkout builds: the files, then the dependencies."""

    def test_identical_content_differs_nowhere(self, tmp_path: Path) -> None:
        files = {"lib/__init__.py": "X = 1\n"}
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        published = _wheel(tmp_path / "a" / "lib-0.1.0-py3-none-any.whl", files, requires=["numpy", "click"])
        local = _wheel(
            tmp_path / "b" / "lib-0.2.0-py3-none-any.whl", files, requires=["click", "numpy"], version="0.2.0"
        )
        assert w.wheel_differences(published, local) == []

    def test_changed_added_and_removed_files_and_dependencies_are_named(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        published = _wheel(
            tmp_path / "a" / "lib-0.1.0-py3-none-any.whl",
            {"lib/__init__.py": "X = 1\n", "lib/gone.py": "", "lib/same.py": ""},
            requires=["click"],
        )
        local = _wheel(
            tmp_path / "b" / "lib-0.1.0-py3-none-any.whl",
            {"lib/__init__.py": "X = 2\n", "lib/new.py": "", "lib/same.py": ""},
            requires=["click>=8"],
        )
        assert w.wheel_differences(published, local) == [
            "lib/__init__.py",
            "lib/gone.py",
            "lib/new.py",
            "(its dependencies)",
        ]

    def test_entry_points_are_compared_and_folder_entries_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        published = _wheel(
            tmp_path / "a" / "lib-0.1.0-py3-none-any.whl",
            {"lib/": "", "lib/__init__.py": "", "lib-0.1.0.dist-info/entry_points.txt": "[console_scripts]\nlib = a\n"},
        )
        local = _wheel(
            tmp_path / "b" / "lib-0.1.0-py3-none-any.whl",
            {"lib/__init__.py": "", "lib-0.1.0.dist-info/entry_points.txt": "[console_scripts]\nlib = b\n"},
        )
        assert w.wheel_differences(published, local) == ["(its entry points)"]


# --- refusals --------------------------------------------------------------------------------


def _no_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a refusal case reaches a build."""

    def _refused(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("nothing may be built when the run is refused")

    monkeypatch.setattr(w, "build_wheel", _refused)


class TestRefusals:
    def test_an_unknown_project_is_named(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _workspace(tmp_path, "app")
        _project(root, "app")
        with pytest.raises(click.ClickException, match="no project 'nope' in the workspace"):
            w.vendor_wheels(root, "nope")

    def test_a_project_not_checked_out_here_is_refused_with_the_command_that_activates_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workspace(tmp_path, "navigaitor")
        with pytest.raises(click.ClickException) as error:
            w.vendor_wheels(root, "navigaitor")
        assert "navigaitor is not checked out on this machine" in error.value.message
        assert "aisland setup --activate navigaitor" in error.value.message

    def test_a_project_named_by_its_distribution_is_found_and_named_by_its_folder(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path, "aisland")
        (root / "config.toml").write_text('[sources.aisland]\nsource = "pypi"\ndistribution = "aisland-manager"\n')
        with pytest.raises(click.ClickException, match="aisland is not checked out on this machine"):
            w.vendor_wheels(root, "aisland-manager")

    def test_a_needed_project_without_a_checkout_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workspace(tmp_path, "app", "lib")
        _project(root, "app", deps=["lib"])
        _commit(root / "app")
        _pypi(monkeypatch, {})
        _no_builds(monkeypatch)
        with pytest.raises(click.ClickException) as error:
            w.vendor_wheels(root, "app")
        assert "lib is not on PyPI and not checked out here" in error.value.message
        assert "aisland source set lib local" in error.value.message

    def test_a_project_with_uncommitted_changes_is_refused_naming_the_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workspace(tmp_path, "app", "lib")
        _project(root, "app", deps=["lib"])
        _project(root, "lib")
        _commit(root / "app")
        _commit(root / "lib")
        (root / "lib" / "lib" / "__init__.py").write_text("X = 2\n")
        (root / "lib" / "lib" / "new.py").write_text("")
        _pypi(monkeypatch, {})
        _no_builds(monkeypatch)
        with pytest.raises(click.ClickException) as error:
            w.vendor_wheels(root, "app")
        assert "lib has changes no commit holds" in error.value.message
        assert " M lib/__init__.py" in error.value.message
        assert "?? lib/new.py" in error.value.message

    def test_all_refuses_a_project_the_target_does_not_need_with_uncommitted_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workspace(tmp_path, "app", "other")
        _project(root, "app")
        _project(root, "other")
        _commit(root / "app")
        _commit(root / "other")
        (root / "other" / "other" / "__init__.py").write_text("X = 2\n")
        _pypi(monkeypatch, {})
        _no_builds(monkeypatch)
        with pytest.raises(click.ClickException, match="other has changes no commit holds"):
            w.vendor_wheels(root, "app", scope="all")

    def test_an_unreadable_wheels_yaml_is_refused_at_its_line_before_anything_is_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workspace(tmp_path, "app")
        _project(root, "app")
        _commit(root / "app")
        (root / "app" / "wheels").mkdir()
        (root / "app" / "wheels" / "wheels.yaml").write_text("project: app\nwheels: [\n")
        _pypi(monkeypatch, {})
        _no_builds(monkeypatch)
        with pytest.raises(click.ClickException, match=r"app/wheels/wheels\.yaml:3:1: "):
            w.vendor_wheels(root, "app")


# --- the report ------------------------------------------------------------------------------


def _report(**overrides: Any) -> w.WheelsReport:
    built = [
        w.BuiltWheel("torpaido", "torpaido", "0.1.0", "dev/main", "38ba9a9", "torpaido-0.1.0-py3-none-any.whl"),
        w.BuiltWheel(
            "raidar-ultralytics",
            "raidar-ultralytics",
            "0.1.0",
            "dev/main",
            "d63b739",
            "raidar_ultralytics-0.1.0-py3-none-any.whl",
        ),
    ]
    published = [
        w.PublishedDependency("confluid", "confluid", "0.4.0", "0.4.0", ["confluid/a.py", "confluid/b.py"]),
        w.PublishedDependency("loggair", "loggair", "0.2.0", "0.2.0", []),
        w.PublishedDependency(
            "recordstream",
            "recordstream",
            "0.1.0a2",
            "0.1.0a3",
            ["recordstream/draws.py", "recordstream/x.py", "recordstream/y.py", "recordstream/z.py"],
        ),
        w.PublishedDependency("matrainer", "matrainer", "0.1.0a1", None, [], note="no checkout here"),
    ]
    failures = [
        w.ImportFailure(
            "raidar.ultralytics",
            "ModuleNotFoundError: No module named 'recordstream.draws'",
            "recordstream.draws",
            "recordstream",
        )
    ]
    values: Dict[str, Any] = dict(
        target="navigaitor",
        wheels_dir="navigaitor/wheels",
        wheels_bytes=3_400_000,
        built=built,
        published=published,
        name_clashes=[],
        install_error=None,
        import_failures=failures,
        imported=["torpaido", "navigaitor"],
        packages=["torpaido", "navigaitor", "raidar.ultralytics"],
    )
    values.update(overrides)
    return w.WheelsReport(**values)


class TestReport:
    def test_a_break_names_the_project_to_release_first(self) -> None:
        report = _report()
        assert report.ok is False
        assert report.release_first == ["recordstream"]
        assert report.behind_only == ["confluid"]
        text = w.render_report(report)
        assert "navigaitor needs 2 workspace projects that are not on PyPI" in text
        assert "navigaitor/wheels (3.4 MB)" in text
        assert "raidar-ultralytics  0.1.0  dev/main d63b739" in text
        assert (
            "confluid      PyPI 0.4.0                      behind: 2 files differ (confluid/a.py, confluid/b.py)"
            in text
        )
        assert "loggair       PyPI 0.2.0                      up to date" in text
        assert "matrainer     PyPI 0.1.0a1                    not compared: no checkout here" in text
        assert (
            "recordstream  PyPI 0.1.0a2, checkout 0.1.0a3  behind: 4 files differ "
            "(recordstream/draws.py, recordstream/x.py, recordstream/y.py, …)" in text
        )
        assert "raidar.ultralytics  ModuleNotFoundError: No module named 'recordstream.draws'  (recordstream)" in text
        assert "imported every module of its 3 packages:" in text
        assert "2 other modules imported" in text
        assert text.rstrip().endswith("Release first: recordstream\nBehind, nothing breaks: confluid")

    def test_all_imported_and_nothing_behind_says_so(self) -> None:
        report = _report(
            published=[w.PublishedDependency("loggair", "loggair", "0.2.0", "0.2.0", [])],
            import_failures=[],
        )
        assert report.ok is True
        text = w.render_report(report)
        assert "all 2 modules imported" in text
        assert text.rstrip().endswith("PyPI is up to date for navigaitor.")

    def test_a_failed_install_shows_uvs_own_words_and_cannot_clear_anything_behind(self) -> None:
        error = "× No solution found when resolving dependencies:\n╰─▶ Because only confluid<=0.4.0 is available"
        report = _report(install_error=error, import_failures=[], imported=[])
        assert report.ok is False
        text = w.render_report(report)
        assert "Installing navigaitor in an empty venv failed:\n    × No solution found" in text
        assert "Behind: confluid, recordstream" in text
        nothing_behind = _report(install_error=error, import_failures=[], imported=[], published=[])
        assert w.render_report(nothing_behind).rstrip().endswith("Because only confluid<=0.4.0 is available")

    def test_nothing_to_build_and_nothing_from_pypi(self) -> None:
        text = w.render_report(_report(built=[], published=[], import_failures=[], wheels_bytes=0))
        assert "navigaitor needs no workspace project that is not on PyPI" in text
        assert "It takes no workspace project from PyPI." in text

    def test_one_of_each_is_written_in_the_singular(self) -> None:
        text = w.render_report(
            _report(
                built=_report().built[:1],
                wheels_bytes=465_000,
                published=[w.PublishedDependency("loggair", "loggair", "0.2.0", "0.2.0", ["loggair/core.py"])],
                import_failures=[],
                imported=["navigaitor"],
                packages=["navigaitor"],
            )
        )
        assert "needs 1 workspace project that is not on PyPI. Its wheel is in navigaitor/wheels (465 kB):" in text
        assert "It takes 1 workspace project from PyPI" in text
        assert "behind: 1 file differs (loggair/core.py)" in text
        assert "imported every module of its 1 package:" in text
        assert "all 1 module imported" in text
        assert text.rstrip().endswith("Behind, nothing breaks: loggair")

    def test_a_failure_no_published_project_explains_is_said_so(self) -> None:
        failure = w.ImportFailure("navigaitor", "ModuleNotFoundError: No module named 'torch'", "torch", None)
        report = _report(import_failures=[failure])
        assert report.release_first == []
        text = w.render_report(report)
        assert "navigaitor  ModuleNotFoundError: No module named 'torch'\n" in text
        assert "navigaitor does not import, and no project it takes from PyPI is the cause." in text

    def test_a_module_missing_what_no_workspace_project_ships_is_listed_and_breaks_nothing(self) -> None:
        warning = w.ImportFailure("navigaitor.ops", "ModuleNotFoundError: No module named 'pycrate'", "pycrate", None)
        report = _report(
            published=[w.PublishedDependency("loggair", "loggair", "0.2.0", "0.2.0", [])],
            import_failures=[],
            import_warnings=[warning],
        )
        assert report.ok is True
        text = w.render_report(report)
        assert "  2 modules imported\n" in text
        assert "all 2" not in text
        assert (
            "Not imported, missing what no workspace project ships (an optional extra's?):\n"
            "  navigaitor.ops  ModuleNotFoundError: No module named 'pycrate'\n" in text
        )
        assert text.rstrip().endswith("PyPI is up to date for navigaitor.")

    def test_a_name_clash_is_reported(self) -> None:
        clash = "purifai: PyPI's purifai 0.1.3 is another project's — pip takes it over this wheel (0.1.0)"
        assert clash in w.render_report(_report(name_clashes=[clash]))

    def test_the_json_form_carries_every_field_and_the_verdicts(self) -> None:
        data = json.loads(json.dumps(_report().to_dict()))
        assert data["ok"] is False
        assert data["release_first"] == ["recordstream"]
        assert data["built"][0] == {
            "project": "torpaido",
            "distribution": "torpaido",
            "version": "0.1.0",
            "branch": "dev/main",
            "commit": "38ba9a9",
            "wheel": "torpaido-0.1.0-py3-none-any.whl",
            "needed": True,
            "on_pypi": None,
        }
        assert data["import_failures"][0]["distribution"] == "recordstream"
        assert data["scope"] == "unpublished" and data["not_built"] == []


class TestReadme:
    def test_the_readme_names_its_generator_each_wheels_commit_and_the_install_command(self) -> None:
        text = w.render_readme("navigaitor", "unpublished", _report().built)
        assert "AUTO-GENERATED FILE — DO NOT EDIT BY HAND" in text
        assert "Generated by: aisland wheels build navigaitor" in text
        assert "Source: aisland/aisland/wheels.py (render_readme)" in text
        assert "To regenerate: aisland wheels build navigaitor" in text
        assert "    python wheels/install.py\n" in text
        assert "    python wheels/install.py --reinstall\n" in text
        assert "| raidar_ultralytics-0.1.0-py3-none-any.whl | raidar-ultralytics | dev/main | d63b739 | yes |" in text

    def test_the_readme_holds_no_date_so_an_unchanged_rebuild_changes_no_file(self) -> None:
        built = _report().built
        assert w.render_readme("navigaitor", "all", built) == w.render_readme("navigaitor", "all", built)

    def test_with_all_the_readme_names_the_scope_and_which_wheels_are_needed(self) -> None:
        built = [
            *_report().built,
            w.BuiltWheel(
                "raidar",
                "raidar",
                "0.2.0",
                "dev/main",
                "9f8e7d6",
                "raidar-0.2.0-py3-none-any.whl",
                needed=False,
                on_pypi="0.2.0",
            ),
        ]
        text = w.render_readme("navigaitor", "all", built)
        assert "To regenerate: aisland wheels build navigaitor --all" in text
        assert "| raidar-0.2.0-py3-none-any.whl | raidar | dev/main | 9f8e7d6 | no |" in text


class TestWheelsYaml:
    """`wheels.yaml`: what install.py reads; `install:` is the user's, kept across reruns."""

    @staticmethod
    def _built() -> List[w.BuiltWheel]:
        return [
            w.BuiltWheel(
                "confluid",
                "confluid",
                "0.4.0",
                "dev/main",
                "1a2b3c4",
                "confluid-0.4.0-py3-none-any.whl",
                on_pypi="0.4.0",
            ),
            w.BuiltWheel(
                "raidar",
                "raidar",
                "0.2.0",
                "dev/main",
                "9f8e7d6",
                "raidar-0.2.0-py3-none-any.whl",
                needed=False,
                on_pypi="1.0",
            ),
        ]

    def test_each_wheel_its_commit_whether_it_is_needed_and_install_defaulting_to_needed(self, tmp_path: Path) -> None:
        not_built = [w.NotBuilt("loggair", "no checkout here — taken from PyPI 0.2.0")]
        text = w.render_wheels_yaml("navigaitor", "all", self._built(), not_built, {})
        assert "# To regenerate: aisland wheels build navigaitor --all" in text
        assert "# AUTO-GENERATED FILE — edit only the `install:` values (a rerun keeps them)" in text
        assert "DO NOT EDIT" not in text
        assert (
            '  - wheel: "raidar-0.2.0-py3-none-any.whl"\n    project: "raidar"\n    commit: "dev/main 9f8e7d6"\n'
            '    needed: false\n    on_pypi: "1.0"\n    install: false\n' in text
        )
        assert '  - project: "loggair"\n    reason: "no checkout here — taken from PyPI 0.2.0"\n' in text
        (tmp_path / "wheels.yaml").write_text(text)
        project, entries = wi.read_wheels(tmp_path / "wheels.yaml")
        assert project == "navigaitor"
        assert [(e.project, e.needed, e.install) for e in entries] == [
            ("confluid", True, True),
            ("raidar", False, False),
        ]

    def test_an_install_the_user_set_is_kept(self) -> None:
        text = w.render_wheels_yaml("app", "all", self._built(), [], {"raidar": True, "confluid": False, "gone": True})
        assert (
            '    project: "confluid"\n    commit: "dev/main 1a2b3c4"\n    needed: true\n    on_pypi: "0.4.0"\n    install: false\n'
            in text
        )
        assert '    needed: false\n    on_pypi: "1.0"\n    install: true\n' in text
        assert "gone" not in text

    def test_no_wheel_and_nothing_left_out_are_empty_lists(self) -> None:
        text = w.render_wheels_yaml("app", "unpublished", [], [], {})
        assert text.endswith('project: "app"\nwheels: []\nnot_built: []\n')

    def test_the_choices_are_read_back_from_the_file_and_a_missing_file_has_none(self, tmp_path: Path) -> None:
        assert w.read_install_choices(tmp_path / "wheels.yaml") == {}
        (tmp_path / "wheels.yaml").write_text(w.render_wheels_yaml("app", "all", self._built(), [], {"raidar": True}))
        assert w.read_install_choices(tmp_path / "wheels.yaml") == {"confluid": True, "raidar": True}

    def test_an_install_that_is_not_true_or_false_is_refused_at_its_line(self, tmp_path: Path) -> None:
        text = w.render_wheels_yaml("app", "all", self._built(), [], {}).replace("install: false", "install: maybe")
        (tmp_path / "wheels.yaml").write_text(text)
        line = text.splitlines().index("    install: maybe") + 1
        with pytest.raises(click.ClickException, match=rf"wheels\.yaml:{line}: `install:` must be true or false"):
            w.read_install_choices(tmp_path / "wheels.yaml")


class TestTheInstallScript:
    def test_the_script_is_the_template_under_a_header_naming_its_command(self) -> None:
        script = w.render_install_script("navigaitor", "needed")
        template = Path(wi.__file__).read_text()
        header = script.split("# =========================================================================\n")
        assert "# Generated by: aisland wheels build navigaitor --include-published\n" in header[1]
        assert "# To regenerate: aisland wheels build navigaitor --include-published\n" in header[1]
        assert "THE TEMPLATE" not in script
        assert (
            header[2]
            == template.split("# =========================================================================\n")[2]
        )

    @pytest.mark.parametrize("scope", ["unpublished", "needed", "all"])
    def test_each_files_regenerate_line_is_a_command_the_cli_accepts(self, scope: w.WheelsScope) -> None:
        """Parsed with the REAL `wheels` command (as the Jenkins scaffold's pin does), never string-matched."""
        from aisland.cli import wheels_build

        built = TestWheelsYaml._built()
        files = {
            "README.md": w.render_readme("navigaitor", scope, built),
            "wheels.yaml": w.render_wheels_yaml("navigaitor", scope, built, [], {}),
            "install.py": w.render_install_script("navigaitor", scope),
        }
        expected = {"unpublished": (False, False), "needed": (True, False), "all": (False, True)}[scope]
        for name, text in files.items():
            line = next(ln for ln in text.splitlines() if "To regenerate:" in ln)
            argv = line.split("To regenerate:")[1].split()
            assert argv[:3] == ["aisland", "wheels", "build"], f"{name}: {argv}"
            context = wheels_build.make_context("build", argv[3:])
            assert context.params["project"] == "navigaitor", name
            assert (context.params["include_published"], context.params["include_all"]) == expected, name


class TestCli:
    def test_exit_one_and_the_text_report_when_something_breaks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(w, "vendor_wheels", lambda root, project, **kwargs: _report())
        result = CliRunner().invoke(main, ["wheels", "navigaitor"])
        assert result.exit_code == 1, result.output
        assert "Release first: recordstream" in result.output

    def test_exit_zero_and_json_when_nothing_breaks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(w, "vendor_wheels", lambda root, project, **kwargs: _report(import_failures=[]))
        result = CliRunner().invoke(main, ["wheels", "navigaitor", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["ok"] is True

    @pytest.mark.parametrize(
        "flags, scope",
        [
            ([], "unpublished"),
            (["--include-published"], "needed"),
            (["--all"], "all"),
            (["--include-published", "--all"], "all"),
        ],
    )
    def test_the_flags_choose_what_is_built(
        self, monkeypatch: pytest.MonkeyPatch, flags: List[str], scope: str
    ) -> None:
        asked: List[str] = []

        def _vendor(root: Path, project: str, scope: str, commit: bool, make_zip: bool) -> w.WheelsReport:
            asked.append(scope)
            return _report(import_failures=[])

        monkeypatch.setattr(w, "vendor_wheels", _vendor)
        result = CliRunner().invoke(main, ["wheels", "navigaitor", *flags])
        assert result.exit_code == 0, result.output
        assert asked == [scope]


# --- the run, with builds and the empty venv faked ---------------------------------------------


class TestTheRunWithFakedBuilds:
    """What vendor_wheels decides, without uv: each build hands back a wheel written by hand."""

    @staticmethod
    def _fake_builds(
        monkeypatch: pytest.MonkeyPatch,
        versions: Dict[str, str],
        failures: Optional[Dict[str, Dict[str, Optional[str]]]] = None,
    ) -> List[Tuple[str, ...]]:
        installs: List[Tuple[str, ...]] = []

        def _build(uv: str, source: Path, out: Path, timestamp: Optional[int] = None) -> Path:
            name = source.name
            out.mkdir(parents=True, exist_ok=True)
            return _wheel(out / f"{name}-{versions.get(name, '0.1.0')}-py3-none-any.whl", {f"{name}/__init__.py": ""})

        def _install(
            uv: str, target: Path, wheels: Path, packages: List[str], scratch: Path
        ) -> Tuple[None, Dict, List[str]]:
            installs.append(tuple(packages))
            return None, failures or {}, list(packages)

        monkeypatch.setattr(w, "_uv", lambda root: "uv")
        monkeypatch.setattr(w, "build_wheel", _build)
        monkeypatch.setattr(w, "install_and_import", _install)
        return installs

    def test_names_pypi_gives_to_another_project_are_reported_when_pip_would_take_theirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workspace(tmp_path, "app", "purifai", "older", "odd")
        _project(root, "app", deps=["purifai", "older", "odd"])
        for name in ("app", "purifai", "older", "odd"):
            if name != "app":
                _project(root, name)
            _commit(root / name)
        stranger = {"project_urls": {"Home": "https://example.com"}}
        _pypi(
            monkeypatch,
            {
                "purifai": ("ok", {"info": {"version": "0.1.3", **stranger}}),
                "older": ("ok", {"info": {"version": "0.0.1", **stranger}}),
                "odd": ("ok", {"info": {"version": "not a version", **stranger}}),
            },
        )
        installs = self._fake_builds(monkeypatch, {})

        report = w.vendor_wheels(root, "app")

        assert [b.project for b in report.built] == ["odd", "older", "purifai"]
        assert report.name_clashes == [
            "odd: PyPI's odd not a version is another project's — pip takes it over this wheel (0.1.0)",
            "purifai: PyPI's purifai 0.1.3 is another project's — pip takes it over this wheel (0.1.0)",
        ]
        assert installs == [("app", "odd", "older", "purifai")]

    @staticmethod
    def _scoped_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """`app` needs libx (not on PyPI), pubdep (on PyPI, checked out) and gone (on PyPI, no checkout);
        `other` (not needed, checked out) and `ghost` (not needed, no checkout) are the workspace's too."""
        root = _workspace(tmp_path, "app", "libx", "pubdep", "gone", "other", "ghost")
        _project(root, "app", deps=["libx", "pubdep", "gone"])
        for name in ("libx", "pubdep", "other"):
            _project(root, name)
        for name in ("app", "libx", "pubdep", "other"):
            _commit(root / name)
        gone_wheel = _wheel(tmp_path / "gone-1.0-py3-none-any.whl", {"gone/__init__.py": ""})
        pubdep_wheel = _wheel(tmp_path / "pubdep-0.1.0-py3-none-any.whl", {"pubdep/__init__.py": ""})
        _pypi(monkeypatch, {"pubdep": _published("0.1.0", pubdep_wheel), "gone": _published("1.0", gone_wheel)})
        return root

    def test_by_default_only_what_pypi_lacks_is_built(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self._scoped_workspace(tmp_path, monkeypatch)
        installs = self._fake_builds(monkeypatch, {})

        report = w.vendor_wheels(root, "app")

        assert [(b.project, b.needed, b.on_pypi) for b in report.built] == [("libx", True, None)]
        assert [p.project for p in report.published] == ["gone", "pubdep"]
        assert report.not_built == []
        assert installs == [("app", "libx")]
        _, entries = wi.read_wheels(root / "app" / "wheels" / "wheels.yaml")
        assert [(e.project, e.install) for e in entries] == [("libx", True)]
        assert (root / "app" / "wheels" / "install.py").is_file()

    def test_include_published_builds_every_needed_project_with_a_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._scoped_workspace(tmp_path, monkeypatch)
        installs = self._fake_builds(monkeypatch, {})

        report = w.vendor_wheels(root, "app", scope="needed")

        assert [(b.project, b.needed, b.on_pypi) for b in report.built] == [
            ("libx", True, None),
            ("pubdep", True, "0.1.0"),
        ]
        assert [(n.project, n.reason) for n in report.not_built] == [("gone", "no checkout here — taken from PyPI 1.0")]
        assert [(p.project, p.note) for p in report.published] == [("gone", "no checkout here")]
        assert installs == [("app", "libx", "pubdep")]
        text = w.render_report(report)
        assert "app needs 3 workspace projects; 2 are built from this checkout into app/wheels" in text
        assert "  pubdep  0.1.0  dev/main" in text and "on PyPI 0.1.0" in text
        assert "Not built: gone (no checkout here — taken from PyPI 1.0)" in text

    def test_all_builds_every_project_with_a_checkout_and_installs_only_the_needed_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._scoped_workspace(tmp_path, monkeypatch)
        installs = self._fake_builds(monkeypatch, {})

        report = w.vendor_wheels(root, "app", scope="all")

        assert [(b.project, b.needed) for b in report.built] == [("libx", True), ("other", False), ("pubdep", True)]
        assert [(n.project, n.reason) for n in report.not_built] == [
            ("ghost", "no checkout here"),
            ("gone", "no checkout here — taken from PyPI 1.0"),
        ]
        assert installs == [("app", "libx", "pubdep")], "the check installs and imports what app needs, not `other`"
        folder = root / "app" / "wheels"
        assert sorted(p.name for p in folder.glob("*.whl")) == [
            "libx-0.1.0-py3-none-any.whl",
            "other-0.1.0-py3-none-any.whl",
            "pubdep-0.1.0-py3-none-any.whl",
        ]
        _, entries = wi.read_wheels(folder / "wheels.yaml")
        assert [(e.project, e.needed, e.install) for e in entries] == [
            ("libx", True, True),
            ("other", False, False),
            ("pubdep", True, True),
        ]
        text = w.render_report(report)
        assert "Every workspace project with a checkout here is built into app/wheels" in text
        assert "app needs 3 workspace projects:" in text
        assert "not needed" in text

    def test_a_rerun_keeps_the_install_the_user_chose(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = self._scoped_workspace(tmp_path, monkeypatch)
        self._fake_builds(monkeypatch, {})
        w.vendor_wheels(root, "app", scope="all")
        yaml_file = root / "app" / "wheels" / "wheels.yaml"
        yaml_file.write_text(
            yaml_file.read_text()
            .replace('    project: "other"\n    commit: ', '    project: "other"\n    commit: ')
            .replace(
                "    needed: false\n    on_pypi: null\n    install: false",
                "    needed: false\n    on_pypi: null\n    install: true",
            )
        )

        w.vendor_wheels(root, "app", scope="all")

        _, entries = wi.read_wheels(yaml_file)
        assert [(e.project, e.install) for e in entries] == [("libx", True), ("other", True), ("pubdep", True)]

    def test_a_package_or_a_module_missing_a_workspace_projects_module_breaks_any_other_module_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workspace(tmp_path, "app", "libx")
        _project(root, "app", deps=["libx"])
        _project(root, "libx")
        _commit(root / "app")
        _commit(root / "libx")
        _pypi(monkeypatch, {})
        failures: Dict[str, Dict[str, Optional[str]]] = {
            "libx": {"error": "ModuleNotFoundError: No module named 'torch'", "missing": "torch"},
            "app.labelling": {"error": "ModuleNotFoundError: No module named 'libx.new'", "missing": "libx.new"},
            "app.extra": {"error": "ModuleNotFoundError: No module named 'pycrate'", "missing": "pycrate"},
        }
        self._fake_builds(monkeypatch, {}, failures)

        report = w.vendor_wheels(root, "app")

        assert report.packages == ["app", "libx"]
        assert [(f.module, f.distribution) for f in report.import_failures] == [
            ("app.labelling", "libx"),  # a deeper module, but what it misses is a workspace project's
            ("libx", None),  # a package that does not import is a break, whatever it misses
        ]
        assert [f.module for f in report.import_warnings] == ["app.extra"]
        assert report.ok is False

    def test_a_published_dependency_without_a_checkout_or_a_pure_wheel_is_not_compared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _workspace(tmp_path, "app", "detached", "native")
        _project(root, "app", deps=["detached", "native"])
        _project(root, "native")
        _commit(root / "app")
        _commit(root / "native")
        pypi_wheel = _wheel(tmp_path / "detached-1.0-py3-none-any.whl", {"detached/__init__.py": ""})
        native_only = {"packagetype": "bdist_wheel", "filename": "native-2.0-cp313-cp313-macosx_11_0_arm64.whl"}
        _pypi(
            monkeypatch,
            {
                "detached": _published("1.0", pypi_wheel),
                "native": ("ok", {**_published("2.0")[1], "urls": [native_only]}),
            },
        )
        self._fake_builds(monkeypatch, {})

        report = w.vendor_wheels(root, "app")

        assert report.built == []
        assert [(p.project, p.pypi_version, p.checkout_version, p.note) for p in report.published] == [
            ("detached", "1.0", None, "no checkout here"),
            ("native", "2.0", None, "PyPI has no pure-Python wheel of 2.0 to compare"),
        ]
        assert (root / "app" / "wheels" / "README.md").read_text().rstrip().endswith("No wheel is needed.")

    def test_the_workspaces_uv_comes_from_its_venv(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        asked: List[Path] = []

        def _ensure(venv_bin: Path) -> str:
            asked.append(venv_bin)
            return "uv"

        monkeypatch.setattr(w, "ensure_uv_in_venv", _ensure)
        assert w._uv(tmp_path) == "uv"
        assert asked == [tmp_path / ".venv" / "bin"]


class TestOwner:
    """A missing module points at the distribution whose package is its longest prefix."""

    def test_the_longest_prefix_wins(self) -> None:
        owners = {"raidar": "raidar", "raidar.ultralytics": "raidar-ultralytics"}
        assert w._owner("raidar.ultralytics.models", owners) == "raidar-ultralytics"
        assert w._owner("raidar.other", owners) == "raidar"

    def test_a_module_nobody_ships_or_no_module_has_no_owner(self) -> None:
        assert w._owner("torch", {"raidar": "raidar"}) is None
        assert w._owner(None, {"raidar": "raidar"}) is None


# --- the empty venv, with uv replaced by a script ---------------------------------------------


def _fake_uv(folder: Path, *, venv: int = 0, install: int = 0, imports: str = "exit 3") -> Path:
    """A `uv` that logs its arguments and exits as told. The venv it makes has a `python` that runs this
    interpreter (so the shipped install.py runs for real), except for the import run (`-c`), which does
    `imports`."""
    log = folder / "uv.log"
    script = folder / "uv"
    script.write_text(
        f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = venv ]; then
    [ {venv} -eq 0 ] || {{ echo "venv: no interpreter" >&2; exit {venv}; }}
    for argument in "$@"; do venv_dir="$argument"; done
    mkdir -p "$venv_dir/bin"
    printf '#!/bin/sh\\nif [ "$1" = -c ]; then echo boom >&2; {imports}; fi\\nexec {sys.executable} "$@"\\n' > "$venv_dir/bin/python"
    chmod +x "$venv_dir/bin/python"
    exit 0
fi
[ {install} -eq 0 ] || {{ echo "× No solution found when resolving dependencies" >&2; exit {install}; }}
"""
    )
    script.chmod(0o755)
    return script


def _target_with_wheels(tmp_path: Path) -> Tuple[Path, Path]:
    """`app` and the wheels folder `aisland wheels` writes: one needed wheel, one not."""
    target = tmp_path / "app"
    folder = target / "wheels"
    folder.mkdir(parents=True)
    built = [
        w.BuiltWheel("libx", "libx", "0.1.0", "dev/main", "1111111", "libx-0.1.0-py3-none-any.whl"),
        w.BuiltWheel("other", "other", "0.1.0", "dev/main", "2222222", "other-0.1.0-py3-none-any.whl", needed=False),
    ]
    for b in built:
        (folder / b.wheel).write_text("")
    (folder / "wheels.yaml").write_text(w.render_wheels_yaml("app", "all", built, [], {"other": True}))
    (folder / "install.py").write_text(w.render_install_script("app", "all"))
    return target, folder


class TestInstallAndImport:
    def test_a_venv_that_cannot_be_made_is_the_install_error(self, tmp_path: Path) -> None:
        target, folder = _target_with_wheels(tmp_path)
        uv = _fake_uv(tmp_path, venv=2)
        error, failures, imported = w.install_and_import(str(uv), target, folder, ["app"], tmp_path)
        assert error == "creating the venv failed:\nvenv: no interpreter"
        assert failures == {} and imported == []

    def test_the_shipped_script_installs_the_needed_wheels_with_uv_into_the_venv(self, tmp_path: Path) -> None:
        target, folder = _target_with_wheels(tmp_path)
        uv = _fake_uv(tmp_path)
        w.install_and_import(str(uv), target, folder, ["app"], tmp_path)
        install_line = (tmp_path / "uv.log").read_text().splitlines()[1]
        assert install_line.startswith(
            f"pip install --python {sys.executable} --quiet --find-links {folder} -e {target} "
        )
        assert install_line.endswith(str(folder / "libx-0.1.0-py3-none-any.whl")), "only what app needs, not `other`"

    def test_uvs_resolution_error_is_the_install_error(self, tmp_path: Path) -> None:
        target, folder = _target_with_wheels(tmp_path)
        uv = _fake_uv(tmp_path, install=1)
        error, _, _ = w.install_and_import(str(uv), target, folder, ["app"], tmp_path)
        assert error is not None and error.endswith("× No solution found when resolving dependencies")

    def test_an_import_run_that_dies_without_a_result_says_so(self, tmp_path: Path) -> None:
        target, folder = _target_with_wheels(tmp_path)
        uv = _fake_uv(tmp_path)
        error, _, _ = w.install_and_import(str(uv), target, folder, ["app"], tmp_path)
        assert error == "the import run ended without a result (exit 3):\nboom"

    def test_the_targets_own_uv_toml_configures_the_install(self, tmp_path: Path) -> None:
        target, folder = _target_with_wheels(tmp_path)
        (target / "uv.toml").write_text('extra-index-url = ["https://private.example/simple"]\n')
        uv = _fake_uv(tmp_path)
        w.install_and_import(str(uv), target, folder, ["app"], tmp_path)
        install_line = (tmp_path / "uv.log").read_text().splitlines()[1]
        assert f"--config-file {target / 'uv.toml'}" in install_line

    def test_without_a_uv_toml_no_configuration_is_named(self, tmp_path: Path) -> None:
        target, folder = _target_with_wheels(tmp_path)
        uv = _fake_uv(tmp_path)
        w.install_and_import(str(uv), target, folder, ["app"], tmp_path)
        assert "--config-file" not in (tmp_path / "uv.log").read_text()


# --- end to end: real git, real uv, a local folder for PyPI ----------------------------------


def _repackaged_wheel(folder: Path, name: str) -> None:
    """This interpreter's copy of `name`, repackaged as a wheel: setuptools for uv's isolated builds, PyYAML
    for the shipped install.py, which installs it into the empty venv first."""
    distribution = importlib.metadata.distribution(name)
    base = Path(str(distribution.locate_file("")))
    wheel_name = f"{distribution.metadata['Name'].replace('-', '_')}-{distribution.version}-py3-none-any.whl"
    with zipfile.ZipFile(folder / wheel_name, "w") as archive:
        for file in distribution.files or []:
            relative = str(file)
            if relative.startswith("..") or "__pycache__" in relative or not (base / file).is_file():
                continue
            archive.write(base / file, relative)


@pytest.fixture
def offline_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The real uv, offline, with a fresh cache and a local folder in PyPI's place; returns that folder."""
    pypi = tmp_path / "pypi"
    pypi.mkdir()
    _repackaged_wheel(pypi, "setuptools")
    _repackaged_wheel(pypi, "PyYAML")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setattr(w, "_PYPI_INDEX_ARGS", ("--no-index", "--find-links", str(pypi)))
    monkeypatch.setattr(w, "_uv", lambda root: str(Path(sys.executable).with_name("uv")))
    return pypi


def _end_to_end_workspace(root: Path, pypi: Path, monkeypatch: pytest.MonkeyPatch, *, released_new: bool) -> Path:
    """`app` needs `libx` (not on PyPI), which needs `pubdep` (on PyPI). The checkout's pubdep has
    `pubdep/newer.py`, which libx imports; PyPI's pubdep 1.0 has it only when `released_new`."""
    _workspace(root / "ws", "app", "libx", "pubdep", "unrelated")
    ws = root / "ws"
    app_modules = {
        "app/__init__.py": "",
        "app/core.py": "import libx\n",
        # Only a deeper import reaches it: `import app` never does.
        "app/labelling.py": "from pubdep.newer import VALUE\n",
        # An optional extra's module: what it misses no workspace project ships.
        "app/extra.py": "import some_optional_extra\n",
        # Importing it would run the program.
        "app/__main__.py": "raise SystemExit('ran the program')\n",
    }
    _project(ws, "app", deps=["libx>=0.1.0"], modules=app_modules)
    _project(ws, "libx", deps=["pubdep>=1.0"], modules={"libx/__init__.py": "from pubdep.newer import VALUE\n"})
    pubdep_modules = {"pubdep/__init__.py": "", "pubdep/newer.py": "VALUE = 1\n"}
    _project(ws, "pubdep", version="1.0", modules=pubdep_modules)
    _project(ws, "unrelated")
    for directory in ("app", "libx", "pubdep", "unrelated"):
        _commit(ws / directory)

    # PyPI's pubdep 1.0: built from the same project, with or without newer.py.
    release = root / "release"
    _project(root, "release", name="pubdep", version="1.0", modules=pubdep_modules if released_new else {})
    if not released_new:
        (release / "pubdep").mkdir()
        (release / "pubdep" / "__init__.py").write_text("")
    wheel = w.build_wheel(str(Path(sys.executable).with_name("uv")), release, pypi)
    _pypi(monkeypatch, {"pubdep": _published("1.0", wheel)})

    # A stale file in libx's setuptools build folder must not reach its wheel, an old wheel must
    # leave the target's folder, and a file the command did not write must stay.
    (ws / "libx" / "build" / "lib" / "libx").mkdir(parents=True)
    (ws / "libx" / "build" / "lib" / "libx" / "deleted_long_ago.py").write_text("")
    (ws / "libx" / ".gitignore").write_text("build/\n")
    _commit(ws / "libx", "ignore build")
    (ws / "app" / "wheels").mkdir()
    (ws / "app" / "wheels" / "old-0.0.1-py3-none-any.whl").write_text("")
    (ws / "app" / "wheels" / "notes.txt").write_text("mine")
    return ws


class TestEndToEnd:
    def test_a_folder_without_a_commit_cannot_be_exported(self, tmp_path: Path) -> None:
        (tmp_path / "plain").mkdir()
        with pytest.raises(click.ClickException, match="plain: cannot export its last commit"):
            w.export_head(tmp_path / "plain", tmp_path / "out")

    def test_a_failed_build_shows_what_the_build_said(self, tmp_path: Path, offline_uv: Path) -> None:
        broken = tmp_path / "broken"
        broken.mkdir()
        (broken / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools>=61"]\n[project]\nname = 1\n')
        with pytest.raises(click.ClickException, match="building the wheel of broken failed:\n.+"):
            w.build_wheel(str(Path(sys.executable).with_name("uv")), broken, tmp_path / "out")

    def test_a_stale_pypi_release_breaks_the_import_and_is_named(
        self, tmp_path: Path, offline_uv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _end_to_end_workspace(tmp_path, offline_uv, monkeypatch, released_new=False)

        report = w.vendor_wheels(ws, "app")

        folder = ws / "app" / "wheels"
        assert sorted(p.name for p in folder.iterdir()) == [
            ".gitignore",
            "README.md",
            "install.py",
            "libx-0.1.0-py3-none-any.whl",
            "notes.txt",
            "wheels.yaml",
        ]
        with zipfile.ZipFile(folder / "libx-0.1.0-py3-none-any.whl") as archive:
            assert not [n for n in archive.namelist() if "deleted_long_ago" in n]
        commit = _git(ws / "libx", "rev-parse", "--short", "HEAD").strip()
        assert [(b.project, b.version, b.branch, b.commit) for b in report.built] == [
            ("libx", "0.1.0", "dev/main", commit)
        ]
        assert f"| libx-0.1.0-py3-none-any.whl | libx | dev/main | {commit} |" in (folder / "README.md").read_text()
        assert [(p.project, p.pypi_version, p.differing_files) for p in report.published] == [
            ("pubdep", "1.0", ["pubdep/newer.py"])
        ]
        assert report.install_error is None
        assert [(f.module, f.missing, f.distribution) for f in report.import_failures] == [
            ("app.core", "pubdep.newer", "pubdep"),
            ("app.labelling", "pubdep.newer", "pubdep"),
            ("libx", "pubdep.newer", "pubdep"),
        ]
        assert [(f.module, f.missing) for f in report.import_warnings] == [("app.extra", "some_optional_extra")]
        assert report.imported == ["app"]
        assert report.release_first == ["pubdep"]
        assert report.ok is False

    def test_once_pypi_has_the_release_everything_imports(
        self, tmp_path: Path, offline_uv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _end_to_end_workspace(tmp_path, offline_uv, monkeypatch, released_new=True)
        first = w.vendor_wheels(ws, "app")
        digest = hashlib.sha256((ws / "app" / "wheels" / "libx-0.1.0-py3-none-any.whl").read_bytes()).hexdigest()

        second = w.vendor_wheels(ws, "app")

        assert first.ok and second.ok
        assert [(p.project, p.differing_files) for p in second.published] == [("pubdep", [])]
        assert second.imported == ["app", "app.core", "app.labelling", "libx"]
        assert [f.module for f in second.import_warnings] == ["app.extra"]
        rebuilt = hashlib.sha256((ws / "app" / "wheels" / "libx-0.1.0-py3-none-any.whl").read_bytes()).hexdigest()
        assert rebuilt == digest, "an unchanged project must rebuild byte for byte, or git sees a change"

    def test_include_published_ships_the_checkouts_wheel_over_pypis_of_the_same_version(
        self, tmp_path: Path, offline_uv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PyPI's pubdep 1.0 lacks `pubdep.newer`; the checkout's 1.0 has it. Without the flag that is a
        "release first"; with it, the shipped wheel is installed by its file and everything imports."""
        ws = _end_to_end_workspace(tmp_path, offline_uv, monkeypatch, released_new=False)

        report = w.vendor_wheels(ws, "app", scope="needed")

        assert [(b.project, b.on_pypi) for b in report.built] == [("libx", None), ("pubdep", "1.0")]
        assert report.published == [] and report.not_built == []
        assert report.ok, w.render_report(report)
        assert report.imported == ["app", "app.core", "app.labelling", "libx", "pubdep", "pubdep.newer"]
        assert "    python wheels/install.py\n" in (ws / "app" / "wheels" / "README.md").read_text()

    def test_all_ships_a_project_the_target_does_not_need_without_installing_it(
        self, tmp_path: Path, offline_uv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _end_to_end_workspace(tmp_path, offline_uv, monkeypatch, released_new=False)

        report = w.vendor_wheels(ws, "app", scope="all")

        assert [(b.project, b.needed) for b in report.built] == [("libx", True), ("pubdep", True), ("unrelated", False)]
        assert report.ok, w.render_report(report)
        assert "unrelated" not in report.imported
