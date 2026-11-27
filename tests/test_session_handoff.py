"""The harness helper ``session_handoff.py`` carries a session's chat from one workspace to another and lists it in
the desktop app there. It carries no code: every commit is on a remote.

Each test builds throwaway repositories — an origin, a root with one submodule, and a second clone of both standing
for the other machine — and a fake chat in a HOME of its own, so nothing of the workspace or of the real chats is
touched.
"""

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional, Tuple

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "aisland" / "harness" / "scripts" / "session_handoff.py"
SESSION = "abc-123"
Json = Any  # Any: the script's JSON output, read by key


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


def _env(home: Path, **extra: str) -> Dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
        "CLAUDE_CODE_SESSION_ID": SESSION,
        **extra,
    }


def _run(cwd: Path, *args: str, home: Path, **env: str) -> Json:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, env=_env(home, **env)
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _refused(cwd: Path, *args: str, home: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True, env=_env(home)
    )
    assert result.returncode != 0, result.stdout
    return result.stderr


def _slug(folder: Path) -> str:
    """The name Claude Code gives a folder's chats: every character that is no ASCII letter or digit becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(folder))


def _home(path: Path) -> Path:
    path.mkdir()
    (path / ".gitconfig").write_text(
        '[user]\n\temail = test@example.com\n\tname = test\n[protocol "file"]\n\tallow = always\n'
    )
    return path


@pytest.fixture()
def machines(tmp_path: Path) -> Tuple[Path, Path, Path]:
    """``here`` and ``there``: two clones of a root whose submodule ``lib`` holds ``lib.txt``; and the HOME both use."""
    home = _home(tmp_path / "home")
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


def _chat(home: Path, started_in: Path, workspace: Path) -> Path:
    """A chat of ``SESSION`` started in ``started_in``: two lines naming paths of ``workspace`` and one of its tool
    results, a line still being written, and the folder beside it (a tool result, a subagent's chat)."""
    folder = home / ".claude" / "projects" / _slug(started_in)
    result = folder / SESSION / "tool-results" / "r1.txt"
    lines = [
        {"type": "user", "cwd": str(started_in), "sessionId": SESSION, "message": f"read {workspace}/lib/lib.txt"},
        {"type": "assistant", "sessionId": SESSION, "message": f"output saved to {result}"},
    ]
    transcript = folder / f"{SESSION}.jsonl"
    result.parent.mkdir(parents=True)
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines) + '{"type": "assist')
    result.write_text(f"{workspace}/lib/lib.txt: one\n")
    agent = folder / SESSION / "subagents" / "agent-a1.jsonl"
    agent.parent.mkdir()
    agent.write_text(json.dumps({"cwd": str(workspace), "sessionId": SESSION}) + "\n")
    return transcript


def _handoff(workspace: Path, home: Path, *args: str) -> Path:
    """Export and pack a handoff of ``workspace`` the way ``/export-session`` does: the brief is written between."""
    out = workspace / ".handoff" / "h"
    _run(workspace, "export", str(out), *args, home=home)
    (out / "HANDOFF.md").write_text("# brief\n")
    _run(workspace, "pack", str(out), home=home)
    return out


def _files(folder: Path) -> Dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def _desktop(home: Path) -> Path:
    """The desktop app's Code sessions in ``home``: an account folder last used in the past and the one signed in now,
    each holding a session record."""
    base = home / "Library" / "Application Support" / "Claude" / "claude-code-sessions"
    for account, model, age in (("old/org", "old-model", 1000), ("now/org", "claude-model", 0)):
        folder = base / account
        folder.mkdir(parents=True)
        record = folder / f"local_{uuid.uuid4()}.json"
        record.write_text(json.dumps({"sessionId": record.stem, "model": model, "effort": "xhigh"}))
        stamp = record.stat().st_mtime - age
        os.utime(record, (stamp, stamp))
    return base / "now" / "org"


def test_the_export_records_each_repositorys_branch_and_commit_and_carries_no_code(
    machines: Tuple[Path, Path, Path],
) -> None:
    here, _there, home = machines
    head = _commit(here / "lib", "lib.txt", "two\n", "unpushed work")
    (here / "lib" / "new.txt").write_text("a new file\n")
    (here / "README.md").write_text("an edit\n")
    before = {repo: _git(here / repo, "status", "--porcelain") for repo in (".", "lib")}
    out = here / ".handoff" / "h"

    manifest = _run(here, "export", str(out), home=home)

    repos = {repo["path"]: repo for repo in manifest["repos"]}
    assert sorted(repos) == [".", "lib"]
    assert repos["lib"]["branch"] == "dev/main" and repos["lib"]["head"] == head
    assert repos["lib"]["unpushed"] == 1 and repos["lib"]["changed"] == ["new.txt"]
    assert repos["."]["unpushed"] == 0 and "README.md" in repos["."]["changed"]
    assert sorted(p.name for p in out.iterdir()) == ["state.json"], "no bundle, no patch"
    assert {repo: _git(here / repo, "status", "--porcelain") for repo in (".", "lib")} == before


