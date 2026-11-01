"""The workspace's test-run statistics: the pytest plugin that records every run, and the report that reads them.

Root AGENTS.md "Two Test Runs" (user ruling 2026-10-05). The plugin is driven in-process through `pytester`, inside a
fake workspace (a `.gitmodules` beside a project folder), with the module's workspace root and stats folder pointed at
tmp paths — no test writes to the real `aisland/data/test-stats/`. The installed entry point is blocked in those inner
runs (`-p no:aisland_test_stats`) and the module handed in as a plugin object, so they do not depend on whether the
editable install has picked up the entry point yet; one test pins the entry point itself.
"""

import importlib.metadata
import json
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import pytest
from click.testing import CliRunner

from aisland import locations, pytest_stats
from aisland.cli import main
from aisland.paths import DATA_DIR
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
from aisland import projects


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
    monkeypatch.setattr(pytest_stats, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(pytest_stats, "STATS_DIR", root / "stats")
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
    monkeypatch.setattr(pytest_stats, "WORKSPACE_ROOT", tmp_path / "elsewhere")
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
    monkeypatch.setattr(pytest_stats, "STATS_DIR", blocker)
    result = _run(pytester)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["aisland test stats: this run was not recorded (*"])


def test_the_junit_address_is_the_one_pytests_own_junit_report_writes(pytester: pytest.Pytester, project: Path) -> None:
    """CI's records are JUnit files; a local nodeid must land on the same test as CI's classname and name."""
    _run(pytester, "-m", "", "--junitxml=report.xml")
    written = {
        f"{case.get('classname')}::{case.get('name')}" for case in ET.parse(project / "report.xml").iter("testcase")
    }
    assert {pytest_stats.junit_address(nodeid) for nodeid in _tests(pytester.path / "stats")} == written


def test_the_plugin_is_declared_as_a_pytest11_entry_point() -> None:
    """Every pytest run in the workspace `.venv` loads it; after adding it, `uv pip install -e ./aisland --no-deps`."""
    found = {ep.name: ep.value for ep in importlib.metadata.entry_points(group="pytest11")}
    assert found.get("aisland_test_stats") == "aisland.pytest_stats"


@needs_workspace
def test_the_records_live_in_aislands_data_folder() -> None:
    """The module resolves the folder itself (no `aisland.manager` import in every pytest start); it must agree."""
    assert pytest_stats.STATS_DIR == DATA_DIR / "test-stats"
    assert pytest_stats.JENKINS_JOBS_DIR == DATA_DIR / "jenkins" / "jobs"
    assert pytest_stats.WORKSPACE_ROOT == DATA_DIR.parent.parent


# --------------------------------------------------------------------------
# The report: synthetic records, one tmp folder per case
# --------------------------------------------------------------------------

_NOW = datetime(2026, 10, 5, 12, 0).astimezone()


def _local(
    stats: Path,
    when: datetime,
    seconds: float,
    tests: Sequence[Tuple[str, float, str, bool]],
    leg: str = "fast",
    whole: bool = True,
    exit_code: int = 0,
) -> None:
    """One recorded local run of `proj` and its tests (nodeid, seconds, outcome, slow)."""
    run_id = f"{when:%Y%m%dT%H%M%S}-{len(list(stats.rglob('*.jsonl')))}-{seconds}"
    lines = [
        {
            "kind": "run",
            "run": run_id,
            "at": when.isoformat(),
            "commit": "abc",
            "leg": leg,
            "whole": whole,
            "seconds": seconds,
            "exit": exit_code,
            "tests": len(tests),
        }
    ] + [
        {"kind": "test", "run": run_id, "test": t, "seconds": s, "outcome": o, "slow": slow} for t, s, o, slow in tests
    ]
    path = stats / "proj" / f"{when.date()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write("".join(json.dumps(line) + "\n" for line in lines))


