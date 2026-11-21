# Shipped by aisland (aisland/harness/scripts/session_handoff.py), installed into .agents/scripts/ by `aisland setup`
# (aisland/setup/harness.py). Change it in aisland; `aisland setup` installs it again.
"""The git and file half of a session handoff — what /export-session and /import-session run to carry a session's
work from one machine to another. The other half, ``HANDOFF.md`` (what the session knows), the agent writes.

    python3 .agents/scripts/session_handoff.py export <folder> [--path FILE ...] [--attach FILE ...]
    python3 .agents/scripts/session_handoff.py pack <folder>              # <folder>.tar.gz, once HANDOFF.md is in
    python3 .agents/scripts/session_handoff.py check <folder|tar.gz>      # what importing would do; changes nothing
    python3 .agents/scripts/session_handoff.py apply <folder|tar.gz>      # do it
    python3 .agents/scripts/session_handoff.py install-transcript <folder|tar.gz>  # for `claude --resume <id>` here

Run it from the workspace root (the repository whose submodules are the projects). Each prints JSON.

``export`` writes ``state.json``: for the root and every checked-out submodule, the branch, HEAD, its remote and the
files with changes. Commits on no remote travel as a ``git bundle`` (``repos/<name>.bundle``), so nothing needs
pushing. The session's own uncommitted edits — the ``--path`` files, never another session's — travel as one binary
patch per repository (``repos/<name>.patch``), new and deleted files included, made through a private copy of the
index so the real one is never touched. ``--attach`` copies other files or folders (a scratch output, a screenshot, a
memory file) into ``attachments/``. The session's transcript (``$CLAUDE_CODE_SESSION_ID``, found under
``~/.claude/projects``) is copied as ``transcript.jsonl`` unless ``--no-transcript``. A folder inside the workspace
is kept out of ``git status`` through the root's ``.git/info/exclude``.

``check`` compares each repository here with what was exported — the same commit, behind it (fast-forwardable),
ahead, diverged, or the commit missing — and tries each patch with ``git apply --check``. ``apply`` fetches the
bundles (local files: no network), fast-forwards a repository that is behind ON THE SAME BRANCH, and applies a patch
that applies cleanly. It never switches a branch, resets, stashes, merges anything but a fast-forward, or touches a
repository it cannot move cleanly: those it reports, and the user decides.
"""

import argparse
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, TypedDict

FORMAT = 1
STATE = "state.json"
HANDOFF = "HANDOFF.md"
TRANSCRIPT = "transcript.jsonl"
APPLIED = "already applied: every change is on disk here"


class RepoState(TypedDict):
    """One repository as the exporting machine left it."""

    path: str
    branch: Optional[str]
    head: str
    upstream: Optional[str]
    origin: Optional[str]
    unpushed: int
    changed: List[str]
    bundle: Optional[str]
    patch: Optional[str]
    patch_files: List[str]


class Attachment(TypedDict):
    source: str
    stored: str


class Manifest(TypedDict):
    """``state.json``."""

    format: int
    exported_at: str
    host: str
    workspace: str
    session_id: Optional[str]
    transcript: Optional[str]
    repos: List[RepoState]
    attachments: List[Attachment]


class Row(TypedDict, total=False):
    """One repository of a ``check`` or ``apply`` report."""

    path: str
    status: str
    wanted_branch: Optional[str]
    wanted_head: str
    branch: Optional[str]
    head: str
    relation: str
    same_branch: bool
    bundle: str
    patch: str
    patch_files: List[str]
    done: List[str]


def _git(repo: Path, *args: str, check: bool = True, env: Optional[Dict[str, str]] = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env={**os.environ, **(env or {})}
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} in {repo}: {result.stderr.strip()}")
    return result.stdout.strip() if result.returncode == 0 else ""