def test_a_workspace_with_no_root_repository_is_read_from_its_clones(tmp_path: Path) -> None:
    """A workspace whose root is no repository (its projects plain clones beside a root project) is read from the
    folders: each direct subfolder that is a repository of its own, never a link to one of them or a hidden one."""
    home = _home(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("app", "lib", ".hidden"):
        _git(workspace, "init", "-q", "-b", "dev/main", name)
        _identity(workspace / name)
        _commit(workspace / name, "a.txt", "a\n", "start")
    (workspace / "notes").mkdir()
    (workspace / "short").symlink_to(workspace / "lib")

    manifest = _run(workspace, "export", str(workspace / ".handoff" / "h"), home=home)

    assert [repo["path"] for repo in manifest["repos"]] == ["app", "lib"]
    assert manifest["workspace"] == str(workspace)


def test_a_root_repository_with_no_commit_is_left_out(tmp_path: Path) -> None:
    """The root repository beside a root project records nothing and has no commit (`aisland workspace init`): there is
    no branch or commit of it to carry, and `git rev-parse HEAD` there fails."""
    home = _home(tmp_path / "home")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    _git(workspace, "init", "-q", "-b", "dev/main", "app")
    _identity(workspace / "app")
    _commit(workspace / "app", "a.txt", "a\n", "start")

    manifest = _run(workspace, "export", str(workspace / ".handoff" / "h"), home=home)

    assert [repo["path"] for repo in manifest["repos"]] == ["app"]


def test_the_handoff_folder_stays_out_of_git_status(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    manifest = _run(here, "export", str(here / ".handoff" / "h"), home=home)
    assert ".handoff" not in _git(here, "status", "--porcelain")
    assert next(r for r in manifest["repos"] if r["path"] == ".")["changed"] == []


def test_the_whole_chat_travels_as_it_is_when_packed(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    transcript = _chat(home, here, here)
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home)
    (out / "HANDOFF.md").write_text("# brief\n")
    with transcript.open("a") as chat:  # the session writes on: the brief, then the pack itself
        chat.write('nt"}\n{"type": "user", "message": "pack it"}\n{"type": "assis')

    packed = _run(here, "pack", str(out), home=home)

    assert packed["chat"] is True and Path(packed["archive"]).stat().st_mode & 0o077 == 0, "it holds the chat"
    copied = (out / "chat" / f"{SESSION}.jsonl").read_text()
    assert copied == transcript.read_text().rsplit("\n", 1)[0] + "\n", "every whole line, never the one being written"
    assert _files(out / "chat" / SESSION) == _files(transcript.parent / SESSION)
    assert json.loads((out / "state.json").read_text())["chat_folder"] == _slug(here)
    with tarfile.open(packed["archive"]) as tar:
        assert f"h/chat/{SESSION}/subagents/agent-a1.jsonl" in tar.getnames()


@pytest.mark.parametrize("config_dir", [False, True], ids=["home", "CLAUDE_CONFIG_DIR"])
def test_a_chat_restored_at_the_same_path_is_the_same_files(
    machines: Tuple[Path, Path, Path], tmp_path: Path, config_dir: bool
) -> None:
    here, _there, home = machines
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "config")} if config_dir else {}
    chats = (tmp_path / "config" if config_dir else home / ".claude") / "projects"
    _chat(home, here, here)
    if config_dir:
        (home / ".claude").rename(tmp_path / "config")
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home, **env)
    (out / "HANDOFF.md").write_text("# brief\n")
    _run(here, "pack", str(out), home=home, **env)
    packed = _files(out / "chat")
    for folder in chats.iterdir():  # the other machine has none of it
        subprocess.run(["rm", "-rf", str(folder)], check=True)

    restored = _run(here, "restore", str(out), home=home, **env)

    assert restored["session_id"] == SESSION and restored["taken"] is False
    assert restored["title"] == "brief" and restored["desktop"] is None, "no desktop app in this HOME"
    assert Path(restored["transcript"]) == chats / _slug(here) / f"{SESSION}.jsonl"
    assert restored["start_folder"] == str(here)
    assert _files(chats / _slug(here)) == packed
    assert (chats / _slug(here)).stat().st_mode & 0o077 == 0, "a folder of chats is the user's alone"
    assert (chats / _slug(here) / f"{SESSION}.jsonl").stat().st_mode & 0o077 == 0, "a chat is the user's alone"