def _jenkins(jobs: Path, number: int, when: datetime, cases: Sequence[Tuple[str, str, float, bool]]) -> None:
    """One Jenkins build of `proj.local` with its JUnit cases (className, testName, duration, failed)."""
    build = jobs / "proj.local" / "builds" / str(number)
    build.mkdir(parents=True)
    (build / "build.xml").write_text(f"<build><timestamp>{int(when.timestamp() * 1000)}</timestamp></build>")
    body = "".join(
        f"<case><duration>{d}</duration><className>{c}</className><testName>{n}</testName>"
        + ("<errorStackTrace>boom</errorStackTrace>" if failed else "")
        + "<skipped>false</skipped></case>"
        for c, n, d, failed in cases
    )
    (build / "junitResult.xml").write_text(f"<result><suites><suite><cases>{body}</cases></suite></suites></result>")


@pytest.fixture
def stores(tmp_path: Path) -> Tuple[Path, Path]:
    return tmp_path / "stats", tmp_path / "jobs"


def _report(stores: Tuple[Path, Path], days: int = 30) -> pytest_stats.StatsReport:
    return pytest_stats.build_report("proj", days=days, now=_NOW, stats_dir=stores[0], jenkins_jobs_dir=stores[1])


def test_the_fast_run_time_is_the_median_of_the_complete_whole_fast_runs(stores: Tuple[Path, Path]) -> None:
    stats, _ = stores
    _local(stats, _NOW - timedelta(days=3), 200.0, [])
    _local(stats, _NOW - timedelta(days=2), 400.0, [])
    _local(stats, _NOW - timedelta(days=1), 360.0, [], exit_code=1)  # tests failed: still a complete run
    _local(stats, _NOW - timedelta(hours=5), 10.0, [], whole=False)  # one file
    _local(stats, _NOW - timedelta(hours=4), 2000.0, [], leg="slow")
    _local(stats, _NOW - timedelta(hours=3), 50.0, [], exit_code=2)  # interrupted
    report = _report(stores)
    assert report.whole_fast_runs == 3
    assert report.fast_run_seconds == 360.0
    assert report.last_fast_run_seconds == 360.0
    assert report.local_runs == 6 and report.budget_seconds == 300.0


def test_over_budget_the_slowest_never_failed_fast_tests_are_proposed_for_the_slow_run(
    stores: Tuple[Path, Path],
) -> None:
    stats, _ = stores
    tests = [("t::a", 50.0, "passed", False), ("t::c", 30.0, "passed", False), ("t::d", 1.0, "passed", False)]
    _local(stats, _NOW - timedelta(days=1), 360.0, tests + [("t::b", 40.0, "failed", False)], exit_code=1)
    _local(stats, _NOW - timedelta(hours=1), 360.0, tests + [("t::b", 40.0, "passed", False)])
    report = _report(stores)
    # 60 s over: a (50 s) and c (30 s) cover it; b is slower than c but has caught a break, so it stays.
    assert [t.test for t in report.to_slow] == ["t::a", "t::c"]
    assert [t.test for t in report.slowest_fast[:4]] == ["t::a", "t::b", "t::c", "t::d"]
    (failed,) = report.failed
    assert (failed.test, failed.failures, failed.runs, failed.last_failure) == (
        "t::b",
        1,
        2,
        str(_NOW.date() - timedelta(days=1)),
    )


def test_a_parallel_run_needs_the_overrun_times_its_parallelism_in_test_time(stores: Tuple[Path, Path]) -> None:
    stats, _ = stores
    # 1440 s of tests ran in 360 s of wall time: four at once. 60 s over the budget is 240 s of test time, which
    # two tests of 144 s cover; one process would have called one enough.
    _local(stats, _NOW - timedelta(days=1), 360.0, [(f"t::{n}", 144.0, "passed", False) for n in "abcdefghij"])
    report = _report(stores)
    assert report.parallel == 4.0
    assert [t.test for t in report.to_slow] == ["t::a", "t::b"]
    assert report.uncovered_seconds == 0.0
    assert "the fast run runs about 4.0 tests at once: each second over is 4.0 s of tests" in (
        pytest_stats.render_report(report)
    )


