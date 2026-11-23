# Shipped by aisland (aisland/harness/scripts/session_handoff.py), installed into .agents/scripts/ by `aisland setup`
# (aisland/setup/harness.py). Change it in aisland; `aisland setup` installs it again.
"""The file half of a session handoff — what /export-session and /import-session run to carry a session's chat from
one machine to another. The other half, ``HANDOFF.md`` (what the session knows), the agent writes.

    python3 .agents/scripts/session_handoff.py export <folder> [--attach FILE ...] [--no-transcript]
    python3 .agents/scripts/session_handoff.py pack <folder>              # copies the chat, writes <folder>.tar.gz
    python3 .agents/scripts/session_handoff.py check <folder|tar.gz>      # what importing would do; changes nothing
    python3 .agents/scripts/session_handoff.py restore <folder|tar.gz>    # puts the chat where `claude --resume` finds it

Run it from the workspace root. Each prints JSON. It needs the standard library only and runs on Python 3.9 and later:
the commands run it as ``python3``, which on macOS is the system's 3.9.

No code travels: every commit is on a remote, and the other machine pulls it. ``export`` writes ``state.json``: for
the root (when it is a repository) and every direct subfolder that is a repository of its own, the branch and the
commit — and, so the export can say what would not travel, the commits no remote has and the files with changes.
``--attach`` copies other files or folders (a scratch output, a screenshot, a memory file) into ``attachments/``. A
folder inside the workspace is kept out of ``git status`` through the root's ``.git/info/exclude``.

``pack`` copies the session's chat (``$CLAUDE_CODE_SESSION_ID``) into ``chat/`` — its transcript up to its last whole
line, and the folder beside it with the subagents' chats and the tool results — unless the export was made with
``--no-transcript``, then writes the archive. It runs last, so the chat holds the whole export but its end.

``check`` compares each repository here with what was exported — the same commit, behind it, ahead, diverged, or the
commit missing — and says where the chat would go. ``restore`` writes the chat into Claude Code's folder for the
folder the session started in, as this machine names it: a workspace at another path, or another HOME, has every
such path in the chat's files rewritten. A session this machine has already (the handoff came back to where it
started) is restored under a new id, so no chat here is ever overwritten; a handoff restored once is not restored
again.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, TypedDict

FORMAT = 1
STATE = "state.json"
HANDOFF = "HANDOFF.md"
CHAT = "chat"
RESTORED = "restored.json"


class RepoState(TypedDict):
    """One repository as the exporting machine left it."""

    path: str
    branch: Optional[str]
    head: str
    unpushed: int
    changed: List[str]


class Attachment(TypedDict):
    source: str
    stored: str


class Manifest(TypedDict):
    """``state.json``."""

    format: int
    exported_at: str
    host: str
    workspace: str
    projects: str
    session_id: Optional[str]
    chat_folder: Optional[str]
    repos: List[RepoState]
    attachments: List[Attachment]


class Row(TypedDict, total=False):
    """One repository of a ``check`` report."""

    path: str
    status: str
    wanted_branch: Optional[str]
    wanted_head: str
    branch: Optional[str]
    head: str
    relation: str
    same_branch: bool


class ChatPlan(TypedDict):
    """Where ``restore`` would put the chat, and what it would rewrite on the way."""

    session_id: str
    folder: str
    rewrites: Dict[str, str]
    taken: bool
    restored: Optional[str]


class Restored(TypedDict):
    session_id: str
    transcript: str
    start_folder: Optional[str]
    taken: bool
    already: bool


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def _repos(root: Path) -> List[str]:
    """The root (``.``) when it is a repository, then every direct subfolder that is a repository of its own — a
    submodule or a clone; never a hidden folder, nor a link to a folder of the workspace (a short name for it)."""
    found = ["."] if (root / ".git").exists() else []
    for folder in sorted(root.iterdir()):
        if folder.name.startswith(".") or not folder.is_dir() or not (folder / ".git").exists():
            continue
        if folder.is_symlink() and root.resolve() in folder.resolve().parents:
            continue
        found.append(folder.name)
    return found


def _changed(repo: Path) -> List[str]:
    """The paths ``git status`` lists (its first column may be a space, which stripping would eat)."""
    result = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return [line[3:] for line in result.stdout.splitlines() if line]


def _projects() -> Path:
    """Where Claude Code keeps its chats: ``$CLAUDE_CONFIG_DIR/projects``, else ``~/.claude/projects``."""
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config).expanduser() if config else Path.home() / ".claude") / "projects"


def _slug(folder: str) -> str:
    """The name of Claude Code's folder for the chats started in ``folder``."""
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in folder)


