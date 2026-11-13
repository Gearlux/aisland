"""Every pytest run of a workspace project, recorded.

The workspace's two test runs: a project's fast run (plain ``pytest``) finishes within five
minutes and holds the unit tests plus only the integration tests that break; every other test carries ``slow``. Which
tests are slow and which ones break is read from statistics, never guessed. CI keeps each build's per-test results
(Jenkins' JUnit record), but code reaches CI only after it passes locally — 11 of the 351 Jenkins builds recorded by
2026-10-05 had a failing test — so the breaks that decide the split happen in local runs. Hence this pytest plugin
(entry point ``pytest11`` named ``aisland_test_stats``, so every run in the workspace ``.venv`` loads it), which appends
each run of a workspace project to ``aisland/data/test-stats/<project>/<date>.jsonl``: one line for the run (leg, whole
or partial, wall seconds, exit status, commit) and one per test (seconds, outcome, ``slow`` mark). The reader behind
``aisland tests report <project>`` is :mod:`aisland.ci.stats_report`, which this module never imports.

The plugin must never cost a run its result: it records only a run whose rootdir is ``<workspace>/<project>`` of an
aisland workspace (one holding ``workspace.toml`` or ``.gitmodules``, :func:`aisland.locations.is_workspace`), imports
only the standard library (it loads in every pytest start, so no ``aisland.paths`` with the rest of aisland behind it), and a
record it cannot write is one printed line. ``-p no:aisland_test_stats`` disables it for one run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Literal, Optional

from aisland import locations

if TYPE_CHECKING:
    import pytest

# Resolved exactly as `aisland.paths.data_dir` is, through `aisland.locations` (standard library only; pinned by
# `tests/test_pytest_stats.py::test_the_records_live_in_aislands_data_folder`), and asked each time: the MCP server
# outlives a change of workspace, and a test points the workspace elsewhere. The report reads the records from here.


def workspace_root() -> Path:
    """The workspace root."""
    return locations.workspace_root()


def stats_folder() -> Path:
    """One folder per project, one JSON-lines file per day."""
    return locations.state_dir() / "data" / "test-stats"


#: `aisland workspace test-stats on|off` (aisland/workspace/recording.py writes it): a project's `test-stats = false` and the
#: workspace's `[workspace] test-stats = false` in `workspace.toml`; this machine's `aisland.test-stats.<project>.record`
#: and `aisland.test-stats.default` in `.git/config`.
RECORD_KEY = "test-stats"
MACHINE_PREFIX = "aisland.test-stats."
MACHINE_DEFAULT_KEY = MACHINE_PREFIX + "default"
#: Who decided whether a project's runs are recorded.
RecordedBy = Literal["default", "workspace.toml", "this machine"]

#: Day files older than this are removed when a run is recorded: a full fast run of a large project is a few hundred
#: kilobytes, and the rule reads the last weeks, not the last year.
RETENTION_DAYS = 90

#: Which of a project's two runs this was: `fast` (plain `pytest`, i.e. `-m 'not slow'`, or plain `pytest` in a project
#: that marks nothing slow), `slow` (`-m slow`), `all` (`-m ''` where `slow` is registered), `other` (any other `-m`).
Leg = Literal["fast", "slow", "all", "other"]
#: A test's result over its setup, call and teardown: the worst phase wins.
Outcome = Literal["passed", "skipped", "failed", "error"]
_SEVERITY: Dict[str, int] = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


# --------------------------------------------------------------------------
# The plugin
# --------------------------------------------------------------------------


#: The one line a run gets when the plugin could not record it.
_NOT_RECORDED = "aisland test stats: this run was not recorded ({})"


def pytest_configure(config: "pytest.Config") -> None:
    """Start recording when this run is a workspace project's own: pytest's rootdir is `<workspace>/<project>` and
    the workspace is an aisland one (`locations.is_workspace`) — a repository of its own gets nothing written beside
    it.

    Any error of the plugin's own is one line on stderr, never the run's: the plugin loads in every pytest run of
    the environment, whatever the project (the reason for the broad ``except``).
    """
    try:
        _start_recording(config)
    except Exception as error:  # any: never the run's error (see the docstring)
        sys.stderr.write(_NOT_RECORDED.format(f"{type(error).__name__}: {error}") + "\n")


def _start_recording(config: "pytest.Config") -> None:
    if hasattr(config, "workerinput"):  # an xdist worker: its controller records the run
        return
    # A Jenkins build: the local Jenkins builds IN the project folder (`customWorkspace`) with the workspace `.venv`,
    # and keeps its own JUnit record, which `build_report` reads; recording the run here would count it twice.
    if os.environ.get("JENKINS_URL"):
        return
    if not locations.is_workspace(workspace_root()) or not _is_workspace_project(Path(config.rootpath)):
        return
    if not records(workspace_root(), Path(config.rootpath).name):
        return
    config.pluginmanager.register(_Recorder(config, stats_folder()), "aisland_test_stats_recorder")


def _is_workspace_project(folder: Path) -> bool:
    """A direct child of the workspace root holding a `pyproject.toml` — a submodule, or a project folder of a
    workspace without `.gitmodules`.

    A test's own tmp project (`pytester`, a subprocess in `tmp_path`) is not one: its inner pytest run is no run of
    the project.
    """
    root = workspace_root().resolve()
    return folder.resolve().parent == root and (folder / "pyproject.toml").is_file()


def machine_key(project: str) -> str:
    """This machine's switch for ``project`` in ``.git/config``."""
    return f"{MACHINE_PREFIX}{project}.record"