def test_the_parallelism_is_the_latest_runs_not_the_windows(stores: Tuple[Path, Path]) -> None:
    stats, _ = stores
    # Ten runs in one process, then the project went parallel: five runs of four tests at once.
    for day in range(20, 10, -1):
        _local(stats, _NOW - timedelta(days=day), 400.0, [("t::a", 400.0, "passed", False)])
    for day in range(5, 0, -1):
        _local(stats, _NOW - timedelta(days=day), 100.0, [("t::a", 400.0, "passed", False)])
    assert _report(stores).parallel == 4.0


def test_when_the_never_failed_tests_cannot_cover_the_overrun_the_report_says_how_much_is_left(
    stores: Tuple[Path, Path],
) -> None:
    stats, _ = stores
    _local(stats, _NOW - timedelta(days=1), 420.0, [("t::a", 60.0, "passed", False), ("t::b", 300.0, "failed", False)])
    report = _report(stores)
    # 120 s over in one process; a covers 60 s, b caught a break and stays.
    assert [t.test for t in report.to_slow] == ["t::a"]
    assert report.uncovered_seconds == 60.0
    assert "Even moving all of them leaves the fast run 1m 00s over" in pytest_stats.render_report(report)


def test_a_test_marked_slow_in_its_file_since_its_last_run_is_not_proposed_again(
    stores: Tuple[Path, Path], tmp_path: Path
) -> None:
    stats, jobs = stores
    folder = tmp_path / "proj"
    (folder / "tests").mkdir(parents=True)
    (folder / "tests" / "test_m.py").write_text(
        "import pytest\n\n\n@pytest.mark.slow\ndef test_marked():\n    pass\n\n\ndef test_plain():\n    pass\n\n\n"
        "@pytest.mark.slow\nclass TestSlowClass:\n    def test_in_it(self):\n        pass\n"
    )
    (folder / "tests" / "test_module.py").write_text(
        "import pytest\n\npytestmark = [pytest.mark.slow]\n\n\n" "def test_any():\n    pass\n"
    )
    names = [
        "tests/test_m.py::test_marked",
        "tests/test_m.py::test_plain",
        "tests/test_m.py::TestSlowClass::test_in_it",
        "tests/test_module.py::test_any",
    ]
    _local(stats, _NOW - timedelta(days=1), 3600.0, [(n, 100.0, "passed", False) for n in names])
    report = pytest_stats.build_report("proj", now=_NOW, stats_dir=stats, jenkins_jobs_dir=jobs, project_dir=folder)
    assert [t.test for t in report.to_slow] == ["tests/test_m.py::test_plain"]
    assert [t.test for t in report.slowest_fast] == ["tests/test_m.py::test_plain"]


def test_within_budget_nothing_is_proposed_for_the_slow_run(stores: Tuple[Path, Path]) -> None:
    stats, _ = stores
    _local(stats, _NOW - timedelta(days=1), 200.0, [("t::a", 150.0, "passed", False)])
    assert _report(stores).to_slow == []


def test_a_slow_test_that_failed_is_proposed_for_the_fast_run(stores: Tuple[Path, Path]) -> None:
    stats, _ = stores
    _local(stats, _NOW - timedelta(days=1), 900.0, [("t::ref", 80.0, "failed", True)], leg="slow", exit_code=1)
    _local(stats, _NOW - timedelta(days=1), 900.0, [("t::other", 80.0, "passed", True)], leg="slow")
    assert [t.test for t in _report(stores).to_fast] == ["t::ref"]


