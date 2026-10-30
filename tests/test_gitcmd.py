"""``gitcmd.git``: output captured as text, ``network`` and ``check`` behave as documented."""

import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

from aisland import gitcmd
from aisland.errors import AislandError


def test_git_captures_text_output_in_the_repository(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    done = gitcmd.git(tmp_path, "rev-parse", "--is-inside-work-tree")
    assert (done.returncode, done.stdout.strip()) == (0, "true")


def test_a_failing_command_returns_its_code_unless_checked(tmp_path: Path) -> None:
    assert gitcmd.git(tmp_path, "rev-parse", "--is-inside-work-tree").returncode != 0
    with pytest.raises(AislandError, match="git rev-parse --is-inside-work-tree failed"):
        gitcmd.git(tmp_path, "rev-parse", "--is-inside-work-tree", check=True)


def test_network_never_prompts_and_has_a_default_time_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Dict[str, Any] = {}

    def fake_run(cmd: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    gitcmd.git(tmp_path, "fetch", network=True)
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["timeout"] == gitcmd.NETWORK_TIMEOUT
    gitcmd.git(tmp_path, "fetch", network=True, timeout=5)
    assert seen["timeout"] == 5
    gitcmd.git(tmp_path, "status")
    assert (seen["env"], seen["timeout"]) == (None, None)