def _home_relative(path: Path) -> str:
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _private(path: Path, data: bytes) -> None:
    """A chat file holds whatever the conversation showed: only its owner reads it, as Claude Code keeps it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)


def _repo_state(repo: Path, path: str) -> RepoState:
    return {
        "path": path,
        "branch": _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None,
        "head": _git(repo, "rev-parse", "HEAD"),
        "unpushed": int(_git(repo, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0),
        "changed": _changed(repo),
    }


def _attach(out: Path, files: List[str]) -> List[Attachment]:
    attachments: List[Attachment] = []
    for file in files:
        source = Path(file).expanduser()
        stored = Path("attachments") / source.name
        n = 1
        while (out / stored).exists():
            stored = Path("attachments") / f"{source.stem}-{n}{source.suffix}"
            n += 1
        (out / stored).parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, out / stored)
        else:
            shutil.copy2(source, out / stored)
        attachments.append({"source": _home_relative(source), "stored": stored.as_posix()})
    return attachments


def export(root: Path, out: Path, attach: List[str], session_id: Optional[str]) -> Manifest:
    out.mkdir(parents=True, exist_ok=True)
    _exclude(root, out)
    manifest: Manifest = {
        "format": FORMAT,
        "exported_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "workspace": str(root),
        "projects": str(_projects()),
        "session_id": session_id,
        "chat_folder": None,
        "repos": [_repo_state(root / path, path) for path in _repos(root)],
        "attachments": _attach(out, attach),
    }
    (out / STATE).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _exclude(root: Path, out: Path) -> None:
    """Keep a handoff folder inside the workspace out of ``git status``."""
    try:
        relative = out.resolve().relative_to(root.resolve())
    except ValueError:
        return
    if not (root / ".git").exists():
        return
    entry = "/" + relative.parts[0] + "/"
    exclude = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if entry not in lines:
        exclude.write_text("\n".join(lines + [entry]) + "\n")


def _copy_chat(out: Path, session_id: str) -> Optional[str]:
    """The session's chat copied into ``chat/``; the name of the folder Claude Code kept it in, or None."""
    hits = sorted(_projects().glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not hits:
        return None
    transcript = hits[0]
    shutil.rmtree(out / CHAT, ignore_errors=True)
    data = transcript.read_bytes()
    _private(out / CHAT / transcript.name, data[: data.rfind(b"\n") + 1])  # never the line still being written
    if (transcript.parent / session_id).is_dir():
        shutil.copytree(transcript.parent / session_id, out / CHAT / session_id)
    return transcript.parent.name


def pack(out: Path) -> Dict[str, object]:
    if not (out / HANDOFF).is_file():
        raise SystemExit(f"{out / HANDOFF} is missing: write the handoff brief before packing")
    manifest = _manifest(out)
    if manifest["session_id"]:
        manifest["chat_folder"] = _copy_chat(out, manifest["session_id"])
        (out / STATE).write_text(json.dumps(manifest, indent=2) + "\n")
    archive = out.with_name(out.name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(out, arcname=out.name)
    archive.chmod(0o600)  # it holds the chat
    return {"archive": str(archive), "chat": manifest["chat_folder"] is not None}


def _members(tar: tarfile.TarFile, archive: Path, folder: str) -> List[tarfile.TarInfo]:
    """Every member of a handoff archive, each a plain file or folder inside ``folder`` — what ``pack`` writes."""
    members = tar.getmembers()
    for member in members:
        parts = PurePosixPath(member.name).parts
        if not parts or parts[0] != folder or ".." in parts or not (member.isfile() or member.isdir()):
            raise SystemExit(
                f"{archive}: refused {member.name!r} — a handoff archive holds only files and folders inside {folder}/"
            )
    return members


def _open(bundle: Path) -> Path:
    """A handoff folder; a ``.tar.gz`` is unpacked beside itself first, once every member is checked."""
    if bundle.is_dir():
        return bundle
    if not bundle.name.endswith(".tar.gz"):
        raise SystemExit(f"{bundle}: neither a handoff folder nor a .tar.gz")
    target = bundle.with_name(bundle.name[: -len(".tar.gz")])
    if not target.exists():
        with tarfile.open(bundle) as tar:
            members = _members(tar, bundle, target.name)
            if hasattr(tarfile, "data_filter"):  # Python 3.12, and 3.8.17, 3.9.17, 3.10.12, 3.11.4 on
                tar.extractall(bundle.parent, members=members, filter="data")
            else:  # an older Python, macOS's 3.9.6 among them: the members are checked above
                tar.extractall(bundle.parent, members=members)
    return target


def _manifest(folder: Path) -> Manifest:
    manifest: Manifest = json.loads((folder / STATE).read_text())
    if manifest.get("format") != FORMAT:
        raise SystemExit(f"{folder / STATE}: format {manifest.get('format')}, this script reads {FORMAT}")
    return manifest


def _relation(repo: Path, local: str, wanted: str) -> str:
    if local == wanted:
        return "same"
    if not _ok(repo, "cat-file", "-e", f"{wanted}^{{commit}}"):
        return "missing"
    if _ok(repo, "merge-base", "--is-ancestor", local, wanted):
        return "behind"
    if _ok(repo, "merge-base", "--is-ancestor", wanted, local):
        return "ahead"
    return "diverged"


def _check_repo(root: Path, state: RepoState) -> Row:
    repo = root / state["path"]
    row: Row = {"path": state["path"], "wanted_branch": state["branch"], "wanted_head": state["head"]}
    if not (repo / ".git").exists():
        row["status"] = "absent: the repository is not checked out here"
        return row
    local = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None
    row["branch"], row["head"], row["relation"] = branch, local, _relation(repo, local, state["head"])
    row["same_branch"] = branch == state["branch"]
    return row


def _chat_plan(root: Path, folder: Path, manifest: Manifest) -> Optional[ChatPlan]:
    session_id, name = manifest["session_id"], manifest["chat_folder"]
    if not session_id or not name:
        return None
    there, here = _slug(manifest["workspace"]), _slug(str(root))
    if name == there or name.startswith(there + "-"):  # the session started in the workspace or a folder of it
        name = here + name[len(there) :]
    projects = _projects()
    rewrites: Dict[str, str] = {}
    for old, new in (
        (str(Path(manifest["projects"]) / there), str(projects / here)),
        (manifest["workspace"], str(root)),
    ):
        if old != new:
            rewrites[old] = new
    restored: Optional[str] = None
    if (folder / RESTORED).is_file():
        restored = json.loads((folder / RESTORED).read_text())["transcript"]
        restored = restored if restored and Path(restored).is_file() else None
    taken = any(projects.glob(f"*/{session_id}.jsonl")) or any(p.is_dir() for p in projects.glob(f"*/{session_id}"))
    return {
        "session_id": session_id,
        "folder": str(projects / name),
        "rewrites": rewrites,
        "taken": taken,
        "restored": restored,
    }


def check(root: Path, folder: Path) -> Dict[str, object]:
    manifest = _manifest(folder)
    return {
        "repos": [_check_repo(root, state) for state in manifest["repos"]],
        "chat": _chat_plan(root, folder, manifest),
    }


def _rewrite(data: bytes, swaps: Dict[str, str]) -> bytes:
    """``data`` with every ``swaps`` key replaced by its value, in one pass: a replacement is never replaced again."""
    if not swaps:
        return data
    pattern = re.compile(b"|".join(re.escape(old.encode()) for old in sorted(swaps, key=len, reverse=True)))
    return pattern.sub(lambda match: swaps[match.group().decode()].encode(), data)


def restore(root: Path, folder: Path) -> Restored:
    manifest = _manifest(folder)
    plan = _chat_plan(root, folder, manifest)
    if plan is None:
        raise SystemExit("this handoff carries no chat")
    if plan["restored"]:
        before = json.loads((folder / RESTORED).read_text())
        return {
            "session_id": before["session_id"],
            "transcript": before["transcript"],
            "start_folder": before["start_folder"],
            "taken": before["taken"],
            "already": True,
        }
    session_id = plan["session_id"]
    new_id = str(uuid.uuid4()) if plan["taken"] else session_id
    swaps = {**plan["rewrites"], **({session_id: new_id} if new_id != session_id else {})}
    source, target = folder / CHAT, Path(plan["folder"])
    target.mkdir(mode=0o700, parents=True, exist_ok=True)  # as Claude Code makes its own
    if (source / session_id).is_dir():
        for file in sorted((source / session_id).rglob("*")):
            if file.is_file():
                _private(target / new_id / file.relative_to(source / session_id), _rewrite(file.read_bytes(), swaps))
    transcript = target / f"{new_id}.jsonl"
    _private(transcript, _rewrite((source / f"{session_id}.jsonl").read_bytes(), swaps))  # last: it makes it a chat
    done: Restored = {
        "session_id": new_id,
        "transcript": str(transcript),
        "start_folder": _start_folder(root, target.name),
        "taken": plan["taken"],
        "already": False,
    }
    (folder / RESTORED).write_text(json.dumps(done, indent=2) + "\n")
    return done


def _start_folder(root: Path, name: str) -> Optional[str]:
    """The folder here whose chats Claude Code keeps under ``name`` — the workspace or a folder of it, one level down
    (where ``claude --resume`` is run); None for one deeper or gone."""
    for folder in [root, *sorted(p for p in root.iterdir() if p.is_dir())]:
        if _slug(str(folder)) == name:
            return str(folder)
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("folder", type=Path)
    ex.add_argument("--attach", action="append", default=[], help="a file or folder to carry along")
    ex.add_argument("--session-id", default=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    ex.add_argument("--no-transcript", action="store_true", help="leave the session's chat out")
    for name in ("pack", "check", "restore"):
        sub.add_parser(name).add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    root = Path.cwd()
    folder: Path = args.folder.expanduser().resolve()
    result: object
    if args.command == "export":
        result = export(root, folder, args.attach, None if args.no_transcript else args.session_id)
    elif args.command == "pack":
        result = pack(folder)
    elif args.command == "check":
        result = check(root, _open(folder))
    else:
        result = restore(root, _open(folder))
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
