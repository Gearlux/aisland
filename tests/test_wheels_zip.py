"""Tests for what `aisland wheels` does with the folder once it is built: git (ignored unless `--commit`, then
committed by the command), the zip that carries it, and installing that zip — with aisland, or with the zip's
own `__main__.py` and plain `python` on a machine without the workspace.

The builds and the empty-venv check are faked as in `test_wheels.py::TestTheRunWithFakedBuilds`; the zip is
run with the real interpreter.
"""

from __future__ import annotations

import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click
import pytest
from click.testing import CliRunner

from aisland import wheels as w
from aisland.cli import main

from .test_wheels import TestTheRunWithFakedBuilds, _commit, _git, _project, _pypi, _report, _workspace

_fake_builds = TestTheRunWithFakedBuilds._fake_builds


def _app_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`app` needs `libx`, which PyPI lacks; both are committed git checkouts."""
    root = _workspace(tmp_path / "ws", "app", "libx")
    _project(root, "app", deps=["libx"])
    _project(root, "libx")
    _commit(root / "app")
    _commit(root / "libx")
    _pypi(monkeypatch, {})
    return root


def _tracked(repo: Path, path: str = "wheels") -> List[str]:
    return [line for line in _git(repo, "ls-files", "--", path).splitlines() if line]


# --- git: ignored by default, committed by the command on --commit ---------------------------------


class TestGit:
    def test_by_default_the_folder_ignores_itself_and_nothing_is_committed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {})
        head = _git(root / "app", "rev-parse", "HEAD")

        report = w.vendor_wheels(root, "app")

        assert (root / "app" / "wheels" / ".gitignore").read_text() == "*\n"
        assert _git(root / "app", "status", "--porcelain") == ""
        assert _git(root / "app", "rev-parse", "HEAD") == head
        assert report.committed is None and report.commit_note is None

    def test_commit_commits_the_folder_without_the_zip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {})

        report = w.vendor_wheels(root, "app", commit=True, make_zip=True)

        app = root / "app"
        assert (app / "wheels" / ".gitignore").read_text() == "*.zip\n"
        assert _tracked(app) == [
            "wheels/.gitignore",
            "wheels/README.md",
            "wheels/install.py",
            "wheels/libx-0.1.0-py3-none-any.whl",
            "wheels/wheels.yaml",
        ]
        assert (app / "wheels" / "app-wheels.zip").is_file()
        assert report.committed == _git(app, "rev-parse", "--short", "HEAD").strip()
        assert _git(app, "log", "-1", "--format=%s").strip() == "chore: wheels built with aisland wheels build app"
        assert _git(app, "show", "--name-only", "--format=", "HEAD").split() == _tracked(app)
        assert f"Committed app/wheels: {report.committed}" in w.render_report(report)

    def test_commit_takes_only_the_wheels_folder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {})
        (root / "app" / "notes.txt").write_text("the user's own work in progress\n")

        w.vendor_wheels(root, "app", commit=True)

        assert _git(root / "app", "status", "--porcelain") == "?? notes.txt\n"

    def test_a_rerun_with_nothing_new_commits_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {})
        w.vendor_wheels(root, "app", commit=True)
        head = _git(root / "app", "rev-parse", "HEAD")

        report = w.vendor_wheels(root, "app", commit=True)

        assert _git(root / "app", "rev-parse", "HEAD") == head
        assert report.committed is None
        assert report.commit_note == "nothing new to commit in app/wheels"

    def test_a_rerun_after_the_clock_moved_commits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two runs ten seconds apart: what decided the test above when the runs straddled a two-second step
        (a zip member's time has two-second steps)."""
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {})
        w.vendor_wheels(root, "app", commit=True)
        head = _git(root / "app", "rev-parse", "HEAD")
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now + 10)  # zipfile stamps a member from time.time()

        report = w.vendor_wheels(root, "app", commit=True)

        assert _git(root / "app", "rev-parse", "HEAD") == head
        assert report.commit_note == "nothing new to commit in app/wheels"

    def test_a_failed_check_commits_nothing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {}, {"libx": {"error": "ModuleNotFoundError: No module named 'x'", "missing": "x"}})
        head = _git(root / "app", "rev-parse", "HEAD")

        report = w.vendor_wheels(root, "app", commit=True)

        assert report.ok is False
        assert _git(root / "app", "rev-parse", "HEAD") == head
        assert report.commit_note == "not committed: the install check failed"
        assert "Not committed: the install check failed" in w.render_report(report)

    def test_a_tracked_folder_without_commit_is_reported_with_the_command_that_untracks_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {})
        w.vendor_wheels(root, "app", commit=True)

        report = w.vendor_wheels(root, "app")

        assert (root / "app" / "wheels" / ".gitignore").read_text() == "*\n"
        assert report.tracked_warning == (
            "wheels/ is tracked in app (5 files); stop tracking it with: git -C app rm -r --cached wheels"
        )
        assert report.tracked_warning in w.render_report(report)


