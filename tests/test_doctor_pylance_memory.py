"""`aisland doctor`'s `pylance-memory` check: VS Code's Python language server keeps running out of memory.

The check reads the language-server log of the newest VS Code window opened on the workspace. The logs here are
written the way Pylance writes them: a `Starting service instance … for workspace "<root>"` line, the V8 heap's
`FATAL ERROR: … JavaScript heap out of memory` lines (several per crash, one per worker thread), and
`Client: Running with node: <path>` once Pylance runs on a Node.js of its own (`python.analysis.nodeExecutable`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

import pytest

from aisland import doctor, locations

_START = '2026-10-30 10:25:32.361 [info] (16583): Starting service instance "acme" for workspace "{root}"'
_CRASH = "{stamp} [error] FATAL ERROR: MarkCompactCollector: young object promotion failed Allocation failed - JavaScript heap out of memory"  # noqa: E501
_OWN_NODE = "{stamp} [info] Client: Running with node: /home/me/.vscode/extensions/ms-python.vscode-pylance-2026.4.1/dist/.cache/node/node"  # noqa: E501


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "acme"
    root.mkdir()
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    return root


@pytest.fixture
def logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "Code" / "logs"
    monkeypatch.setattr(doctor, "_vscode_logs_root", lambda: folder)
    return folder


def _log(logs: Path, session: str, window: str, lines: List[str], mtime: float = 1_000_000.0) -> Path:
    path = logs / session / window / "exthost" / "ms-python.python" / "Python Language Server.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (mtime, mtime))
    return path


def _crash(stamp: str) -> List[str]:
    """One crash as Pylance logs it: the same line from several worker threads."""
    return [_CRASH.format(stamp=stamp)] * 3


def test_a_machine_without_vs_code_logs_passes(workspace: Path, logs: Path) -> None:
    result = doctor.check_pylance_memory()
    assert result.ok
    assert result.name == "pylance-memory"
    assert "no VS Code window of this workspace" in result.detail


def test_a_log_of_another_workspace_is_not_read_as_this_ones(workspace: Path, logs: Path) -> None:
    _log(
        logs, "20261030T090000", "window1", [_START.format(root="/elsewhere/other"), *_crash("2026-10-30 10:25:31.427")]
    )
    result = doctor.check_pylance_memory()
    assert result.ok
    assert "no VS Code window of this workspace" in result.detail


def test_a_log_without_a_crash_passes_naming_it(workspace: Path, logs: Path) -> None:
    log = _log(logs, "20261030T090000", "window1", [_START.format(root=workspace)])
    result = doctor.check_pylance_memory()
    assert result.ok
    assert result.detail == f"Pylance has not run out of memory ({log})"


def test_crashes_on_vs_codes_own_node_fail_with_the_user_setting_to_add(workspace: Path, logs: Path) -> None:
    log = _log(
        logs,
        "20261030T090000",
        "window9",
        [_START.format(root=workspace), *_crash("2026-10-30 10:25:31.427"), *_crash("2026-10-30 12:06:49.961")],
    )
    result = doctor.check_pylance_memory()
    assert not result.ok
    assert result.detail == (
        f"Pylance ran out of memory 2 times, the last at 2026-10-30 12:06:49 ({log}) — no completion or symbol "
        "search while it restarts"
    )
    assert '"python.analysis.nodeExecutable": "auto"' in result.remedy
    assert "User Settings" in result.remedy
    assert "Developer: Reload Window" in result.remedy


def test_crashes_before_pylance_ran_on_its_own_node_pass(workspace: Path, logs: Path) -> None:
    _log(
        logs,
        "20261030T090000",
        "window9",
        [
            _START.format(root=workspace),
            *_crash("2026-10-30 12:06:49.961"),
            _OWN_NODE.format(stamp="2026-10-30 12:15:51.691"),
        ],
    )
    result = doctor.check_pylance_memory()
    assert result.ok
    assert result.detail == (
        "Pylance ran out of memory at 2026-10-30 12:06:49 and runs on its own Node.js since 2026-10-30 12:15:51"
    )


def test_a_crash_on_its_own_node_fails_with_a_larger_heap(workspace: Path, logs: Path) -> None:
    _log(
        logs,
        "20261030T090000",
        "window9",
        [
            _START.format(root=workspace),
            _OWN_NODE.format(stamp="2026-10-30 12:15:51.691"),
            *_crash("2026-10-31 09:00:00.000"),
        ],
    )
    result = doctor.check_pylance_memory()
    assert not result.ok
    assert "on its own Node.js too" in result.detail
    assert '"python.analysis.nodeArguments": ["--max-old-space-size=16384"]' in result.remedy


def test_the_newest_window_of_the_workspace_is_the_one_judged(workspace: Path, logs: Path) -> None:
    _log(logs, "20261023T161012", "window4", [_START.format(root=workspace), *_crash("2026-10-23 22:05:53.714")], 1.0)
    newest = _log(logs, "20261030T090000", "window2", [_START.format(root=workspace)], 2.0)
    result = doctor.check_pylance_memory()
    assert result.ok
    assert str(newest) in result.detail


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a file the user cannot read")
def test_a_log_that_cannot_be_read_is_passed_over(workspace: Path, logs: Path) -> None:
    unreadable = _log(logs, "20261030T090000", "window2", [_START.format(root=workspace)], 2.0)
    unreadable.chmod(0)
    try:
        older = _log(logs, "20261030T090000", "window1", [_START.format(root=workspace)], 1.0)
        result = doctor.check_pylance_memory()
    finally:
        unreadable.chmod(0o644)
    assert result.ok
    assert str(older) in result.detail


def test_vs_codes_logs_folder_on_each_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert doctor._vscode_logs_root() == tmp_path / "Library" / "Application Support" / "Code" / "logs"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert doctor._vscode_logs_root() == tmp_path / ".config" / "Code" / "logs"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert doctor._vscode_logs_root() == tmp_path / "xdg" / "Code" / "logs"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert doctor._vscode_logs_root() == tmp_path / "Roaming" / "Code" / "logs"
    monkeypatch.delenv("APPDATA")
    assert doctor._vscode_logs_root() == tmp_path / "AppData" / "Roaming" / "Code" / "logs"


def test_the_check_is_registered() -> None:
    assert doctor.check_pylance_memory in doctor.CHECKS