def test_a_chat_restored_at_another_path_names_the_paths_there(
    machines: Tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """The other machine keeps the workspace elsewhere and has another HOME: the chat goes to the folder Claude Code
    reads there for the folder the session started in (here ``lib``), and every path of the workspace and of the
    chat's own files is that machine's."""
    here, there, home = machines
    home_there = _home(tmp_path / "home-there")
    _chat(home, here / "lib", here)
    out = _handoff(here, home)
    chats = home_there / ".claude" / "projects" / _slug(there / "lib")

    restored = _run(there, "restore", str(out), home=home_there)

    assert Path(restored["transcript"]) == chats / f"{SESSION}.jsonl" and restored["start_folder"] == str(there / "lib")
    lines = [json.loads(line) for line in (chats / f"{SESSION}.jsonl").read_text().splitlines()]
    assert lines[0]["cwd"] == str(there / "lib") and lines[0]["message"] == f"read {there}/lib/lib.txt"
    assert lines[1]["message"] == f"output saved to {chats / SESSION / 'tool-results' / 'r1.txt'}"
    assert (chats / SESSION / "tool-results" / "r1.txt").read_text() == f"{there}/lib/lib.txt: one\n"
    assert json.loads((chats / SESSION / "subagents" / "agent-a1.jsonl").read_text())["cwd"] == str(there)
    old = (str(here).encode(), f"{home}/".encode())  # home-there starts with home's name: the slash tells them apart
    assert not any(path in data for data in _files(chats).values() for path in old)


def test_a_session_this_machine_has_is_restored_under_a_new_id_and_only_once(machines: Tuple[Path, Path, Path]) -> None:
    """Back on the machine it came from, the session is there already, and longer than the copy that left: the
    export turn went on writing. The restored chat gets an id of its own; the one here stays as it is."""
    here, _there, home = machines
    original = _chat(home, here, here)
    out = _handoff(here, home)
    with original.open("a") as chat:
        chat.write('nt", "message": "the export turn ends here"}\n')
    kept = _files(original.parent)

    restored = _run(here, "restore", str(out), home=home)

    new = restored["session_id"]
    assert restored["taken"] is True and str(uuid.UUID(new)) == new
    assert {k: v for k, v in _files(original.parent).items() if not k.startswith(new)} == kept
    files = {k: v for k, v in _files(original.parent).items() if k.startswith(new)}
    assert sorted(files) == [f"{new}.jsonl", f"{new}/subagents/agent-a1.jsonl", f"{new}/tool-results/r1.txt"]
    assert not any(SESSION.encode() in data for data in files.values())
    assert json.loads(files[f"{new}.jsonl"].splitlines()[0])["sessionId"] == new

    again = _run(here, "restore", str(out), home=home)

    assert again == {**restored, "already": True}
    assert len(list(original.parent.glob("*.jsonl"))) == 2


def test_restore_refuses_a_handoff_without_a_chat(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    _chat(home, here, here)
    out = _handoff(here, home, "--no-transcript")
    assert not (out / "chat").exists() and json.loads((out / "state.json").read_text())["session_id"] is None
    assert "carries no chat" in _refused(here, "restore", str(out), home=home)


def test_the_restored_chat_is_listed_in_the_desktop_app_under_the_briefs_heading(
    machines: Tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """The record goes to the account signed in now (the folder holding the newest record), points at the restored
    chat and its folder, and asks before every edit whatever the session that left was allowed."""
    here, there, home = machines
    home_there = _home(tmp_path / "home-there")
    sessions = _desktop(home_there)
    _chat(home, here, here)
    out = _handoff(here, home)
    (out / "HANDOFF.md").write_text("# Handoff: signal detection\n\nthe brief\n")

    restored = _run(there, "restore", str(out), home=home_there)

    record_path = Path(restored["desktop"])
    assert restored["title"] == "signal detection" and record_path.parent == sessions
    record = json.loads(record_path.read_text())
    assert record["sessionId"] == record_path.stem and record["sessionId"].startswith("local_")
    assert record["cliSessionId"] == SESSION and record["cwd"] == record["originCwd"] == str(there)
    assert record["title"] == "signal detection" and record["isArchived"] is False
    assert (record["model"], record["effort"], record["permissionMode"]) == ("claude-model", "xhigh", "default")
    assert record_path.stat().st_mode & 0o077 == 0, "a session record is the user's alone"


def test_a_chat_restored_before_the_desktop_record_existed_is_listed_once_on_the_next_restore(
    machines: Tuple[Path, Path, Path], tmp_path: Path
) -> None:
    here, there, home = machines
    home_there = _home(tmp_path / "home-there")
    _chat(home, here, here)
    out = _handoff(here, home)
    first = _run(there, "restore", str(out), home=home_there)
    assert first["desktop"] is None
    sessions = _desktop(home_there)  # the app is installed, or first used, afterwards

    second = _run(there, "restore", str(out), home=home_there)
    third = _run(there, "restore", str(out), home=home_there)

    assert second["already"] is True and Path(second["desktop"]).parent == sessions
    assert third == second, "listed once"
    assert len(list(sessions.glob("local_*.json"))) == 2


def test_pack_refuses_without_the_brief(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home)
    assert "HANDOFF.md" in _refused(here, "pack", str(out), home=home)


def test_an_archive_holds_one_folder_named_like_it(machines: Tuple[Path, Path, Path]) -> None:
    here, _there, home = machines
    out = here / ".handoff" / "h"
    _run(here, "export", str(out), home=home)
    (out / "HANDOFF.md").write_text("# brief\n")
    archive = Path(_run(here, "pack", str(out), home=home)["archive"])
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
    archive = Path(_run(here, "pack", str(out), home=home)["archive"])
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
