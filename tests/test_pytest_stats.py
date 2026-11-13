"""The workspace's test-run statistics: the pytest plugin that records every run (`aisland.pytest_stats`).

Root AGENTS.md "Two Test Runs" (user ruling 2026-10-05). The plugin is driven in-process through `pytester`, inside a
fake workspace (a `.gitmodules` beside a project folder), with the module's workspace root and stats folder pointed at
tmp paths — no test writes to the real `aisland/data/test-stats/`. The installed entry point is blocked in those inner
runs (`-p no:aisland_test_stats`) and the module handed in as a plugin object, so they do not depend on whether the
editable install has picked up the entry point yet; one test pins the entry point itself.
"""

import importlib.metadata
import json
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from aisland import locations, paths, pytest_stats
from aisland.ci import stats_report
from tests.workspace_layout import needs_workspace

pytest_plugins = ["pytester"]

_PYPROJECT = """\
[project]
name = "proj"
version = "0.1.0"

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["slow: the slow run"]
addopts = "-m 'not slow'"
"""

_PYPROJECT_WITHOUT_SLOW = """\
[project]
name = "proj"
version = "0.1.0"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""

_TESTS = """\
import pytest
from aisland.workspace import projects


def test_passes():
    pass


def test_fails():
    assert False


@pytest.mark.skip(reason="not here")
def test_skipped():
    pass


@pytest.fixture
def broken():
    raise RuntimeError("setup")


def test_errors(broken):
    pass


@pytest.mark.slow
def test_slow_one():
    pass


class TestGroup:
    @pytest.mark.parametrize("n", [1, 2])
    def test_param(self, n):
        pass
"""


@pytest.fixture
def project(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`<ws>/proj`, a workspace project, the cwd; the module's workspace root and stats folder point into `<ws>`."""
    root = pytester.path
    (root / ".gitmodules").write_text("")
    proj = root / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "pyproject.toml").write_text(_PYPROJECT)
    (proj / "tests" / "test_a.py").write_text(_TESTS)
    monkeypatch.setattr(pytest_stats, "workspace_root", lambda: root)
    monkeypatch.setattr(pytest_stats, "stats_folder", lambda: root / "stats")
    # These tests run inside the local Jenkins too, where `JENKINS_URL` is set and the plugin records nothing.
    monkeypatch.delenv("JENKINS_URL", raising=False)
    monkeypatch.chdir(proj)
    return proj


def _run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_inprocess(
        "-p", "no:aisland_test_stats", "-p", "no:cacheprovider", *args, plugins=[pytest_stats]
    )


def _records(stats_root: Path) -> List[Dict]:
    files = sorted((stats_root / "proj").glob("*.jsonl"))
    return [json.loads(line) for path in files for line in path.read_text().splitlines()]


def _runs(stats_root: Path) -> List[Dict]:
    return [r for r in _records(stats_root) if r["kind"] == "run"]


def _tests(stats_root: Path) -> Dict[str, Dict]:
    return {r["test"]: r for r in _records(stats_root) if r["kind"] == "test"}


# --------------------------------------------------------------------------
# The plugin: what a run records
# --------------------------------------------------------------------------


def test_a_run_records_each_test_with_its_outcome_and_seconds_and_the_run_itself(
    pytester: pytest.Pytester, project: Path
) -> None:
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.TESTS_FAILED

    (run,) = _runs(pytester.path / "stats")
    assert run["leg"] == "fast" and run["whole"] is True and run["exit"] == 1
    assert run["tests"] == 6 and run["seconds"] > 0
    assert run["commit"] == ""  # the fake workspace is no git repository

    tests = _tests(pytester.path / "stats")
    outcomes = {name: record["outcome"] for name, record in tests.items()}
    assert outcomes == {
        "tests/test_a.py::test_passes": "passed",
        "tests/test_a.py::test_fails": "failed",
        "tests/test_a.py::test_skipped": "skipped",
        "tests/test_a.py::test_errors": "error",
        "tests/test_a.py::TestGroup::test_param[1]": "passed",
        "tests/test_a.py::TestGroup::test_param[2]": "passed",
    }
    assert all(record["run"] == run["run"] and record["seconds"] >= 0 for record in tests.values())
    assert not any(record["slow"] for record in tests.values())


def test_the_slow_run_is_recorded_as_the_slow_leg_with_its_tests_marked_slow(
    pytester: pytest.Pytester, project: Path
) -> None:
    _run(pytester, "-m", "slow")
    (run,) = _runs(pytester.path / "stats")
    assert run["leg"] == "slow"
    assert _tests(pytester.path / "stats")["tests/test_a.py::test_slow_one"]["slow"] is True


def test_both_runs_together_are_the_all_leg(pytester: pytest.Pytester, project: Path) -> None:
    _run(pytester, "-m", "")
    (run,) = _runs(pytester.path / "stats")
    assert run["leg"] == "all" and run["tests"] == 7


def test_plain_pytest_is_the_fast_run_of_a_project_that_marks_nothing_slow(
    pytester: pytest.Pytester, project: Path
) -> None:
    (project / "pyproject.toml").write_text(_PYPROJECT_WITHOUT_SLOW)
    _run(pytester)
    (run,) = _runs(pytester.path / "stats")
    assert run["leg"] == "fast" and run["tests"] == 7


@pytest.mark.parametrize(
    "args",
    [
        ("tests/test_a.py::test_passes",),
        ("tests/test_a.py",),
        ("-k", "passes"),
        ("-x",),
        ("--deselect", "tests/test_a.py::test_fails"),
    ],
    ids=["a test", "a file", "-k", "-x", "--deselect"],
)
def test_a_partial_run_is_recorded_but_not_as_a_whole_run(
    pytester: pytest.Pytester, project: Path, args: Tuple[str, ...]
) -> None:
    _run(pytester, *args)
    (run,) = _runs(pytester.path / "stats")
    assert run["whole"] is False


