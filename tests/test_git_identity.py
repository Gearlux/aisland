"""A test that commits does not depend on the machine's git identity.

GitHub's runners have no `user.name`/`user.email`, so every test that makes git commit (`aisland wheels build
--commit`, the `--extra` commit) failed there with "Author identity unknown" while passing on a developer's machine.
`conftest.git_identity` gives every test one identity through git's own environment variables.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def test_a_test_commits_on_a_machine_without_a_git_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "file").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "file"], check=True)
    done = subprocess.run(["git", "-C", str(repo), "commit", "-qm", "x"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
