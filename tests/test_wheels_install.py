"""Tests for the install script `aisland wheels` writes into `<project>/wheels/` (aisland/wheels_install.py).

Unit tests drive `main(..., folder=...)` with `--dry-run` over a hand-written `wheels.yaml`; one test runs the
script with the real pip, offline, to prove the measured rule it rests on: a wheel named by its file wins over
an index's wheel of the same version.
"""

from __future__ import annotations

import importlib.metadata
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

import pytest

from aisland import wheels_install as wi

_YAML = """\
# a comment the user may keep
project: app
wheels:
  - wheel: "liba-0.1.0-py3-none-any.whl"
    project: "liba"
    commit: "dev/main 1111111"
    needed: true
    on_pypi: "0.1.0"
    install: true
  - wheel: "libb-0.2.0-py3-none-any.whl"
    project: "libb"
    commit: "dev/main 2222222"
    needed: true
    on_pypi: null
    install: false
  - wheel: "extra-1.0-py3-none-any.whl"
    project: "extra"
    commit: "dev/main 3333333"
    needed: false
    on_pypi: null
    install: false
not_built: []
"""


def _folder(tmp_path: Path, text: str = _YAML, wheels: bool = True) -> Path:
    folder = tmp_path / "app" / "wheels"
    folder.mkdir(parents=True)
    (folder / "wheels.yaml").write_text(text)
    if wheels:
        for name in ("liba-0.1.0-py3-none-any.whl", "libb-0.2.0-py3-none-any.whl", "extra-1.0-py3-none-any.whl"):
            (folder / name).write_text("")
    return folder