def _machine_switches(root: Path) -> Dict[str, bool]:
    """This machine's ``aisland.test-stats.*`` settings in ``<root>/.git/config``; none when git cannot say."""
    try:
        run = subprocess.run(
            ["git", "config", "--local", "--type=bool", "--get-regexp", r"^aisland\.test-stats\."],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    switches = {}
    for line in run.stdout.splitlines() if run.returncode == 0 else []:
        key, _, value = line.partition(" ")
        switches[key] = value.strip() == "true"
    return switches


@dataclass(frozen=True)
class Recording:
    """Whether a project's runs are recorded (``on``), who decided it, and the project's name in ``workspace.toml``."""

    on: bool
    by: RecordedBy
    project: str


def recording(root: Path, folder: str) -> Recording:
    """Whether runs of the project checked out in ``<root>/<folder>`` are recorded, and who decided it.

    This machine's switch for the project, else this machine's for every project, else the project's ``test-stats`` in
    ``workspace.toml``, else the workspace's, else yes. Never raises: a file it cannot read leaves the default — the
    plugin loads in every pytest run and must never cost one.
    """
    try:
        data = tomllib.loads((root / "workspace.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        data = {}
    name, own = folder, None
    projects = data.get("project")
    for project, table in projects.items() if isinstance(projects, dict) else []:
        if isinstance(table, dict) and table.get("path", project) == folder:
            name, own = project, table.get(RECORD_KEY)
            break
    machine = _machine_switches(root)
    for key in (machine_key(name), MACHINE_DEFAULT_KEY):
        if key in machine:
            return Recording(machine[key], "this machine", name)
    settings = data.get("workspace")
    shared = settings.get(RECORD_KEY) if isinstance(settings, dict) else None
    for value in (own, shared):
        if isinstance(value, bool):
            return Recording(value, "workspace.toml", name)
    return Recording(True, "default", name)


def records(root: Path, folder: str) -> bool:
    """Whether runs of the project checked out in ``<root>/<folder>`` are recorded (:func:`recording`)."""
    return recording(root, folder).on


def run_leg(markexpr: str, slow_registered: bool) -> Leg:
    """Which run a `-m` expression makes of a project that does (or does not) register the `slow` marker."""
    expression = " ".join(markexpr.split())
    if expression == "not slow":
        return "fast"
    if expression == "slow":
        return "slow"
    if not expression:
        return "all" if slow_registered else "fast"
    return "other"


def _is_whole_run(config: "pytest.Config") -> bool:
    """True when the run is the project's whole leg — only then does its wall time measure the fast run.

    Partial: a file or a test named, `-k`, `-x`/`--maxfail`, `--lf`, `--sw`, `--deselect`.
    """
    option = config.option
    if option.keyword or option.maxfail or getattr(option, "lf", False) or getattr(option, "stepwise", False):
        return False
    if getattr(option, "deselect", None):
        return False
    here = Path(config.invocation_params.dir)
    return all("::" not in arg and (here / arg).is_dir() for arg in config.args)


def _phase_outcome(report: "pytest.TestReport") -> Outcome:
    if report.passed:
        return "passed"
    if report.skipped:  # a skip, or an expected failure
        return "skipped"
    return "failed" if report.when == "call" else "error"


@dataclass
class _Result:
    slow: bool
    seconds: float = 0.0
    outcome: Outcome = "passed"


class _Recorder:
    """One run's record, held while it runs and written once when the session ends."""

    def __init__(self, config: "pytest.Config", stats_dir: Path) -> None:
        self._config = config
        self._folder = stats_dir / Path(config.rootpath).name
        self._project_dir = Path(config.rootpath)
        markers = config.getini("markers")
        slow_registered = any(str(line).split(":", 1)[0].strip() == "slow" for line in markers)
        self._leg = run_leg(config.option.markexpr or "", slow_registered)
        self._whole = _is_whole_run(config)
        self._started = time.monotonic()
        self._at = datetime.now().astimezone()
        self._tests: Dict[str, _Result] = {}
        #: Why this run is not recorded, once something of the plugin's own failed; the run itself goes on.
        self._failed: Optional[str] = None

    def pytest_runtest_logreport(self, report: "pytest.TestReport") -> None:
        try:
            self._note(report)
        except Exception as error:  # any: never the run's error (pytest_configure)
            self._failed = self._failed or f"{type(error).__name__}: {error}"

    def _note(self, report: "pytest.TestReport") -> None:
        result = self._tests.setdefault(report.nodeid, _Result(slow="slow" in report.keywords))
        result.seconds += report.duration
        outcome = _phase_outcome(report)
        if _SEVERITY[outcome] > _SEVERITY[result.outcome]:
            result.outcome = outcome

    def pytest_sessionfinish(self, session: "pytest.Session", exitstatus: int) -> None:
        try:
            if self._failed is None:
                self._write(exitstatus)
        except Exception as error:  # any: never the run's error (pytest_configure)
            self._failed = f"{type(error).__name__}: {error}"
        if self._failed is not None:
            self._say(self._failed)

    def _say(self, why: str) -> None:
        reporter = self._config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(_NOT_RECORDED.format(why))

    def _write(self, exitstatus: int) -> None:
        if not self._tests:  # `--collect-only`, or nothing selected: no run to record
            return
        run_id = f"{self._at:%Y%m%dT%H%M%S}-{os.getpid()}"
        lines: List[Dict[str, object]] = [
            {
                "kind": "run",
                "run": run_id,
                "at": self._at.isoformat(timespec="seconds"),
                "commit": _commit(self._project_dir),
                "leg": self._leg,
                "whole": self._whole,
                "seconds": round(time.monotonic() - self._started, 3),
                "exit": int(exitstatus),
                "tests": len(self._tests),
            }
        ]
        lines += [
            {
                "kind": "test",
                "run": run_id,
                "test": nodeid,
                "seconds": round(result.seconds, 3),
                "outcome": result.outcome,
                "slow": result.slow,
            }
            for nodeid, result in self._tests.items()
        ]
        try:
            _append(self._folder, self._at.date(), lines)
        except OSError as error:
            self._failed = str(error)


def _commit(folder: Path) -> str:
    """The project's HEAD, short; empty outside a git checkout."""
    try:
        done = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _append(folder: Path, day: date, lines: List[Dict[str, object]]) -> None:
    """Append the run to the day's file in ONE write (sessions run tests side by side), then drop expired days."""
    folder.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(line) + "\n" for line in lines).encode("utf-8")
    descriptor = os.open(folder / f"{day.isoformat()}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    cutoff = day - timedelta(days=RETENTION_DAYS)
    for path in folder.glob("*.jsonl"):
        stamp = day_of(path)
        if stamp is not None and stamp < cutoff:
            path.unlink(missing_ok=True)


def day_of(path: Path) -> Optional[date]:
    """The day a day file (`<date>.jsonl`) holds; ``None`` for a file of another name."""
    try:
        return date.fromisoformat(path.stem)
    except ValueError:
        return None
