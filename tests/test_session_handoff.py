"""The harness helper ``session_handoff.py`` carries a session's commits and its own uncommitted edits from one
workspace to another, and refuses to move a repository it cannot move cleanly.

Each test builds throwaway repositories — an origin, a root with one submodule, and a second clone of both standing
for the other machine — so nothing of the workspace is touched.
"""

import importlib.util
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional, Tuple

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "aisland" / "harness" / "scripts" / "session_handoff.py"


def _script() -> ModuleType:
    """The script loaded in this process, so a test can change the ``tarfile`` it sees."""
    spec = importlib.util.spec_from_file_location("session_handoff", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _python_without_the_data_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tarfile`` as a Python before 3.12 has it (macOS's ``/usr/bin/python3`` is 3.9): no extraction filters, and
    an ``extractall`` that refuses a ``filter`` argument."""
    monkeypatch.delattr(tarfile, "data_filter")
    real = tarfile.TarFile.extractall

    def extractall(
        self: tarfile.TarFile, path: str = ".", members: Optional[List[tarfile.TarInfo]] = None, **kw: object
    ) -> None:
        if kw:
            raise TypeError(f"extractall() got an unexpected keyword argument {next(iter(kw))!r}")
        real(self, path, members, filter="fully_trusted")  # what an old Python does: no check at all

    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run(cwd: Path, *args: str, home: Path) -> object:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
            "CLAUDE_CODE_SESSION_ID": "abc-123",
        },
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture()
def machines(tmp_path: Path) -> Tuple[Path, Path, Path]:
    """``here`` and ``there``: two clones of a root whose submodule ``lib`` holds ``lib.txt``; and the HOME both use."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        '[user]\n\temail = test@example.com\n\tname = test\n[protocol "file"]\n\tallow = always\n'
    )
    origins = tmp_path / "origins"
    lib = origins / "lib"
    lib.mkdir(parents=True)
    _git(lib, "init", "-q", "-b", "dev/main")
    _identity(lib)
    _commit(lib, "lib.txt", "one\n", "start")
    root = origins / "root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "dev/main")
    _identity(root)
    _commit(root, "README.md", "root\n", "start")
    _git(root, "submodule", "add", "-q", str(lib), "lib")
    _git(root, "commit", "-q", "-m", "add lib")
    for name in ("lib", "root"):
        _git(origins / name, "config", "receive.denyCurrentBranch", "updateInstead")
    clones = []
    for name in ("here", "there"):
        clone = tmp_path / name
        _git(tmp_path, "clone", "-q", "--recurse-submodules", str(root), name)
        _git(clone / "lib", "switch", "-q", "dev/main")
        _identity(clone)
        _identity(clone / "lib")
        clones.append(clone)
    return clones[0], clones[1], home


def _rows(report: object) -> Dict[str, Dict]:
    assert isinstance(report, list)
    return {row["path"]: row for row in report}


def test_commits_on_no_remote_and_the_sessions_edits_reach_the_other_clone(machines: Tuple[Path, Path, Path]) -> None:
    here, there, home = machines
    head = _commit(here / "lib", "lib.txt", "two\n", "unpushed work")
    (here / "lib" / "lib.txt").write_text("two\nthree\n")
    (here / "lib" / "new.txt").write_text("a new file\n")
    (here / "README.md").write_text("another session's edit\n")
    out = here / ".handoff" / "h1"

    manifest = _run(here, "export", str(out), "--path", "lib/lib.txt", "--path", "lib/new.txt", home=home)

    assert isinstance(manifest, dict)
    lib = next(r for r in manifest["repos"] if r["path"] == "lib")
    root = next(r for r in manifest["repos"] if r["path"] == ".")
    assert lib["unpushed"] == 1 and lib["bundle"] == "repos/lib.bundle"
    assert lib["patch_files"] == ["lib.txt", "new.txt"]
    assert root["patch"] is None, "another session's README edit is never carried"
    assert "README.md" in root["changed"]
    (out / "HANDOFF.md").write_text("# brief\n")
    archive = Path(_run(here, "pack", str(out), home=home)["archive"])  # type: ignore[index]

    moved = there.parent / "inbox" / archive.name
    moved.parent.mkdir()
    archive.rename(moved)
    rows = _rows(_run(there, "check", str(moved), home=home))
    assert rows["lib"]["relation"] == "missing" and rows["lib"]["bundle"] == "fetchable"
    assert rows["lib"]["patch"].startswith("does not apply")  # its base commit is not here yet
    rows = _rows(_run(there, "apply", str(moved), home=home))

    assert rows["lib"]["done"] == ["fast-forwarded", "patch applied"]
    assert _git(there / "lib", "rev-parse", "HEAD") == head
    assert (there / "lib" / "lib.txt").read_text() == "two\nthree\n"
    assert (there / "lib" / "new.txt").read_text() == "a new file\n"
    assert (there / "README.md").read_text() == "root\n"


def test_a_patch_already_on_disk_is_said_so_and_not_applied_twice(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    (here / "lib" / "lib.txt").write_text("edited\n")
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), "--path", "lib/lib.txt", home=home)

    rows = _rows(_run(here, "apply", str(out), home=home))

    assert rows["lib"]["patch"].startswith("already applied") and rows["lib"]["done"] == ["patch already applied"]
    assert (here / "lib" / "lib.txt").read_text() == "edited\n"