def _ok(repo: Path, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def _repos(root: Path) -> List[str]:
    """The root (``.``) and every checked-out submodule, by its path from the root."""
    listed = _git(root, "submodule", "foreach", "--quiet", "--recursive", 'echo "$displaypath"', check=False)
    return ["."] + [line for line in listed.splitlines() if line]


def _changed(repo: Path) -> List[str]:
    """The paths ``git status`` lists (its first column may be a space, which stripping would eat)."""
    result = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return [line[3:] for line in result.stdout.splitlines() if line]


def _bundle_name(path: str) -> str:
    return "root" if path == "." else path.replace("/", "__")


def _absolute(file: str) -> Path:
    """``file`` resolved, also when it no longer exists (a deleted file's change is a change too)."""
    path = Path(file).expanduser()
    path = path if path.is_absolute() else Path.cwd() / path
    return path.resolve() if path.exists() else path.parent.resolve() / path.name


def _owner(root: Path, repos: List[str], file: Path) -> str:
    """The innermost repository holding ``file``."""
    best = "."
    for path in repos:
        folder = root / path
        if path != "." and (file == folder or folder in file.parents) and (best == "." or len(path) > len(best)):
            best = path
    return best


def _session_patch(repo: Path, files: List[str]) -> str:
    """``files``' changes against HEAD, new and deleted files included, staged in a private copy of the index."""
    real_index = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    with tempfile.TemporaryDirectory() as scratch:
        index = Path(scratch) / "index"
        if real_index.exists():
            shutil.copyfile(real_index, index)
        env = {"GIT_INDEX_FILE": str(index)}
        _git(repo, "add", "-A", "--", *files, env=env)
        return subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--binary", "HEAD", "--", *files],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **env},
        ).stdout