def test_a_jenkins_build_counts_beside_the_local_runs_of_the_same_test(stores: Tuple[Path, Path]) -> None:
    stats, jobs = stores
    nodeid = "tests/test_a.py::TestX::test_y[1-a]"
    _local(stats, _NOW - timedelta(days=1), 100.0, [(nodeid, 2.0, "passed", False)])
    _jenkins(jobs, 7, _NOW - timedelta(days=2), [("tests.test_a.TestX", "test_y[1-a]", 4.0, True)])
    report = _report(stores)
    assert report.ci_builds == 1
    (stat,) = report.failed
    assert (stat.test, stat.runs, stat.failures, stat.seconds) == (nodeid, 2, 1, 3.0)


def test_a_test_only_jenkins_saw_is_named_by_its_junit_address(stores: Tuple[Path, Path]) -> None:
    _jenkins(stores[1], 1, _NOW - timedelta(days=2), [("tests.test_b", "test_z", 1.0, True)])
    (stat,) = _report(stores).failed
    assert stat.test == "tests.test_b::test_z" and stat.slow is None


def test_records_older_than_the_window_are_left_out(stores: Tuple[Path, Path]) -> None:
    stats, jobs = stores
    _local(stats, _NOW - timedelta(days=40), 100.0, [("t::a", 1.0, "failed", False)], exit_code=1)
    _jenkins(jobs, 1, _NOW - timedelta(days=40), [("t", "a", 1.0, True)])
    assert _report(stores, days=30).failed == [] and _report(stores, days=30).ci_builds == 0
    assert len(_report(stores, days=60).failed) == 1 and _report(stores, days=60).ci_builds == 1


def test_a_damaged_line_is_skipped(stores: Tuple[Path, Path]) -> None:
    stats, _ = stores
    _local(stats, _NOW - timedelta(days=1), 100.0, [("t::a", 1.0, "passed", False)])
    with (stats / "proj" / f"{(_NOW - timedelta(days=1)).date()}.jsonl").open("a") as handle:
        handle.write('{"kind": "test", "run": \n')  # a run killed mid-write
    assert _report(stores).local_runs == 1


_PROJECT_TESTS = """
import pytest


class Base:
    def test_inherited(self):
        pass


class TestKept(Base):
    def test_method(self):
        pass

    class TestInner:
        def test_deep(self):
            pass


class TestFromElsewhere(pytest.Class):
    pass


@pytest.mark.parametrize("n", [1, 2])
def test_param(n):
    pass


test_assigned = lambda: None  # noqa: E731
"""


def _project_with_tests(tmp_path: Path) -> Path:
    folder = tmp_path / "proj"
    (folder / "tests").mkdir(parents=True)
    (folder / "tests" / "test_kept.py").write_text(_PROJECT_TESTS)
    (folder / "tests" / "test_broken.py").write_text("def test_x(:\n")
    return folder


def test_a_test_the_project_no_longer_has_is_neither_listed_nor_proposed_but_its_failures_stay(
    stores: Tuple[Path, Path], tmp_path: Path
) -> None:
    stats, jobs = stores
    gone = ["tests/test_deleted.py::test_a", "tests/test_kept.py::test_renamed", "tests/test_kept.py::TestGone::test_m"]
    kept = [
        "tests/test_kept.py::test_param[1]",
        "tests/test_kept.py::TestKept::test_method",
        "tests/test_kept.py::TestKept::test_inherited",
        "tests/test_kept.py::TestKept::TestInner::test_deep",
        "tests/test_kept.py::TestFromElsewhere::test_from_a_base_defined_elsewhere",
        "tests/test_kept.py::test_assigned",
        "tests/test_broken.py::test_x",
    ]
    tests = [(t, 100.0, "passed", False) for t in gone + kept]
    _local(stats, _NOW - timedelta(days=1), 3600.0, tests + [("tests/test_deleted.py::test_slow", 9.0, "failed", True)])
    report = pytest_stats.build_report(
        "proj", now=_NOW, stats_dir=stats, jenkins_jobs_dir=jobs, project_dir=_project_with_tests(tmp_path)
    )
    assert sorted(t.test for t in report.to_slow) == sorted(kept)
    assert sorted(t.test for t in report.slowest_fast) == sorted(kept)
    assert report.to_fast == []
    assert [t.test for t in report.failed] == ["tests/test_deleted.py::test_slow"]


