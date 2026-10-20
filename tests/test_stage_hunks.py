"""The harness helper ``stage_hunks.py`` stages some of a file's hunks exactly where they were written.

Each test builds a throwaway git repository, so nothing of the workspace is touched.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "aisland" / "harness" / "scripts" / "stage_hunks.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _stage(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=repo, capture_output=True, text=True)


def _lines(n: int) -> List[str]:
    return [f"line {k}\n" for k in range(1, n + 1)]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repository whose ``notes.txt`` holds lines 1-30 at HEAD."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    (tmp_path / "notes.txt").write_text("".join(_lines(30)))
    _git(tmp_path, "add", "notes.txt")
    _git(tmp_path, "commit", "-q", "-m", "start")
    return tmp_path


def _two_sessions(repo: Path) -> List[str]:
    """Another session inserts 15 lines after line 5; this session inserts one line after line 20."""
    lines = _lines(30)
    theirs = [f"theirs {k}\n" for k in range(15)]
    worktree = lines[:5] + theirs + lines[5:20] + ["mine\n"] + lines[20:]
    (repo / "notes.txt").write_text("".join(worktree))
    return lines[:20] + ["mine\n"] + lines[20:]


def test_an_insertion_after_a_left_out_hunk_lands_where_it_was_written(repo: Path) -> None:
    expected = _two_sessions(repo)
    done = _stage(repo, "take", "notes.txt", "1")
    assert done.returncode == 0, done.stderr
    assert _git(repo, "show", ":notes.txt") == "".join(expected)
    # what stays unstaged is exactly the other session's 15 lines
    left = [line for line in _git(repo, "diff", "-U0", "--", "notes.txt").splitlines() if line[:1] in "+-"]
    assert left[2:] == [f"+theirs {k}" for k in range(15)]


def test_git_apply_unidiff_zero_puts_that_insertion_too_far_down(repo: Path, tmp_path: Path) -> None:
    """Why the script exists: the same choice through ``git apply --cached --unidiff-zero`` misplaces the line by the
    15 the left-out hunk added (measured twice on 2026-10-05 in real commits)."""
    expected = _two_sessions(repo)
    diff = _git(repo, "diff", "-U0", "--", "notes.txt").splitlines(keepends=True)
    hunk_starts = [k for k, line in enumerate(diff) if line.startswith("@@")]
    patch = "".join(diff[: hunk_starts[0]] + diff[hunk_starts[1] :])
    (tmp_path / "mine.patch").write_text(patch)
    _git(repo, "apply", "--cached", "--unidiff-zero", str(tmp_path / "mine.patch"))
    staged = _git(repo, "show", ":notes.txt")
    assert staged != "".join(expected)
    # written after line 20; placed 15 lines further down, which here is past the end: it lands last, as the
    # satellite README's rows did
    assert staged.splitlines().index("mine") == 30


def test_another_sessions_staged_hunks_stay_staged(repo: Path) -> None:
    """The base is the index, not HEAD: hunks someone else already staged are kept."""
    lines = _lines(30)
    staged_by_them = lines[:2] + ["staged by them\n"] + lines[2:]
    (repo / "notes.txt").write_text("".join(staged_by_them))
    _git(repo, "add", "notes.txt")
    worktree = staged_by_them[:25] + ["mine\n"] + staged_by_them[25:]
    (repo / "notes.txt").write_text("".join(worktree))
    assert _stage(repo, "take", "notes.txt", "0").returncode == 0
    assert _git(repo, "show", ":notes.txt") == "".join(worktree)


def test_replacements_and_deletions_are_staged_too(repo: Path) -> None:
    lines = _lines(30)
    worktree = lines[:3] + ["three, rewritten\n"] + lines[4:10] + lines[12:28] + ["their tail\n"] + lines[28:]
    (repo / "notes.txt").write_text("".join(worktree))
    listing = _stage(repo, "list", "notes.txt").stdout
    assert listing.splitlines()[:3] == [
        "0  -4 +4  | -line 4",
        "1  -11,2 +10,0  | -line 11",
        "2  -28,0 +27  | +their tail",
    ]
    assert _stage(repo, "take", "notes.txt", "0,1").returncode == 0
    assert _git(repo, "show", ":notes.txt") == "".join(lines[:3] + ["three, rewritten\n"] + lines[4:10] + lines[12:])


def test_a_last_line_without_a_newline_keeps_it_so(repo: Path) -> None:
    (repo / "notes.txt").write_text("".join(_lines(30)) + "an end without a newline")
    assert _stage(repo, "take", "notes.txt", "0").returncode == 0
    assert _git(repo, "show", ":notes.txt") == "".join(_lines(30)) + "an end without a newline"


def test_the_file_mode_is_kept(repo: Path) -> None:
    os.chmod(repo / "notes.txt", 0o755)
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-q", "-m", "executable")
    _two_sessions(repo)
    assert _stage(repo, "take", "notes.txt", "1").returncode == 0
    assert _git(repo, "ls-files", "-s", "notes.txt").startswith("100755 ")


@pytest.mark.parametrize(
    "args, message",
    [
        (["take", "notes.txt", "7"], "notes.txt has 2 hunks (0-1); there is no hunk 7"),
        (["take", "new.txt", "0"], "new.txt is not in the index — a new file is staged whole: git add -- new.txt"),
        (["take", "notes.txt", ""], "name the hunks to stage, e.g. 0,2"),
    ],
)
def test_a_wrong_request_is_refused_naming_what_works(repo: Path, args: List[str], message: str) -> None:
    _two_sessions(repo)
    (repo / "new.txt").write_text("new\n")
    done = _stage(repo, *args)
    assert done.returncode != 0 and message in done.stderr
    assert _git(repo, "diff", "--cached", "--name-only") == ""