def _find_transcript(session_id: str) -> Optional[Path]:
    projects = Path.home() / ".claude" / "projects"
    hits = sorted(projects.glob(f"*/{session_id}.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def _home_relative(path: Path) -> str:
    try:
        return "~/" + path.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _repo_state(repo: Path, path: str, out: Path, patch_files: List[str]) -> RepoState:
    name = _bundle_name(path)
    state: RepoState = {
        "path": path,
        "branch": _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None,
        "head": _git(repo, "rev-parse", "HEAD"),
        "upstream": _git(repo, "rev-parse", "--abbrev-ref", "@{u}", check=False) or None,
        "origin": _git(repo, "remote", "get-url", "origin", check=False) or None,
        "unpushed": int(_git(repo, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0),
        "changed": _changed(repo),
        "bundle": None,
        "patch": None,
        "patch_files": sorted(patch_files),
    }
    if state["unpushed"]:
        _git(repo, "bundle", "create", str(out / "repos" / f"{name}.bundle"), "HEAD", "--not", "--remotes")
        state["bundle"] = f"repos/{name}.bundle"
    if patch_files:
        patch = _session_patch(repo, state["patch_files"])
        if patch:
            (out / "repos" / f"{name}.patch").write_text(patch)
            state["patch"] = f"repos/{name}.patch"
    return state


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


def export(root: Path, out: Path, paths: List[str], attach: List[str], session_id: Optional[str]) -> Manifest:
    (out / "repos").mkdir(parents=True, exist_ok=True)
    repos = _repos(root)
    by_repo: Dict[str, List[str]] = {}
    for file in paths:
        absolute = _absolute(file)
        owner = _owner(root.resolve(), repos, absolute)
        by_repo.setdefault(owner, []).append(os.path.relpath(absolute, root.resolve() / owner))
    states = [_repo_state(root / path, path, out, by_repo.get(path, [])) for path in repos]
    transcript = _find_transcript(session_id) if session_id else None
    if transcript:
        shutil.copy2(transcript, out / TRANSCRIPT)
    manifest: Manifest = {
        "format": FORMAT,
        "exported_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "workspace": _home_relative(root),
        "session_id": session_id,
        "transcript": TRANSCRIPT if transcript else None,
        "repos": states,
        "attachments": _attach(out, attach),
    }
    (out / STATE).write_text(json.dumps(manifest, indent=2) + "\n")
    _exclude(root, out)
    return manifest


def _exclude(root: Path, out: Path) -> None:
    """Keep a handoff folder inside the workspace out of ``git status``."""
    try:
        relative = out.resolve().relative_to(root.resolve())
    except ValueError:
        return
    entry = "/" + relative.parts[0] + "/"
    exclude = Path(_git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if entry not in lines:
        exclude.write_text("\n".join(lines + [entry]) + "\n")


def pack(out: Path) -> Path:
    if not (out / HANDOFF).is_file():
        raise SystemExit(f"{out / HANDOFF} is missing: write the handoff brief before packing")
    archive = out.with_name(out.name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(out, arcname=out.name)
    return archive


def _open(bundle: Path) -> Path:
    """A handoff folder; a ``.tar.gz`` is unpacked beside itself first."""
    if bundle.is_dir():
        return bundle
    if not bundle.name.endswith(".tar.gz"):
        raise SystemExit(f"{bundle}: neither a handoff folder nor a .tar.gz")
    target = bundle.with_name(bundle.name[: -len(".tar.gz")])
    if not target.exists():
        with tarfile.open(bundle) as tar:
            tar.extractall(bundle.parent, filter="data")
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


def _check_repo(root: Path, folder: Path, state: RepoState, fetch_bundle: bool) -> Row:
    repo = root / state["path"]
    row: Row = {"path": state["path"], "wanted_branch": state["branch"], "wanted_head": state["head"]}
    if not (repo / ".git").exists():
        row["status"] = "absent: the repository is not checked out here"
        return row
    bundle = state["bundle"]
    if fetch_bundle and bundle and not _ok(repo, "cat-file", "-e", f"{state['head']}^{{commit}}"):
        _git(repo, "fetch", "--quiet", str(folder / bundle), check=False)
    local = _git(repo, "rev-parse", "HEAD")
    branch = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None
    row["branch"], row["head"], row["relation"] = branch, local, _relation(repo, local, state["head"])
    row["same_branch"] = branch == state["branch"]
    if row["relation"] == "missing" and bundle:
        verified = _ok(repo, "bundle", "verify", "--quiet", str(folder / bundle))
        row["bundle"] = "fetchable" if verified else "needs `git fetch` first (its base commits are missing)"
    if state["patch"]:
        patch = str((folder / state["patch"]).resolve())
        result = subprocess.run(["git", "-C", str(repo), "apply", "--check", patch], capture_output=True, text=True)
        if result.returncode == 0:
            row["patch"] = "applies"
        elif _ok(repo, "apply", "--check", "--reverse", patch):
            row["patch"] = APPLIED
        else:
            row["patch"] = f"does not apply: {result.stderr.strip()}"
        row["patch_files"] = state["patch_files"]
    return row


def check(root: Path, folder: Path) -> List[Row]:
    return [_check_repo(root, folder, state, fetch_bundle=False) for state in _manifest(folder)["repos"]]


def _apply_repo(root: Path, folder: Path, state: RepoState) -> Row:
    row = _check_repo(root, folder, state, fetch_bundle=True)
    repo = root / state["path"]
    done: List[str] = []
    relation = row.get("relation")
    if relation == "behind" and row.get("same_branch"):
        result = subprocess.run(
            ["git", "-C", str(repo), "merge", "--ff-only", "--quiet", state["head"]], capture_output=True, text=True
        )
        done.append("fast-forwarded" if result.returncode == 0 else f"not fast-forwarded: {result.stderr.strip()}")
    elif relation == "behind":
        done.append(f"left alone: here on {row.get('branch')}, exported on {state['branch']}")
    elif relation in ("ahead", "diverged", "missing"):
        done.append(f"left alone: {relation}")
    if row.get("patch") == APPLIED:
        done.append("patch already applied")
    elif state["patch"] and "status" not in row:
        result = subprocess.run(
            ["git", "-C", str(repo), "apply", str((folder / state["patch"]).resolve())], capture_output=True, text=True
        )
        done.append("patch applied" if result.returncode == 0 else f"patch not applied: {result.stderr.strip()}")
    row["done"] = done
    return row


def apply(root: Path, folder: Path) -> List[Row]:
    return [_apply_repo(root, folder, state) for state in _manifest(folder)["repos"]]


def install_transcript(folder: Path) -> Path:
    manifest = _manifest(folder)
    if not manifest["transcript"] or not manifest["session_id"]:
        raise SystemExit("this handoff carries no transcript")
    slug = "".join(c if c.isalnum() else "-" for c in str(Path.cwd().resolve()))
    target = Path.home() / ".claude" / "projects" / slug / f"{manifest['session_id']}.jsonl"
    if target.exists():
        raise SystemExit(f"{target} exists already: the session is known here")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / manifest["transcript"], target)
    return target


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("folder", type=Path)
    ex.add_argument("--path", action="append", default=[], help="a file whose uncommitted change is this session's")
    ex.add_argument("--attach", action="append", default=[], help="a file or folder to carry along")
    ex.add_argument("--session-id", default=os.environ.get("CLAUDE_CODE_SESSION_ID"))
    ex.add_argument("--no-transcript", action="store_true", help="leave the session's transcript out")
    for name in ("pack", "check", "apply", "install-transcript"):
        sub.add_parser(name).add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    root = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel"))
    folder: Path = args.folder.expanduser().resolve()
    result: object
    if args.command == "export":
        session_id = None if args.no_transcript else args.session_id
        result = export(root, folder, args.path, args.attach, session_id)
    elif args.command == "pack":
        result = {"archive": str(pack(folder))}
    elif args.command == "check":
        result = check(root, _open(folder))
    elif args.command == "apply":
        result = apply(root, _open(folder))
    else:
        result = {"transcript": str(install_transcript(_open(folder)))}
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