def test_a_method_missing_from_a_class_and_its_bases_in_the_same_file_is_gone(tmp_path: Path) -> None:
    present = pytest_stats._Present(_project_with_tests(tmp_path))
    assert not present("tests/test_kept.py::TestKept::test_nowhere")
    assert not present("tests/test_kept.py::TestKept::TestMissing::test_deep")
    assert present("tests/test_kept.py::TestKept::test_inherited")


def test_what_the_report_cannot_read_counts_as_present(tmp_path: Path) -> None:
    present = pytest_stats._Present(_project_with_tests(tmp_path))
    assert present("tests.test_b::test_z")  # a JUnit address: no file to read
    assert present("tests/test_broken.py::test_gone")  # the file does not parse
    assert pytest_stats._Present(tmp_path / "elsewhere")("tests/test_deleted.py::test_a")  # no project folder here


def test_a_mark_the_file_does_not_show_is_left_to_the_last_run(tmp_path: Path) -> None:
    present = pytest_stats._Present(_project_with_tests(tmp_path))
    assert not present.marked_slow("tests.test_b::test_z")  # a JUnit address
    assert not present.marked_slow("tests/test_broken.py::test_x")  # the file does not parse
    assert not present.marked_slow("tests/test_deleted.py::test_a")  # no such file
    assert not present.marked_slow("tests/test_kept.py::TestMissing::test_deep")  # no such class
    assert not present.marked_slow("tests/test_kept.py::test_param[1]")  # no mark on it


def test_the_text_report_names_the_overrun_and_what_to_move(stores: Tuple[Path, Path]) -> None:
    stats, _ = stores
    _local(stats, _NOW - timedelta(days=1), 420.0, [("t::big", 200.0, "passed", False)])
    text = pytest_stats.render_report(_report(stores))
    assert "Fast run: 7m 00s (median of 1 whole run; last 7m 00s) - budget 5m 00s, 2m 00s over" in text
    assert "Move to the slow run (never failed, slowest first) to fit 5m 00s:" in text
    assert "t::big" in text


def test_the_text_report_without_records_says_how_to_make_one(stores: Tuple[Path, Path]) -> None:
    text = pytest_stats.render_report(_report(stores))
    assert "No recorded runs of proj in the last 30 days" in text


# --------------------------------------------------------------------------
# The CLI: `aisland tests report`
# --------------------------------------------------------------------------


@pytest.fixture
def cli_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / "pyproject.toml").write_text(_PYPROJECT)
    monkeypatch.setattr(pytest_stats, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(pytest_stats, "STATS_DIR", tmp_path / "stats")
    monkeypatch.setattr(pytest_stats, "JENKINS_JOBS_DIR", tmp_path / "jobs")
    return tmp_path


def test_the_json_report_is_the_reports_fields(cli_workspace: Path) -> None:
    _local(
        cli_workspace / "stats",
        datetime.now().astimezone() - timedelta(days=1),
        100.0,
        [("t::a", 1.0, "failed", False)],
        exit_code=1,
    )
    result = CliRunner().invoke(main, ["tests", "report", "proj", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["project"] == "proj" and data["fast_run_seconds"] == 100.0
    assert data["failed"][0]["test"] == "t::a"


def test_an_unknown_project_is_refused_by_name(cli_workspace: Path) -> None:
    result = CliRunner().invoke(main, ["tests", "report", "nosuch"])
    assert result.exit_code == 2
    assert f"no project 'nosuch' in the workspace {cli_workspace}" in result.output