def _dry(folder: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> List[str]:
    assert wi.main(["--dry-run", *argv], folder=folder) == 0
    return capsys.readouterr().out.splitlines()


class TestReadWheels:
    def test_the_entries_and_where_each_starts(self, tmp_path: Path) -> None:
        project, entries = wi.read_wheels(_folder(tmp_path) / "wheels.yaml")
        assert project == "app"
        assert [(e.project, e.needed, e.install, e.line) for e in entries] == [
            ("liba", True, True, 4),
            ("libb", True, False, 10),
            ("extra", False, False, 16),
        ]

    def test_an_install_that_is_not_true_or_false_is_named_at_its_line(self, tmp_path: Path) -> None:
        folder = _folder(tmp_path, _YAML.replace("    install: false\n", "    install: maybe\n", 1))
        with pytest.raises(wi.WheelsFileError, match=r"wheels\.yaml:15: `install:` must be true or false, not 'maybe'"):
            wi.read_wheels(folder / "wheels.yaml")

    def test_a_missing_field_is_named_at_its_entry(self, tmp_path: Path) -> None:
        folder = _folder(tmp_path, _YAML.replace('    project: "libb"\n', ""))
        with pytest.raises(wi.WheelsFileError, match=r"wheels\.yaml:10: `project:` must be a name, not None"):
            wi.read_wheels(folder / "wheels.yaml")

    def test_broken_yaml_is_named_at_its_line_and_column(self, tmp_path: Path) -> None:
        folder = _folder(tmp_path, "project: app\nwheels: [\n")
        with pytest.raises(wi.WheelsFileError, match=r"wheels\.yaml:3:1: "):
            wi.read_wheels(folder / "wheels.yaml")

    def test_a_file_without_a_project_is_refused(self, tmp_path: Path) -> None:
        folder = _folder(tmp_path, "- just a list\n")
        with pytest.raises(wi.WheelsFileError, match=r"wheels\.yaml:1: expected a mapping"):
            wi.read_wheels(folder / "wheels.yaml")

    def test_wheels_that_is_not_a_list_is_refused_at_its_line(self, tmp_path: Path) -> None:
        folder = _folder(tmp_path, "project: app\nwheels: nope\n")
        with pytest.raises(wi.WheelsFileError, match=r"wheels\.yaml:2: `wheels:` must be a list"):
            wi.read_wheels(folder / "wheels.yaml")

    def test_a_wheel_that_is_not_a_mapping_is_refused_at_its_line(self, tmp_path: Path) -> None:
        folder = _folder(tmp_path, "project: app\nwheels:\n  - just-a-name\n")
        with pytest.raises(wi.WheelsFileError, match=r"wheels\.yaml:3: a wheel must be a mapping"):
            wi.read_wheels(folder / "wheels.yaml")

    def test_no_wheels_is_an_empty_list(self, tmp_path: Path) -> None:
        assert wi.read_wheels(_folder(tmp_path, "project: app\nwheels: []\n") / "wheels.yaml") == ("app", [])


class TestTheCommands:
    def test_by_default_the_wheels_marked_install_then_the_project_each_named_by_its_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        folder = _folder(tmp_path)
        lines = _dry(folder, capsys)
        assert lines == [
            f"$ {sys.executable} -m pip install --find-links {folder} -e {folder.parent} "
            f"{folder / 'liba-0.1.0-py3-none-any.whl'}"
        ]

    def test_all_needed_and_only_choose_the_wheels(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        folder = _folder(tmp_path)
        assert _dry(folder, capsys, "--all")[0].endswith(
            " ".join(
                str(folder / n)
                for n in ("liba-0.1.0-py3-none-any.whl", "libb-0.2.0-py3-none-any.whl", "extra-1.0-py3-none-any.whl")
            )
        )
        assert _dry(folder, capsys, "--needed")[0].endswith(
            f"{folder / 'liba-0.1.0-py3-none-any.whl'} {folder / 'libb-0.2.0-py3-none-any.whl'}"
        )
        assert _dry(folder, capsys, "--only", "extra, libb")[0].endswith(
            f"-e {folder.parent} {folder / 'extra-1.0-py3-none-any.whl'} {folder / 'libb-0.2.0-py3-none-any.whl'}"
        )

    def test_no_project_reinstall_and_the_passthrough(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        folder = _folder(tmp_path)
        wheel = folder / "liba-0.1.0-py3-none-any.whl"
        lines = _dry(folder, capsys, "--no-project", "--reinstall", "--", "--index-url", "https://example/simple")
        assert lines == [
            f"$ {sys.executable} -m pip install --index-url https://example/simple --force-reinstall --no-deps {wheel}",
            f"$ {sys.executable} -m pip install --index-url https://example/simple --find-links {folder} {wheel}",
        ]

    def test_uv_installs_into_the_interpreter_running_the_script(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        folder = _folder(tmp_path)
        monkeypatch.setattr(wi.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
        lines = _dry(folder, capsys, "--installer", "uv", "--reinstall")
        wheel = folder / "liba-0.1.0-py3-none-any.whl"
        assert lines == [
            f"$ /bin/uv pip install --python {sys.executable} --reinstall --no-deps {wheel}",
            f"$ /bin/uv pip install --python {sys.executable} --find-links {folder} -e {folder.parent} {wheel}",
        ]

    def test_uv_missing_from_path_is_said_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wi.shutil, "which", lambda name: None)
        assert wi.main(["--installer", "uv"], folder=_folder(tmp_path)) == 2
        assert capsys.readouterr().err == "install.py: --installer uv needs `uv` on PATH\n"

    def test_an_unknown_name_lists_the_wheels_there_are(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert wi.main(["--dry-run", "--only", "recordstrem"], folder=_folder(tmp_path)) == 2
        assert capsys.readouterr().err == (
            "install.py: no wheel for 'recordstrem' in wheels.yaml (have: extra, liba, libb)\n"
        )

    def test_a_listed_wheel_missing_from_the_folder_is_named_with_its_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        folder = _folder(tmp_path, wheels=False)
        assert wi.main(["--dry-run"], folder=folder) == 2
        assert capsys.readouterr().err == (
            f"install.py: {folder / 'liba-0.1.0-py3-none-any.whl'} is listed at wheels.yaml:4 but missing\n"
        )

    def test_nothing_chosen_and_no_project_is_nothing_to_do(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        folder = _folder(tmp_path, "project: app\nwheels: []\n")
        assert _dry(folder, capsys, "--no-project") == ["install.py: nothing to install"]

    def test_a_failing_install_stops_with_its_status(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: List[List[str]] = []

        def _run(command: List[str]) -> subprocess.CompletedProcess[str]:
            ran.append(command)
            return subprocess.CompletedProcess(command, 7)

        monkeypatch.setattr(wi.subprocess, "run", _run)
        assert wi.main(["--reinstall"], folder=_folder(tmp_path)) == 7
        assert len(ran) == 1, "the install must not run after the reinstall failed"

    def test_without_pyyaml_it_is_installed_first_with_the_passthrough(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: List[List[str]] = []

        def _run(command: List[str]) -> subprocess.CompletedProcess[str]:
            ran.append(command)
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(wi.importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr(wi.subprocess, "run", _run)
        assert wi.main(["--", "--no-index"], folder=_folder(tmp_path)) == 0
        assert ran[0] == [sys.executable, "-m", "pip", "install", "--no-index", "pyyaml"]
        assert ran[1][:5] == [sys.executable, "-m", "pip", "install", "--no-index"]

    def test_a_failed_pyyaml_install_stops(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wi.importlib.util, "find_spec", lambda name: None)
        monkeypatch.setattr(wi.subprocess, "run", lambda command: subprocess.CompletedProcess(command, 1))
        assert wi.main([], folder=_folder(tmp_path)) == 1

    def test_a_dry_run_without_pyyaml_installs_nothing_and_says_why(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wi.importlib.util, "find_spec", lambda name: None)
        assert wi.main(["--dry-run"], folder=_folder(tmp_path)) == 2
        assert "--dry-run needs PyYAML" in capsys.readouterr().out


# --- the real pip, offline ------------------------------------------------------------------------


def _repackaged(distribution_name: str, folder: Path) -> None:
    """This interpreter's copy of a distribution, repackaged as a wheel an offline install can take."""
    distribution = importlib.metadata.distribution(distribution_name)
    base = Path(str(distribution.locate_file("")))
    name = distribution.metadata["Name"].replace("-", "_")
    with zipfile.ZipFile(folder / f"{name}-{distribution.version}-py3-none-any.whl", "w") as archive:
        for file in distribution.files or []:
            relative = str(file)
            if relative.startswith("..") or "__pycache__" in relative or not (base / file).is_file():
                continue
            archive.write(base / file, relative)


def _tiny_wheel(path: Path, files: dict[str, str]) -> Path:
    name, version = path.name.split("-")[:2]
    info = f"{name}-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        for member, text in files.items():
            archive.writestr(member, text)
        archive.writestr(f"{info}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        archive.writestr(
            f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        )
        archive.writestr(f"{info}/RECORD", "")
    return path


def _simple_index(root: Path, wheels: List[Path]) -> str:
    """A PEP 503 index on disk holding `wheels`; returns its `file:` URL — an index, as PyPI is, not a folder."""
    for wheel in wheels:
        project = root / wheel.name.split("-")[0].lower().replace("_", "-")
        project.mkdir(parents=True, exist_ok=True)
        wheel.rename(project / wheel.name)
        (project / "index.html").write_text(f'<a href="{wheel.name}">{wheel.name}</a>\n')
    return root.as_uri() + "/"


def _offline_venv(tmp_path: Path) -> tuple[Path, List[str]]:
    """A venv with pip, and the pip options that make a local index stand in for PyPI: a release of aislandprobe 1.0
    WITHOUT `aislandprobe.newer`, and PyYAML."""
    staging = tmp_path / "staging"
    staging.mkdir()
    _repackaged("PyYAML", staging)
    _tiny_wheel(staging / "aislandprobe-1.0-py3-none-any.whl", {"aislandprobe/__init__.py": ""})
    index = _simple_index(tmp_path / "simple", sorted(staging.iterdir()))
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    return venv / "bin" / "python", ["--index-url", index, "--disable-pip-version-check", "--quiet"]


def _wheels_folder(tmp_path: Path) -> Path:
    """`app/wheels/` holding aislandprobe 1.0 built from a checkout ahead of the release: it has `aislandprobe.newer`."""
    folder = tmp_path / "app" / "wheels"
    folder.mkdir(parents=True)
    _tiny_wheel(
        folder / "aislandprobe-1.0-py3-none-any.whl", {"aislandprobe/__init__.py": "", "aislandprobe/newer.py": ""}
    )
    (folder / "wheels.yaml").write_text(
        'project: app\nwheels:\n  - wheel: "aislandprobe-1.0-py3-none-any.whl"\n    project: "aislandprobe"\n'
        "    needed: true\n    install: true\n"
    )
    (folder / "install.py").write_text(Path(wi.__file__).read_text())
    return folder


def _isolated() -> Dict[str, str]:
    """The environment without the user's pip configuration: an `extra-index-url` in `~/.pip/pip.conf`, which
    `--index-url` does not replace, served another project's package under a common name (it asked for pandas)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    return {**env, "PIP_CONFIG_FILE": os.devnull}


def _has_newer(python: Path) -> bool:
    probe = "import importlib.util as u; print(u.find_spec('aislandprobe.newer') is not None)"
    return subprocess.run([str(python), "-c", probe], capture_output=True, text=True).stdout.strip() == "True"


def test_pip_takes_the_indexs_wheel_over_one_it_finds_only_through_find_links(tmp_path: Path) -> None:
    """Why the script names files: the same version on the index and in the folder, and pip takes the index's."""
    python, index = _offline_venv(tmp_path)
    folder = _wheels_folder(tmp_path)

    done = subprocess.run(
        [str(python), "-m", "pip", "install", *index, "--find-links", str(folder), "aislandprobe"],
        capture_output=True,
        text=True,
        env=_isolated(),
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert not _has_newer(python)


def test_the_script_installs_the_folders_wheel_over_the_indexs_of_the_same_version(tmp_path: Path) -> None:
    python, index = _offline_venv(tmp_path)
    folder = _wheels_folder(tmp_path)

    done = subprocess.run(
        [str(python), str(folder / "install.py"), "--no-project", "--", *index],
        capture_output=True,
        text=True,
        env=_isolated(),
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert _has_newer(python)
