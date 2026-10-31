"""Tests for `aisland wheels build <project> --bundle` (aisland/wheels_bundle.py and its use in aisland/wheels.py):
the project and the wheels it needs merged into one wheel that plain `pip install` installs.

Three groups: what the bundle requires (pure functions over hand-written requirements), the merge (over wheels
written by hand), and the run — with builds faked, then end to end with the real uv offline.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import click
import pytest
from click.testing import CliRunner
from packaging.requirements import Requirement

from aisland import wheels as w
from aisland import wheels_bundle as wb
from aisland.cli import main

from . import test_wheels
from .test_wheels import _commit, _end_to_end_workspace, _project, _report, _wheel, offline_uv


@pytest.fixture(autouse=True)
def _own_workspace(empty_workspace: Path) -> Path:
    """The commands of this file run in a workspace of the test's own, never the developer's."""
    return empty_workspace


__all__ = ["offline_uv"]  # the fixture, imported so the end-to-end tests here can ask for it

#: The faked builds of test_wheels.py, reached through its module: a test class imported by name here would be
#: collected, and run, a second time.
_faked = test_wheels.TestTheRunWithFakedBuilds

# --- what the bundle requires ----------------------------------------------------------------


def _requirements(table: Mapping[Any, List[str]]) -> wb.Requirements:
    """``requirements(project, extra)`` answered from ``table``, keyed ``(project, extra)``; anything not in it
    requires nothing. (Keyed ``Any``: a literal table whose extras are all ``None`` is not a ``Optional[str]`` one.)"""
    return lambda project, extra: [Requirement(spec) for spec in table.get((project, extra), [])]


class TestTheBundlesRequirements:
    def test_a_bundled_projects_requirement_is_replaced_by_its_own_and_an_outside_one_is_kept(self) -> None:
        table = {
            ("app", None): ["libx>=0.1", "numpy>=1.26", "pubdep>=1.0"],
            ("libx", None): ["pyyaml>=6", "numpy>=1.24"],
        }
        requires = wb.bundle_requirements(_requirements(table), "app", [], {"app": "0.5.0", "libx": "0.1.0"})
        assert requires.base == ["pyyaml>=6", "numpy>=1.24", "numpy>=1.26", "pubdep>=1.0"]
        assert requires.extras == {} and requires.python is None

    def test_an_extra_a_requirement_names_is_followed_and_one_it_does_not_is_left_out(self) -> None:
        table = {
            ("app", None): ["libx[fast]"],
            ("libx", None): ["pyyaml"],
            ("libx", "fast"): ["orjson"],
            ("libx", "slow"): ["never-installed"],
        }
        requires = wb.bundle_requirements(_requirements(table), "app", [], {"app": "1", "libx": "1"})
        assert requires.base == ["pyyaml", "orjson"]

    def test_the_marker_of_a_requirement_reaches_what_replaces_it(self) -> None:
        table = {
            ("app", None): ['libx; sys_platform == "darwin"'],
            ("libx", None): ['mlx; platform_machine == "arm64"', "pyyaml"],
        }
        requires = wb.bundle_requirements(_requirements(table), "app", [], {"app": "1", "libx": "1"})
        assert requires.base == [
            'mlx; platform_machine == "arm64" and sys_platform == "darwin"',
            'pyyaml; sys_platform == "darwin"',
        ]

    def test_a_requirement_by_url_takes_a_marker(self) -> None:
        table = {("app", "dev"): ["torchsig @ git+https://github.com/TorchDSP/torchsig.git"]}
        requires = wb.bundle_requirements(_requirements(table), "app", ["dev"], {"app": "1"})
        assert requires.extras == {"dev": ['torchsig @ git+https://github.com/TorchDSP/torchsig.git ; extra == "dev"']}
        assert Requirement(requires.extras["dev"][0]).url == "git+https://github.com/TorchDSP/torchsig.git"

    def test_an_or_keeps_its_parentheses_when_a_marker_joins_it(self) -> None:
        table = {
            ("app", None): ['libx; python_version >= "3.11"'],
            ("libx", None): ['mlx; platform_machine == "arm64" or sys_platform == "linux"'],
        }
        requires = wb.bundle_requirements(_requirements(table), "app", ["dev"], {"app": "1", "libx": "1"})
        assert requires.base == [
            'mlx; (platform_machine == "arm64" or sys_platform == "linux") and python_version >= "3.11"'
        ]

    def test_the_projects_extras_are_the_bundles_without_what_the_base_already_requires(self) -> None:
        table = {
            ("app", None): ["libx", "pyyaml"],
            ("app", "dev"): ["pytest", "pyyaml", "libx[fast]"],
            ("libx", None): ["pyyaml"],
            ("libx", "fast"): ["orjson"],
        }
        requires = wb.bundle_requirements(_requirements(table), "app", ["dev"], {"app": "1", "libx": "1"})
        assert requires.base == ["pyyaml"]
        assert requires.extras == {"dev": ['pytest; extra == "dev"', 'orjson; extra == "dev"']}

    def test_a_project_requiring_itself_or_a_cycle_ends(self) -> None:
        table = {
            ("app", None): ["libx", "app[cli]"],
            ("app", "cli"): ["click"],
            ("libx", None): ["app", "pyyaml"],
        }
        requires = wb.bundle_requirements(_requirements(table), "app", [], {"app": "1", "libx": "1"})
        assert requires.base == ["pyyaml", "click"]

    def test_a_requirement_the_bundled_version_does_not_satisfy_is_refused_naming_both(self) -> None:
        table = {("app", None): ["libx>=0.2"]}
        with pytest.raises(click.ClickException, match=r"app requires libx>=0.2, but the bundle holds libx 0.1.0"):
            wb.bundle_requirements(_requirements(table), "app", [], {"app": "1", "libx": "0.1.0"})

    def test_a_pre_release_satisfies_a_requirement_it_falls_in(self) -> None:
        table = {("app", None): ["libx>=0.1.0a1"]}
        requires = wb.bundle_requirements(_requirements(table), "app", [], {"app": "1", "libx": "0.1.0a2"})
        assert requires.base == []

    def test_the_bundle_requires_every_projects_python(self) -> None:
        requires = wb.bundle_requirements(_requirements({}), "app", [], {"app": "1"}, [">=3.10", ">=3.11,<4"])
        assert requires.python == "<4,>=3.10,>=3.11"

    def test_the_wheel_name_is_normalized(self) -> None:
        name = wb.BUNDLE_NAME.format(distribution="raidar-ultralytics")
        assert wb.bundle_wheel_name(name, "0.1.0") == "raidar_ultralytics_bundle-0.1.0-py3-none-any.whl"
        assert wb.bundle_wheel_name("App.Core-bundle", "2") == "app_core_bundle-2-py3-none-any.whl"


# --- the merge ---------------------------------------------------------------------------------


def _part(folder: Path, name: str, files: Dict[str, str], dist_info: Optional[Dict[str, str]] = None) -> wb.BundlePart:
    """A hand-made wheel of `name` 0.1.0 holding `files` and, in its dist-info, `dist_info` (name: text)."""
    info = f"{name}-0.1.0.dist-info"
    members = {f"{info}/{leaf}": text for leaf, text in (dist_info or {}).items()}
    wheel = _wheel(folder / f"{name}-0.1.0-py3-none-any.whl", {**files, **members})
    return wb.BundlePart(name, name, wheel)


def _merge(folder: Path, parts: Sequence[wb.BundlePart], metadata: str = "Metadata-Version: 2.1\n") -> Path:
    return wb.merge_wheels(parts, "app-bundle", "0.5.0", metadata, folder / "out", w._ZIP_TIME)


def _members(wheel: Path) -> Dict[str, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class TestTheMerge:
    def test_every_wheels_files_under_one_dist_info_whose_record_holds_every_file(self, tmp_path: Path) -> None:
        parts = [
            _part(tmp_path, "app", {"app/__init__.py": "A = 1\n"}),
            _part(tmp_path, "libx", {"libx/__init__.py": "", "libx/core.py": "X = 2\n"}),
        ]
        bundle = _merge(tmp_path, parts, "Metadata-Version: 2.1\nName: app-bundle\n")

        assert bundle == tmp_path / "out" / "app_bundle-0.5.0-py3-none-any.whl"
        members = _members(bundle)
        info = "app_bundle-0.5.0.dist-info"
        assert sorted(members) == sorted(
            [
                "app/__init__.py",
                "libx/__init__.py",
                "libx/core.py",
                f"{info}/METADATA",
                f"{info}/WHEEL",
                f"{info}/RECORD",
            ]
        )
        assert members[f"{info}/METADATA"] == b"Metadata-Version: 2.1\nName: app-bundle\n"
        assert b"Tag: py3-none-any\n" in members[f"{info}/WHEEL"]
        assert b"Root-Is-Purelib: true\n" in members[f"{info}/WHEEL"]
        record = members[f"{info}/RECORD"].decode().splitlines()
        assert record[-1] == f"{info}/RECORD,,"
        for line in record[:-1]:
            path, digest, size = line.rsplit(",", 2)
            data = members[path]
            expected = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            assert (digest, int(size)) == (f"sha256={expected}", len(data)), path
        assert len(record) == len(members)

    def test_entry_points_and_top_level_names_are_merged_and_licences_kept_per_project(self, tmp_path: Path) -> None:
        parts = [
            _part(
                tmp_path,
                "app",
                {"app/__init__.py": ""},
                {
                    "entry_points.txt": "[console_scripts]\napp = app.cli:main\n",
                    "top_level.txt": "app\n",
                    "licenses/LICENSE": "MIT app\n",
                },
            ),
            _part(
                tmp_path,
                "libx",
                {"libx/__init__.py": ""},
                {
                    "entry_points.txt": "[console_scripts]\nLibX = libx.cli:main\n\n[app.plugins]\nx = libx:Plugin\n",
                    "top_level.txt": "libx\n",
                    "LICENSE.txt": "MIT libx\n",
                    "INSTALLER": "pip\n",
                },
            ),
        ]
        members = _members(_merge(tmp_path, parts))
        info = "app_bundle-0.5.0.dist-info"
        assert members[f"{info}/entry_points.txt"].decode() == (
            "[app.plugins]\nx = libx:Plugin\n\n[console_scripts]\nLibX = libx.cli:main\napp = app.cli:main\n"
        )
        assert members[f"{info}/top_level.txt"] == b"app\nlibx\n"
        assert members[f"{info}/licenses/app/LICENSE"] == b"MIT app\n"
        assert members[f"{info}/licenses/libx/LICENSE.txt"] == b"MIT libx\n"
        assert f"{info}/INSTALLER" not in members, "only what the bundle needs comes over from a dist-info"

    def test_a_data_folder_becomes_the_bundles(self, tmp_path: Path) -> None:
        parts = [_part(tmp_path, "app", {"app/__init__.py": "", "app-0.1.0.data/scripts/run": "#!/bin/sh\n"})]
        assert "app_bundle-0.5.0.data/scripts/run" in _members(_merge(tmp_path, parts))

    def test_a_file_both_wheels_hold_alike_goes_in_once(self, tmp_path: Path) -> None:
        shared = {"ns/__init__.py": "", "app/__init__.py": ""}
        parts = [_part(tmp_path, "app", shared), _part(tmp_path, "libx", {"ns/__init__.py": ""})]
        assert list(_members(_merge(tmp_path, parts))).count("ns/__init__.py") == 1

    def test_a_path_two_wheels_hold_differently_is_refused_naming_both(self, tmp_path: Path) -> None:
        parts = [
            _part(tmp_path, "app", {"common/util.py": "A = 1\n"}),
            _part(tmp_path, "libx", {"common/util.py": "A = 2\n"}),
        ]
        with pytest.raises(click.ClickException, match=r"common/util.py differs between app and libx"):
            _merge(tmp_path, parts)
        assert not (tmp_path / "out" / "app_bundle-0.5.0-py3-none-any.whl").exists()

    def test_one_entry_point_name_pointing_at_two_objects_is_refused(self, tmp_path: Path) -> None:
        parts = [
            _part(tmp_path, "app", {}, {"entry_points.txt": "[console_scripts]\nrun = app:main\n"}),
            _part(tmp_path, "libx", {}, {"entry_points.txt": "[console_scripts]\nrun = libx:main\n"}),
        ]
        with pytest.raises(click.ClickException, match=r"entry point console_scripts:run is app:main in app and libx"):
            _merge(tmp_path, parts)

    def test_an_unreadable_entry_points_file_is_named(self, tmp_path: Path) -> None:
        parts = [_part(tmp_path, "app", {}, {"entry_points.txt": "run = app:main\n"})]
        with pytest.raises(click.ClickException, match=r"app-0.1.0-py3-none-any.whl: cannot read its entry_points"):
            _merge(tmp_path, parts)

    @pytest.mark.parametrize(
        "wheel_file, said",
        [
            ("Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: cp313-cp313-macosx_14_0_arm64\n", "cp313-cp313"),
            ("Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n", "platlib"),
        ],
    )
    def test_a_wheel_that_is_not_pure_python_is_refused(self, tmp_path: Path, wheel_file: str, said: str) -> None:
        part = _part(tmp_path, "fast", {"fast/_core.so": "binary"})
        with zipfile.ZipFile(part.wheel) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["fast-0.1.0.dist-info/WHEEL"] = wheel_file.encode()
        with zipfile.ZipFile(part.wheel, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        with pytest.raises(
            click.ClickException, match=rf"fast's wheel .* is built for {said}.*pure-Python wheels only"
        ):
            _merge(tmp_path, [part])

    def test_a_wheel_without_one_dist_info_is_refused(self, tmp_path: Path) -> None:
        wheel = tmp_path / "odd-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("odd/__init__.py", "")
        with pytest.raises(click.ClickException, match="odd-0.1.0-py3-none-any.whl: expected one .dist-info folder"):
            _merge(tmp_path, [wb.BundlePart("odd", "odd", wheel)])

    def test_unchanged_wheels_merge_into_the_same_bytes(self, tmp_path: Path) -> None:
        parts = [_part(tmp_path, "app", {"app/__init__.py": ""}), _part(tmp_path, "libx", {"libx/__init__.py": ""})]
        first = _merge(tmp_path, parts).read_bytes()
        assert _merge(tmp_path, list(reversed(parts))).read_bytes() == first

    def test_the_metadata_names_the_bundle_its_requirements_and_extras(self) -> None:
        requires = wb.BundleRequirements(["pyyaml>=6"], {"dev": ['pytest; extra == "dev"']}, ">=3.11")
        text = wb.render_metadata("app-bundle", "0.5.0", "app in one wheel", requires, "# app-bundle\n")
        assert text == (
            "Metadata-Version: 2.1\nName: app-bundle\nVersion: 0.5.0\nSummary: app in one wheel\n"
            "Requires-Python: >=3.11\nRequires-Dist: pyyaml>=6\nProvides-Extra: dev\n"
            'Requires-Dist: pytest; extra == "dev"\nDescription-Content-Type: text/markdown\n\n# app-bundle\n'
        )


# --- the run, with builds and the empty venvs faked --------------------------------------------


class TestTheRunWithABundle:
    """`vendor_wheels(..., bundle=True)` without uv: each build hands back a wheel written by hand."""

    @staticmethod
    def _fake_bundle_install(
        monkeypatch: pytest.MonkeyPatch,
        error: Optional[str] = None,
        failures: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[Tuple[str, Tuple[str, ...]]]:
        installs: List[Tuple[str, Tuple[str, ...]]] = []

        def _install(
            uv: str, target: Path, bundle: Path, packages: List[str], scratch: Path
        ) -> Tuple[Optional[str], Dict[str, Dict[str, Any]], List[str]]:
            installs.append((bundle.name, tuple(packages)))
            return error, failures or {}, [] if error else [p for p in packages if p not in (failures or {})]

        monkeypatch.setattr(w, "install_bundle_and_import", _install)
        return installs

    def test_the_project_and_the_wheels_it_needs_go_into_one_wheel_beside_the_separate_ones(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`app` needs libx (not on PyPI) and pubdep (on PyPI); under --all, `other` is built but stays out."""
        root = _faked._scoped_workspace(tmp_path, monkeypatch)
        _project(root, "app", deps=["libx", "pubdep", "gone"], extras={"dev": ["pytest", "libx"]})
        _project(root, "libx", deps=["pyyaml>=6"])
        for name in ("app", "libx"):
            _commit(root / name, "requirements")
        _faked._fake_builds(monkeypatch, {"app": "0.5.0"})
        installs = self._fake_bundle_install(monkeypatch)

        report = w.vendor_wheels(root, "app", scope="all", bundle=True)

        folder = root / "app" / "wheels"
        assert sorted(p.name for p in folder.glob("*.whl")) == [
            "libx-0.1.0-py3-none-any.whl",
            "other-0.1.0-py3-none-any.whl",
            "pubdep-0.1.0-py3-none-any.whl",
        ], "the separate wheels are as they were; the project's own is not among them without --include-project"
        bundle = folder / "bundle" / "app_bundle-0.5.0-py3-none-any.whl"
        members = _members(bundle)
        assert sorted(n for n in members if not n.startswith("app_bundle-")) == [
            "app/__init__.py",
            "libx/__init__.py",
            "pubdep/__init__.py",
        ]
        metadata = members["app_bundle-0.5.0.dist-info/METADATA"].decode()
        requires = [line for line in metadata.splitlines() if line.startswith(("Requires-Dist", "Provides-Extra"))]
        assert requires == [
            "Requires-Dist: pyyaml>=6",
            "Requires-Dist: gone",
            "Provides-Extra: dev",
            'Requires-Dist: pytest; extra == "dev"',
        ]
        app_sha = _git_sha(root / "app")
        assert f"| app | app | 0.5.0 | dev/main {app_sha} |" in metadata
        assert "To regenerate: aisland wheels build app --all --bundle" in metadata
        assert report.bundle is not None
        assert (report.bundle.wheel, report.bundle.distribution, report.bundle.version, report.bundle.projects) == (
            "app/wheels/bundle/app_bundle-0.5.0-py3-none-any.whl",
            "app-bundle",
            "0.5.0",
            ["app", "libx", "pubdep"],
        )
        assert report.bundle.bytes == bundle.stat().st_size
        assert installs == [("app_bundle-0.5.0-py3-none-any.whl", ("app", "libx", "pubdep"))]
        assert report.ok
        text = w.render_report(report)
        assert (
            f"Bundle: app/wheels/bundle/app_bundle-0.5.0-py3-none-any.whl ({w.human_size(report.bundle.bytes)}) — 3 projects "
            "in one wheel (app, libx, pubdep); install it with: pip install app_bundle-0.5.0-py3-none-any.whl\n"
            "  Installed alone in an empty venv (it + PyPI): all 3 modules imported\n" in text
        )
        assert report.to_dict()["bundle"]["projects"] == ["app", "libx", "pubdep"]

    def test_without_bundle_every_file_is_what_it_was_and_an_old_bundle_goes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _faked._scoped_workspace(tmp_path, monkeypatch)
        _faked._fake_builds(monkeypatch, {"app": "0.5.0"})
        self._fake_bundle_install(monkeypatch)
        folder = root / "app" / "wheels"
        w.vendor_wheels(root, "app", bundle=True)
        with_bundle = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        assert (folder / "bundle" / "app_bundle-0.5.0-py3-none-any.whl").is_file()

        report = w.vendor_wheels(root, "app")

        assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == with_bundle
        assert not (folder / "bundle").exists(), "a bundle of older commits must not sit beside the new wheels"
        assert report.bundle is None and "Bundle:" not in w.render_report(report)

    def test_the_zip_leaves_the_bundle_out(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _faked._scoped_workspace(tmp_path, monkeypatch)
        _faked._fake_builds(monkeypatch, {})
        self._fake_bundle_install(monkeypatch)

        report = w.vendor_wheels(root, "app", bundle=True, make_zip=True)

        assert report.zip_file is not None
        with zipfile.ZipFile(root / report.zip_file) as archive:
            assert not [n for n in archive.namelist() if "bundle" in n]

    def test_the_project_is_refused_dirty_and_force_bundles_it_as_it_is(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _faked._scoped_workspace(tmp_path, monkeypatch)
        _faked._fake_builds(monkeypatch, {})
        self._fake_bundle_install(monkeypatch)
        (root / "app" / "app" / "fresh.py").write_text("")

        with pytest.raises(click.ClickException, match=r"app has changes no commit holds.*\n    \?\? app/fresh.py"):
            w.vendor_wheels(root, "app", bundle=True)
        report = w.vendor_wheels(root, "app", bundle=True, force=True)

        assert report.bundle is not None
        metadata = _members(root / report.bundle.wheel)["app_bundle-0.1.0.dist-info/METADATA"].decode()
        assert "+uncommitted |" in metadata
        assert "To regenerate: aisland wheels build app --bundle" in metadata

    def test_no_check_writes_the_bundle_and_installs_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _faked._scoped_workspace(tmp_path, monkeypatch)
        installs = _faked._fake_builds(monkeypatch, {})
        bundle_installs = self._fake_bundle_install(monkeypatch)

        report = w.vendor_wheels(root, "app", bundle=True, check=False)

        assert installs == [] and bundle_installs == []
        assert report.bundle is not None and (root / report.bundle.wheel).is_file()
        assert report.ok
        assert "  Not checked (--no-check): the bundle was not installed in an empty venv." in w.render_report(report)

    def test_a_bundle_that_does_not_install_fails_the_run_and_is_not_committed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _faked._scoped_workspace(tmp_path, monkeypatch)
        _faked._fake_builds(monkeypatch, {})
        self._fake_bundle_install(monkeypatch, error="× No solution found when resolving dependencies")

        report = w.vendor_wheels(root, "app", bundle=True, commit=True)

        assert not report.ok and report.install_error is None
        assert report.commit_note == "not committed: the install check failed"
        assert (
            "  Installing the bundle alone in an empty venv failed:\n"
            "    × No solution found when resolving dependencies\n" in w.render_report(report)
        )

    def test_a_module_the_bundle_cannot_import_fails_the_run_and_is_listed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _faked._scoped_workspace(tmp_path, monkeypatch)
        _faked._fake_builds(monkeypatch, {})
        broken = {"libx": {"error": "ModuleNotFoundError: No module named 'libx'", "missing": "libx", "frames": []}}
        self._fake_bundle_install(monkeypatch, failures=broken)

        report = w.vendor_wheels(root, "app", bundle=True)

        assert [(f.module, f.distribution) for f in report.bundle_import_failures] == [("libx", "libx")]
        assert not report.ok
        text = w.render_report(report)
        assert "  Installed alone in an empty venv (it + PyPI); these did not import:\n" in text
        assert "    libx  ModuleNotFoundError: No module named 'libx'\n    1 other module imported\n" in text

    def test_a_project_whose_dependencies_are_dynamic_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _faked._scoped_workspace(tmp_path, monkeypatch)
        pyproject = root / "libx" / "pyproject.toml"
        pyproject.write_text(pyproject.read_text().replace("dependencies = []", 'dynamic = ["dependencies"]'))
        _commit(root / "libx", "dynamic")
        _faked._fake_builds(monkeypatch, {})
        self._fake_bundle_install(monkeypatch)

        with pytest.raises(click.ClickException, match=r"libx/pyproject.toml declares its dependencies dynamic"):
            w.vendor_wheels(root, "app", bundle=True)


def _git_sha(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


class TestTheReportAndTheCli:
    def test_a_bundle_import_failure_alone_makes_the_run_fail(self) -> None:
        failure = w.ImportFailure("libx", "ModuleNotFoundError: No module named 'libx'", "libx", "libx")
        assert not _report(import_failures=[], bundle_import_failures=[failure]).ok
        assert not _report(import_failures=[], bundle_install_error="boom").ok
        assert _report(import_failures=[]).ok

    @pytest.mark.parametrize("flags, bundle", [([], False), (["--bundle"], True)])
    def test_bundle_is_handed_to_the_run(self, monkeypatch: pytest.MonkeyPatch, flags: List[str], bundle: bool) -> None:
        asked: List[bool] = []

        def _vendor(root: Path, project: str, **kwargs: Any) -> w.WheelsReport:
            asked.append(kwargs["bundle"])
            return _report(import_failures=[])

        monkeypatch.setattr(w, "vendor_wheels", _vendor)
        result = CliRunner().invoke(main, ["wheels", "build", "navigaitor", *flags])
        assert result.exit_code == 0, result.output
        assert asked == [bundle]

    @pytest.mark.parametrize("include_project", [False, True])
    @pytest.mark.parametrize("scope", ["unpublished", "needed", "all"])
    def test_the_bundles_regenerate_line_is_a_command_the_cli_accepts(
        self, scope: w.WheelsScope, include_project: bool
    ) -> None:
        from aisland.cli import wheels_build

        argv = w.bundle_command("navigaitor", scope, include_project).split()
        assert argv[:3] == ["aisland", "wheels", "build"]
        context = wheels_build.make_context("build", argv[3:])
        assert context.params["bundle"] is True
        assert context.params["include_project"] is include_project
        expected = {"unpublished": (False, False), "needed": (True, False), "all": (False, True)}[scope]
        assert (context.params["include_published"], context.params["include_all"]) == expected


# --- end to end: real git, real uv, a local folder for PyPI ----------------------------------


class TestEndToEnd:
    def test_the_bundle_installs_alone_with_plain_pip_and_pip_knows_only_its_name(
        self, tmp_path: Path, offline_uv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run checks the bundle in a venv of its own; then, installed by itself into another venv (pubdep from the
        local PyPI), app and libx import, `app` runs as a console script, and pip knows no `libx`."""
        ws = _end_to_end_workspace(tmp_path, offline_uv, monkeypatch, released_new=True)
        pyproject = ws / "app" / "pyproject.toml"
        pyproject.write_text(pyproject.read_text() + '[project.scripts]\napp = "app.cli:main"\n')
        (ws / "app" / "app" / "cli.py").write_text("import libx\n\ndef main():\n    print('app ran')\n")
        _commit(ws / "app", "a console script")

        report = w.vendor_wheels(ws, "app", bundle=True)

        assert report.ok, w.render_report(report)
        assert report.bundle is not None and report.bundle.projects == ["app", "libx"]
        assert report.bundle_imported == ["app", "app.cli", "app.core", "app.labelling", "libx"]
        venv = tmp_path / "plain-env"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, cwd=tmp_path)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PIP_CONFIG_FILE"] = os.devnull
        python = venv / "bin" / "python"
        index = ["--no-index", "--find-links", str(offline_uv)]

        installed = subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", *index, str(ws / report.bundle.wheel)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )

        assert installed.returncode == 0, installed.stderr
        probe = (
            "import importlib.metadata as m, json, app.core, libx, pubdep.newer\n"
            "names = sorted(d.metadata['Name'] for d in m.distributions())\n"
            "print(json.dumps(names))\n"
        )
        found = subprocess.run([str(python), "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert found.returncode == 0, found.stderr
        names = [n for n in json.loads(found.stdout) if n != "pip"]
        assert names == ["app-bundle", "pubdep"], "pip knows the bundle and what came from PyPI"
        ran = subprocess.run([str(venv / "bin" / "app")], cwd=tmp_path, env=env, capture_output=True, text=True)
        assert (ran.returncode, ran.stdout) == (0, "app ran\n"), ran.stderr

    @pytest.mark.slow
    def test_an_unchanged_workspace_rebuilds_the_same_bundle(
        self, tmp_path: Path, offline_uv: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _end_to_end_workspace(tmp_path, offline_uv, monkeypatch, released_new=True)
        first = w.vendor_wheels(ws, "app", bundle=True, check=False)
        assert first.bundle is not None
        digest = hashlib.sha256((ws / first.bundle.wheel).read_bytes()).hexdigest()

        second = w.vendor_wheels(ws, "app", bundle=True, check=False)

        assert second.bundle is not None
        assert hashlib.sha256((ws / second.bundle.wheel).read_bytes()).hexdigest() == digest