# --- the zip -----------------------------------------------------------------------------------------


def _built_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[Path, Path]:
    root = _app_workspace(tmp_path, monkeypatch)
    _fake_builds(monkeypatch, {})
    w.vendor_wheels(root, "app")
    return root, root / "app" / "wheels"


def _standalone_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tuple[Path, Path]:
    """`app`'s folder built with `--include-project`: libx's wheel and app's own."""
    root = _app_workspace(tmp_path, monkeypatch)
    _fake_builds(monkeypatch, {})
    w.vendor_wheels(root, "app", include_project=True)
    return root, root / "app" / "wheels"


class TestZip:
    def test_the_zip_holds_the_folder_and_a_main_that_runs_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, folder = _built_folder(tmp_path, monkeypatch)

        archive = w.zip_wheels(root, "app")

        assert archive == folder / "app-wheels.zip"
        names = zipfile.ZipFile(archive).namelist()
        assert names == [
            "__main__.py",
            "wheels/.gitignore",
            "wheels/README.md",
            "wheels/install.py",
            "wheels/libx-0.1.0-py3-none-any.whl",
            "wheels/wheels.yaml",
        ]
        main_py = zipfile.ZipFile(archive).read("__main__.py").decode()
        assert main_py == w.render_unzip_script("app")

    def test_a_rezip_of_the_same_folder_is_the_same_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, _folder = _built_folder(tmp_path, monkeypatch)
        first = w.zip_wheels(root, "app").read_bytes()
        assert w.zip_wheels(root, "app").read_bytes() == first, "a zip of an unchanged folder must not change"

    def test_build_with_zip_writes_it_and_reports_how_to_run_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {})

        report = w.vendor_wheels(root, "app", make_zip=True)

        assert report.zip_file == "app/wheels/app-wheels.zip"
        text = w.render_report(report)
        assert "Zip: app/wheels/app-wheels.zip (" in text
        assert "install it from the app folder with: python app-wheels.zip" in text

    def test_a_folder_never_built_is_refused_with_the_build_command(self, tmp_path: Path) -> None:
        root = _workspace(tmp_path / "ws", "app")
        _project(root, "app")
        with pytest.raises(
            click.ClickException, match=r"app/wheels has no wheels\.yaml; build it first: aisland wheels build app"
        ):
            w.zip_wheels(root, "app")

    def test_a_folder_with_the_projects_own_wheel_makes_a_zip_that_runs_anywhere(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _app_workspace(tmp_path, monkeypatch)
        _fake_builds(monkeypatch, {})

        report = w.vendor_wheels(root, "app", include_project=True, make_zip=True)

        archive = root / "app" / "wheels" / "app-wheels.zip"
        assert "wheels/app-0.1.0-py3-none-any.whl" in zipfile.ZipFile(archive).namelist()
        assert zipfile.ZipFile(archive).read("__main__.py").decode() == w.render_unzip_script("app", True)
        assert report.zip_standalone
        assert "install it from any folder with: python app-wheels.zip" in w.render_report(report)

    def test_an_own_wheel_marked_install_false_makes_a_zip_for_the_project_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, folder = _standalone_folder(tmp_path, monkeypatch)
        listing = folder / "wheels.yaml"
        text = listing.read_text()
        assert text.endswith("    install: true\nnot_built: []\n")
        listing.write_text(text[: -len("    install: true\nnot_built: []\n")] + "    install: false\nnot_built: []\n")

        archive = w.zip_wheels(root, "app")

        assert zipfile.ZipFile(archive).read("__main__.py").decode() == w.render_unzip_script("app", False)
        assert not w.installs_own_wheel(folder)

    def test_an_unreadable_wheels_yaml_stops_the_zip_at_its_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, folder = _built_folder(tmp_path, monkeypatch)
        (folder / "wheels.yaml").write_text('project: "app"\nwheels: nope\n')
        with pytest.raises(click.ClickException, match=r"wheels\.yaml:2: `wheels:` must be a list"):
            w.zip_wheels(root, "app")

    def test_the_main_is_the_template_under_a_header_naming_its_project(self) -> None:
        script = w.render_unzip_script("navigaitor")
        assert "\nSTANDALONE = False\n" in script
        assert "\nSTANDALONE = True\n" in w.render_unzip_script("navigaitor", True)
        assert '\nPROJECT = "navigaitor"\n' in script
        assert "# Generated by: aisland wheels zip navigaitor\n" in script
        assert "THE TEMPLATE" not in script


# --- running the zip with plain python ----------------------------------------------------------------


def _clone(tmp_path: Path, name: str = "app") -> Path:
    """The project's folder on the other machine: its pyproject only, and a stale wheel from an earlier zip."""
    clone = tmp_path / "elsewhere" / "app"
    clone.mkdir(parents=True)
    (clone / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n')
    (clone / "wheels").mkdir()
    (clone / "wheels" / "libx-0.0.9-py3-none-any.whl").write_text("old")
    (clone / "wheels" / "notes.txt").write_text("mine")
    return clone


def _run_zip(archive: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(archive), *args], cwd=cwd, capture_output=True, text=True)


class TestRunningTheZip:
    def test_unpack_only_replaces_the_old_wheels_and_keeps_other_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, _folder = _built_folder(tmp_path, monkeypatch)
        archive = w.zip_wheels(root, "app")
        clone = _clone(tmp_path)

        done = _run_zip(archive, clone, "--unpack-only")

        assert done.returncode == 0, done.stderr
        assert sorted(p.name for p in (clone / "wheels").iterdir()) == [
            ".gitignore",
            "README.md",
            "install.py",
            "libx-0.1.0-py3-none-any.whl",
            "notes.txt",
            "wheels.yaml",
        ]
        assert done.stdout == f"unpacked app-wheels.zip into {clone / 'wheels'}\n"

    def test_the_rest_of_the_line_goes_to_install_py(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, _folder = _built_folder(tmp_path, monkeypatch)
        archive = w.zip_wheels(root, "app")
        clone = _clone(tmp_path)

        done = _run_zip(archive, clone, "--dry-run")

        assert done.returncode == 0, done.stderr
        wheel = clone / "wheels" / "libx-0.1.0-py3-none-any.whl"
        assert done.stdout.splitlines()[-1] == (
            f"$ {sys.executable} -m pip install --find-links {clone / 'wheels'} -e {clone} {wheel}"
        )

    def test_into_names_the_project_folder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, _folder = _built_folder(tmp_path, monkeypatch)
        archive = w.zip_wheels(root, "app")
        clone = _clone(tmp_path)

        done = _run_zip(archive, tmp_path, "--into", str(clone), "--unpack-only")

        assert done.returncode == 0, done.stderr
        assert (clone / "wheels" / "libx-0.1.0-py3-none-any.whl").is_file()

    def test_another_projects_folder_is_refused_naming_both(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, _folder = _built_folder(tmp_path, monkeypatch)
        archive = w.zip_wheels(root, "app")
        clone = _clone(tmp_path, name="annotaide")

        done = _run_zip(archive, clone, "--unpack-only")

        assert done.returncode == 2
        assert done.stderr == (
            f"app-wheels.zip: run it from the app folder, or name it with --into "
            f"({clone / 'pyproject.toml'} names annotaide)\n"
        )
        assert (clone / "wheels" / "libx-0.0.9-py3-none-any.whl").is_file(), "nothing may be unpacked"

    def test_a_folder_without_a_pyproject_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, _folder = _built_folder(tmp_path, monkeypatch)
        archive = w.zip_wheels(root, "app")
        empty = tmp_path / "empty"
        empty.mkdir()

        done = _run_zip(archive, empty, "--unpack-only")

        assert done.returncode == 2
        assert done.stderr == (
            f"app-wheels.zip: run it from the app folder, or name it with --into ({empty / 'pyproject.toml'} is missing)\n"
        )

    def test_a_zip_with_the_projects_own_wheel_installs_from_a_folder_of_no_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, _folder = _standalone_folder(tmp_path, monkeypatch)
        archive = w.zip_wheels(root, "app")
        anywhere = tmp_path / "anywhere"
        anywhere.mkdir()

        done = _run_zip(archive, anywhere, "--dry-run")

        assert done.returncode == 0, done.stderr
        wheels = anywhere / "wheels"
        assert done.stdout.splitlines()[-1] == (
            f"$ {sys.executable} -m pip install --find-links {wheels} {wheels / 'libx-0.1.0-py3-none-any.whl'} "
            f"{wheels / 'app-0.1.0-py3-none-any.whl'}"
        )
        again = _run_zip(archive, anywhere, "--unpack-only")
        assert again.returncode == 0, "a folder holding this project's own unpacked wheels takes them again"

    def test_into_a_new_folder_is_made_for_a_zip_with_the_projects_own_wheel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, _folder = _standalone_folder(tmp_path, monkeypatch)
        target = tmp_path / "new" / "place"

        done = _run_zip(w.zip_wheels(root, "app"), tmp_path, "--into", str(target), "--unpack-only")

        assert done.returncode == 0, done.stderr
        assert (target / "wheels" / "app-0.1.0-py3-none-any.whl").is_file()

    def test_a_zip_with_the_projects_own_wheel_still_refuses_another_projects_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, _folder = _standalone_folder(tmp_path, monkeypatch)
        clone = _clone(tmp_path, name="annotaide")

        done = _run_zip(w.zip_wheels(root, "app"), clone, "--unpack-only")

        assert done.returncode == 2
        assert done.stderr == (
            "app-wheels.zip: run it from the app folder or a folder of no project, or name it with --into "
            f"({clone / 'pyproject.toml'} names annotaide)\n"
        )
        assert (clone / "wheels" / "libx-0.0.9-py3-none-any.whl").is_file(), "nothing may be unpacked"

    @pytest.mark.parametrize("listing", [None, 'project: "annotaide"\nwheels: []\n'])
    def test_a_zip_with_the_projects_own_wheel_refuses_a_wheels_folder_of_someone_elses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: Optional[str]
    ) -> None:
        root, _folder = _standalone_folder(tmp_path, monkeypatch)
        anywhere = tmp_path / "anywhere"
        (anywhere / "wheels").mkdir(parents=True)
        (anywhere / "wheels" / "theirs-1.0-py3-none-any.whl").write_text("theirs")
        if listing is not None:
            (anywhere / "wheels" / "wheels.yaml").write_text(listing)

        done = _run_zip(w.zip_wheels(root, "app"), anywhere, "--unpack-only")

        assert done.returncode == 2
        owner = "annotaide" if listing else "something else"
        assert done.stderr == (
            f"app-wheels.zip: {anywhere / 'wheels'} holds the wheels of {owner}, which this zip would replace; "
            "run it from another folder, or name one with --into\n"
        )
        assert sorted(p.name for p in (anywhere / "wheels").glob("*.whl")) == ["theirs-1.0-py3-none-any.whl"]

    def test_into_without_a_folder_is_a_usage_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root, _folder = _built_folder(tmp_path, monkeypatch)
        done = _run_zip(w.zip_wheels(root, "app"), tmp_path, "--into")
        assert done.returncode == 2
        assert done.stderr == "app-wheels.zip: --into needs a folder\n"


# --- aisland wheels install ---------------------------------------------------------------------------


def _fake_python(folder: Path) -> Path:
    """An interpreter that logs its arguments."""
    folder.mkdir(parents=True, exist_ok=True)
    python = folder / "python"
    python.write_text(f'#!/bin/sh\necho "$@" >> {folder / "python.log"}\nexit 0\n')
    python.chmod(0o755)
    return python


class TestInstallZip:
    def test_the_zip_runs_in_the_interpreter_named_with_into_and_the_rest(self, tmp_path: Path) -> None:
        python = _fake_python(tmp_path / "otherenv" / "bin")
        archive = tmp_path / "app-wheels.zip"
        archive.write_bytes(b"")

        status = w.install_zip(archive, python, tmp_path / "app", ["--dry-run"], workspace_root=tmp_path / "ws")

        assert status == 0
        logged = (python.parent / "python.log").read_text().split()
        assert logged == [str(archive), "--into", str(tmp_path / "app"), "--dry-run"]

    def test_the_workspaces_own_venv_is_refused(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        python = _fake_python(workspace / ".venv" / "bin")
        archive = tmp_path / "app-wheels.zip"
        archive.write_bytes(b"")
        with pytest.raises(
            click.ClickException, match=r"is the workspace's \.venv; install the zip into another environment"
        ):
            w.install_zip(archive, python, tmp_path / "app", [], workspace_root=workspace)

    def test_a_missing_zip_is_named(self, tmp_path: Path) -> None:
        python = _fake_python(tmp_path / "otherenv" / "bin")
        with pytest.raises(click.ClickException, match=r"no zip at .*nope\.zip"):
            w.install_zip(tmp_path / "nope.zip", python, tmp_path, [], workspace_root=tmp_path / "ws")


# --- the command line: the group and the short forms ----------------------------------------------------


class TestCli:
    @staticmethod
    def _vendor(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, object]]:
        calls: List[Dict[str, object]] = []

        def _vendor(
            root: Path,
            project: str,
            scope: str,
            commit: bool,
            make_zip: bool,
            force: bool,
            check: bool,
            include_project: bool,
            bundle: bool,
            extra: Optional[str],
        ) -> w.WheelsReport:
            calls.append({"project": project, "scope": scope, "commit": commit, "make_zip": make_zip})
            return _report(import_failures=[])

        monkeypatch.setattr(w, "vendor_wheels", _vendor)
        return calls

    @pytest.mark.parametrize(
        "argv",
        [
            ["wheels", "build", "navigaitor", "--all", "--zip", "--commit"],
            ["wheels", "navigaitor", "--all", "--zip", "--commit"],
        ],
    )
    def test_build_and_its_short_form(self, monkeypatch: pytest.MonkeyPatch, argv: List[str]) -> None:
        calls = self._vendor(monkeypatch)
        result = CliRunner().invoke(main, argv)
        assert result.exit_code == 0, result.output
        assert calls == [{"project": "navigaitor", "scope": "all", "commit": True, "make_zip": True}]

    def test_zip(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        asked: List[str] = []

        def _zip(root: Path, project: str) -> Path:
            asked.append(project)
            return tmp_path / "navigaitor-wheels.zip"

        monkeypatch.setattr(w, "zip_wheels", _zip)
        result = CliRunner().invoke(main, ["wheels", "zip", "navigaitor"])
        assert result.exit_code == 0, result.output
        assert asked == ["navigaitor"]
        assert str(tmp_path / "navigaitor-wheels.zip") in result.output
        assert "install it from the navigaitor folder with: python navigaitor-wheels.zip" in result.output

    def test_zip_of_a_folder_with_the_projects_own_wheel_says_any_folder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, _folder = _standalone_folder(tmp_path, monkeypatch)
        monkeypatch.setattr("aisland.cli.get_workspace_root", lambda: root)
        result = CliRunner().invoke(main, ["wheels", "zip", "app"])
        assert result.exit_code == 0, result.output
        assert "install it from any folder with: python app-wheels.zip" in result.output

    @pytest.mark.parametrize(
        "argv, rest",
        [
            (
                ["wheels", "install", "{zip}", "--python", "{python}", "--into", "{into}", "--", "--dry-run"],
                ["--dry-run"],
            ),
            (["wheels", "--install", "{zip}", "--python", "{python}", "--into", "{into}"], []),
        ],
    )
    def test_install_and_its_short_form(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: List[str], rest: List[str]
    ) -> None:
        archive = tmp_path / "app-wheels.zip"
        archive.write_bytes(b"")
        python = _fake_python(tmp_path / "env" / "bin")
        calls: List[Tuple[Path, Path, Path, List[str]]] = []

        def _install(
            zip_path: Path, interpreter: Path, into: Path, args: List[str], workspace_root: Optional[Path] = None
        ) -> int:
            calls.append((zip_path, interpreter, into, list(args)))
            return 0

        monkeypatch.setattr(w, "install_zip", _install)
        filled = [a.format(zip=archive, python=python, into=tmp_path / "app") for a in argv]
        result = CliRunner().invoke(main, filled)
        assert result.exit_code == 0, result.output
        assert calls == [(archive, python, tmp_path / "app", rest)]

    def test_the_bare_group_says_what_it_takes(self) -> None:
        result = CliRunner().invoke(main, ["wheels"])
        assert result.exit_code == 2
        assert "aisland wheels build PROJECT" in result.output