def test_a_collect_only_run_records_nothing(pytester: pytest.Pytester, project: Path) -> None:
    _run(pytester, "--collect-only")
    assert not (pytester.path / "stats").exists()


def test_a_run_outside_the_workspace_records_nothing(
    pytester: pytest.Pytester, project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A project folder that is not a direct child of the workspace root — a test's own tmp project — is no run of
    a workspace project."""
    monkeypatch.setattr(pytest_stats, "workspace_root", lambda: tmp_path / "elsewhere")
    _run(pytester)
    assert not (pytester.path / "stats").exists()


def test_a_jenkins_build_is_left_to_jenkins_own_record(
    pytester: pytest.Pytester, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local Jenkins builds IN the project folder (`customWorkspace`) with the workspace `.venv`, so the plugin
    loads there too; its JUnit record is what the report reads for CI, and recording the run again would count it twice.
    """
    monkeypatch.setenv("JENKINS_URL", "http://localhost:9080/")
    _run(pytester)
    assert not (pytester.path / "stats").exists()


def test_a_project_folder_of_a_workspace_without_gitmodules_is_recorded(
    pytester: pytest.Pytester, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace without `.gitmodules` reads its project folders as submodules (`projects.workspace_folders`); its
    `workspace.toml` makes it an aisland workspace, from a wheel too."""
    (pytester.path / ".gitmodules").unlink()
    (pytester.path / "workspace.toml").write_text("")
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    _run(pytester)
    assert (pytester.path / "stats" / "proj" / f"{date.today()}.jsonl").stat().st_size > 0


def test_a_repository_of_its_own_gets_nothing_written_beside_it(
    pytester: pytest.Pytester, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """aisland from a wheel, `pytest` in `~/code/app`: `~/code` holds neither `workspace.toml` nor `.gitmodules`, so it
    is no aisland workspace and nothing is written into it."""
    (pytester.path / ".gitmodules").unlink()
    monkeypatch.setattr(locations, "FROM_CHECKOUT", False)
    monkeypatch.delenv(locations.WORKSPACE_ENV, raising=False)
    _run(pytester)
    assert not (pytester.path / "stats").exists()


def test_a_run_writes_todays_file_and_removes_files_past_the_retention(
    pytester: pytest.Pytester, project: Path
) -> None:
    folder = pytester.path / "stats" / "proj"
    folder.mkdir(parents=True)
    old = folder / f"{date.today() - timedelta(days=pytest_stats.RETENTION_DAYS + 1)}.jsonl"
    kept = folder / f"{date.today() - timedelta(days=pytest_stats.RETENTION_DAYS - 1)}.jsonl"
    old.write_text("")
    kept.write_text("")
    _run(pytester)
    assert not old.exists() and kept.exists()
    assert (folder / f"{date.today()}.jsonl").stat().st_size > 0


def test_a_stats_folder_that_cannot_be_written_costs_one_line_never_the_runs_result(
    pytester: pytest.Pytester, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = pytester.path / "stats-is-a-file"
    blocker.write_text("")
    monkeypatch.setattr(pytest_stats, "stats_folder", lambda: blocker)
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["aisland test stats: this run was not recorded (*"])


def test_a_plugin_that_breaks_while_starting_costs_one_line_never_the_runs_result(
    pytester: pytest.Pytester, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plugin loads in every pytest run of the environment: an error of its own, whatever it is, is no error of
    the project's tests."""

    def broken(root: Path, folder: str) -> bool:
        raise RuntimeError("the switch could not be read")

    monkeypatch.setattr(pytest_stats, "records", broken)
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(passed=3, failed=1, errors=1, skipped=1, deselected=1)
    said = result.stderr.str() + capsys.readouterr().err
    assert "aisland test stats: this run was not recorded (RuntimeError: the switch could not be read)" in said
    assert not (pytester.path / "stats").exists()


def test_a_plugin_that_breaks_while_recording_costs_one_line_never_the_runs_result(
    pytester: pytest.Pytester, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(folder: Path) -> str:
        raise RuntimeError("no commit")

    monkeypatch.setattr(pytest_stats, "_commit", broken)
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes(passed=3, failed=1, errors=1, skipped=1, deselected=1)
    result.stdout.fnmatch_lines(["aisland test stats: this run was not recorded (RuntimeError: no commit)"])


def test_the_junit_address_is_the_one_pytests_own_junit_report_writes(pytester: pytest.Pytester, project: Path) -> None:
    """CI's records are JUnit files; a local nodeid must land on the same test as CI's classname and name."""
    _run(pytester, "-m", "", "--junitxml=report.xml")
    written = {
        f"{case.get('classname')}::{case.get('name')}" for case in ET.parse(project / "report.xml").iter("testcase")
    }
    assert {stats_report.junit_address(nodeid) for nodeid in _tests(pytester.path / "stats")} == written


def test_the_plugin_is_declared_as_a_pytest11_entry_point() -> None:
    """Every pytest run in the workspace `.venv` loads it; after adding it, `uv pip install -e ./aisland --no-deps`."""
    found = {ep.name: ep.value for ep in importlib.metadata.entry_points(group="pytest11")}
    assert found.get("aisland_test_stats") == "aisland.pytest_stats"


@needs_workspace
@pytest.mark.real_workspace
def test_the_records_live_in_aislands_data_folder() -> None:
    """The module resolves the folder itself (no `aisland.setup.manager` import in every pytest start); it must agree."""
    assert pytest_stats.stats_folder() == paths.data_dir() / "test-stats"
    assert pytest_stats.workspace_root() == paths.data_dir().parent.parent