def test_the_real_index_is_never_touched(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    (here / "lib" / "new.txt").write_text("new\n")
    before = _git(here / "lib", "status", "--porcelain")

    _run(here, "export", str(here / ".handoff" / "h"), "--path", "lib/new.txt", home=home)

    assert _git(here / "lib", "status", "--porcelain") == before == "?? new.txt"


def test_the_handoff_folder_stays_out_of_git_status(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    _run(here, "export", str(here / ".handoff" / "h"), home=home)
    assert ".handoff" not in _git(here, "status", "--porcelain")


def test_a_diverged_or_other_branch_repository_is_left_alone(machines: Tuple[Path, Path, Path]) -> None:
    here, there, home = machines
    _commit(here / "lib", "lib.txt", "here\n", "here's")
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home)
    theirs = _commit(there / "lib", "other.txt", "there\n", "there's")

    rows = _rows(_run(there, "apply", str(out), home=home))

    assert rows["lib"]["relation"] == "diverged" and rows["lib"]["done"] == ["left alone: diverged"]
    assert _git(there / "lib", "rev-parse", "HEAD") == theirs


def test_a_repository_behind_on_another_branch_is_not_switched(machines: Tuple[Path, Path, Path]) -> None:
    here, there, home = machines
    _commit(here / "lib", "lib.txt", "two\n", "work")
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home)
    _git(there / "lib", "switch", "-q", "-c", "elsewhere")

    rows = _rows(_run(there, "apply", str(out), home=home))

    assert rows["lib"]["done"] == ["left alone: here on elsewhere, exported on dev/main"]
    assert _git(there / "lib", "symbolic-ref", "--short", "HEAD") == "elsewhere"


def test_the_transcript_travels_and_installs_for_resume(machines: Tuple[Path, Path, Path]) -> None:
    here, there, home = machines
    transcript = home / ".claude" / "projects" / "-some-workspace" / "abc-123.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type": "user"}\n')
    out = here / ".handoff" / "h"
    manifest = _run(here, "export", str(out), "--attach", str(transcript), home=home)
    assert isinstance(manifest, dict) and manifest["transcript"] == "transcript.jsonl"
    assert manifest["attachments"][0]["stored"] == "attachments/abc-123.jsonl"
    transcript.unlink()

    installed = Path(_run(there, "install-transcript", str(out), home=home)["transcript"])  # type: ignore[index]

    assert installed.name == "abc-123.jsonl" and installed.read_text() == '{"type": "user"}\n'
    assert installed.parent.parent == home / ".claude" / "projects"


def test_pack_refuses_without_the_brief(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home)
    result = subprocess.run([sys.executable, str(SCRIPT), "pack", str(out)], cwd=here, capture_output=True, text=True)
    assert result.returncode != 0 and "HANDOFF.md" in result.stderr


def test_an_archive_holds_one_folder_named_like_it(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home)
    (out / "HANDOFF.md").write_text("# brief\n")
    archive = Path(_run(here, "pack", str(out), home=home)["archive"])  # type: ignore[index]
    with tarfile.open(archive) as tar:
        names: List[str] = tar.getnames()
    assert {name.split("/")[0] for name in names} == {"h"}


@pytest.mark.parametrize("data_filter", [True, False], ids=["python-3.12", "python-3.9"])
def test_an_archive_unpacks_beside_itself_with_or_without_the_data_filter(
    machines: Tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch, data_filter: bool
) -> None:
    here, _there, home = machines
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home)
    (out / "HANDOFF.md").write_text("# brief\n")
    archive = Path(_run(here, "pack", str(out), home=home)["archive"])  # type: ignore[index]
    moved = here.parent / "inbox" / archive.name
    moved.parent.mkdir()
    archive.rename(moved)
    if not data_filter:
        _python_without_the_data_filter(monkeypatch)

    folder = _script()._open(moved)

    assert folder == moved.parent / "h"
    assert (folder / "HANDOFF.md").read_text() == "# brief\n" and (folder / "state.json").is_file()


def _member(name: str, kind: bytes = tarfile.REGTYPE, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    info.size = 1 if kind == tarfile.REGTYPE else 0
    return info


def _hostile(outside: Path) -> Dict[str, List[tarfile.TarInfo]]:
    """Archives named ``h.tar.gz`` that would write outside ``h/``, by the member each must be refused for."""
    return {
        "../outside/evil": [_member("h", tarfile.DIRTYPE), _member("../outside/evil")],
        str(outside / "evil"): [_member("h", tarfile.DIRTYPE), _member(str(outside / "evil"))],
        "h/link": [_member("h", tarfile.DIRTYPE), _member("h/link", tarfile.SYMTYPE, str(outside))],
        "h/hard": [_member("h", tarfile.DIRTYPE), _member("h/hard", tarfile.LNKTYPE, str(outside / "evil"))],
        "other/evil": [_member("h", tarfile.DIRTYPE), _member("h/ok.txt"), _member("other/evil")],
    }


@pytest.mark.parametrize("data_filter", [True, False], ids=["python-3.12", "python-3.9"])
@pytest.mark.parametrize("refused", ["../outside/evil", "absolute", "h/link", "h/hard", "other/evil"])
def test_an_archive_with_a_member_outside_its_folder_is_refused_and_nothing_unpacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_filter: bool, refused: str
) -> None:
    inbox, outside = tmp_path / "inbox", tmp_path / "outside"
    inbox.mkdir()
    name = str(outside / "evil") if refused == "absolute" else refused
    archive = inbox / "h.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for info in _hostile(outside)[name]:
            tar.addfile(info, io.BytesIO(b"x") if info.isfile() else None)
    if not data_filter:
        _python_without_the_data_filter(monkeypatch)

    with pytest.raises(SystemExit, match=re.escape(repr(name))):
        _script()._open(archive)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["inbox"] and list(inbox.iterdir()) == [archive]
