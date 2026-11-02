"""The wheel versions no branch or tag holds, pruned from a project's history (``aisland wheels prune``), and another
clone brought onto the pruned history (``aisland wheels reset``).

``aisland wheels build --commit`` commits ``wheels/``; every rebuild that changes a wheel adds its bytes to the
history for good, and a clone carries every version ever committed. A prune keeps each wheel version that a branch
or a tag holds — this clone's branches and tags and origin's — and removes every other ``wheels/**.whl`` version
from every commit. Each branch tip and tag keeps exactly the files it has; only commit ids change.

The run, in order (``--apply``; without it the run fetches, counts and changes nothing):

1. **Refusals** (:func:`_problems`): changes no commit holds, a branch with commits origin lacks or behind origin's,
   a tag that differs from origin's. A rewrite of such a clone would lose or tangle work.
2. **A backup** of every ref, ``<git dir>/wheels-prune/<stamp>.bundle``, and the command that puts it back.
3. **The rewrite**, by git-filter-repo (the ``prune`` extra): ``--strip-blobs-with-ids`` with the versions to go,
   ``--preserve-commit-hashes`` and no pruning of commits left empty, so the rewrite is a pure function of each
   commit — another clone stripping the same versions gets the same commit ids (what :func:`reset` relies on).
   Every kept ref's tree is checked to be what it was.
4. **The push**, atomic, each branch and tag with ``--force-with-lease`` against the commit fetched: a push someone
   made meanwhile refuses the whole push and origin stays as it was. With it goes :data:`PRUNE_REF`, a chain of one
   commit per prune holding the versions stripped (:data:`STRIPPED_FILE`) and the old → new commit map
   (:data:`COMMITS_FILE`) — the record another clone follows. The record holds commit ids as text, so it keeps no
   old commit alive.
5. **This clone shrinks**: its reflogs expire (a stash's excepted: it holds work) and gc drops what nothing reaches.
6. **The root's pointer**: when the workspace root records the project, the commit it records is replaced by its
   rewritten twin and committed alone — the root keeps the commit it last recorded, under its new id, so an
   untracked project is no more recorded than before.

:func:`reset` runs on every other clone, in whatever state it is: it fetches the record, sets the changes no commit
holds aside (in :data:`_SET_ASIDE_REF`, rewritten with the rest), strips each prune's versions this clone still reaches with the same filter (its unpushed
commits, merges, local tags and stash rewritten on top as they are), puts the changes back, checks each branch that
was origin's landed on the commit origin's record names, shrinks the clone the same way and, like the prune, moves
the root's pointer to the rewritten twin of the commit it records — also in a clone a reset already brought over.

git-filter-repo runs through :mod:`aisland.filter_repo`, which reads the git config NUL-separated: a value over
several lines stops git-filter-repo's own reader.
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from aisland import gitcmd
from aisland.errors import AislandError
from aisland.wheels import find_target, workspace_projects
from aisland.wheels_files import WHEELS_DIR
from aisland.wheels_report import human_size

#: Where origin keeps the record of every prune: a chain of commits, one per prune, never a branch or a tag.
PRUNE_REF = "refs/wheels-prune/map"

#: The record commit's file listing the wheel versions (blob ids) that prune stripped, one per line.
STRIPPED_FILE = "stripped-blobs"

#: The record commit's file mapping each commit the prune rewrote, ``<old> <new>`` per line.
COMMITS_FILE = "commit-map"

#: The folder inside the git directory that holds the backups.
BACKUP_DIR = "wheels-prune"

#: The extra that brings git-filter-repo.
EXTRA = "prune"

#: The ref namespaces a prune rewrites and whose tips keep their wheels; ``refs/stash`` is rewritten too.
_LOCAL_NAMESPACES = ("refs/heads/", "refs/tags/")
_ORIGIN_NAMESPACE = "refs/remotes/origin/"
_STASH = "refs/stash"

#: Holds a detached HEAD's commit while the rewrite runs.
_HEAD_REF = "refs/wheels-prune/head"

#: Holds a reset's changes no commit held (a ``git stash create`` commit) while the rewrite runs; never the user's
#: stash, whose reflog git-filter-repo does not carry.
_SET_ASIDE_REF = "refs/wheels-prune/set-aside"


class PruneError(AislandError, RuntimeError):
    """A prune or a reset that cannot go on: the message says why and what to do."""


@dataclass
class PruneReport:
    """What ``aisland wheels prune`` found and, with ``--apply``, did — JSON-serializable (``--json``).

    Args:
        project: The project's folder name.
        applied: Whether history was rewritten (``--apply``, something to strip, no refusal).
        versions: How many wheel versions the history holds.
        stripped: The wheel versions (blob ids) that go — or went.
        stripped_bytes: Their size.
        kept: How many versions stay.
        holders: The branches and tags that hold a kept version, as ``git`` names them.
        problems: What stops ``--apply``.
        pushed: The refs pushed to origin.
        rewritten: How many commits changed id.
        backup: The bundle holding every ref as it was.
        restore: The command that puts the backup back.
        root_pointer: ``(old, new)`` when the root's pointer was re-recorded.
        size_before: This clone's object store before, in bytes.
        size_after: And after.
    """

    project: str
    applied: bool
    versions: int
    stripped: List[str]
    stripped_bytes: int
    kept: int
    holders: List[str]
    problems: List[str] = field(default_factory=list)
    pushed: List[str] = field(default_factory=list)
    rewritten: int = 0
    backup: Optional[str] = None
    restore: Optional[str] = None
    root_pointer: Optional[Tuple[str, str]] = None
    size_before: Optional[int] = None
    size_after: Optional[int] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ResetReport:
    """What ``aisland wheels reset`` did — JSON-serializable (``--json``).

    Args:
        project: The project's folder name.
        prunes: How many of origin's prunes this clone took.
        moved: Each ref that changed, ``(ref, old, new)``.
        ahead: Each branch now holding commits origin lacks, and how many — its unpushed work, kept.
        tags_differing: Tags this clone holds that differ from origin's, left as they are.
        set_aside: The changes no commit held (``git status --porcelain`` lines), set aside during the rewrite and
            put back.
        root_pointer: ``(old, new)`` when the root's pointer was re-recorded.
        backup: The bundle holding every ref as it was (None: nothing changed).
        restore: The command that puts the backup back.
        size_before: This clone's object store before, in bytes.
        size_after: And after.
    """

    project: str
    prunes: int
    moved: List[Tuple[str, str, str]] = field(default_factory=list)
    ahead: Dict[str, int] = field(default_factory=dict)
    tags_differing: List[str] = field(default_factory=list)
    set_aside: List[str] = field(default_factory=list)
    root_pointer: Optional[Tuple[str, str]] = None
    backup: Optional[str] = None
    restore: Optional[str] = None
    size_before: Optional[int] = None
    size_after: Optional[int] = None

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


# --- git ------------------------------------------------------------------------------------------


def _run(
    repo: Path, *args: str, network: bool = False, stdin: Optional[str] = None, env: Optional[Dict[str, str]] = None
) -> subprocess.CompletedProcess[str]:
    # A network call: a host asking for a password fails instead of waiting on a prompt no one sees, and a remote
    # that stops sending is given up — with no overall limit, as a prune's fetches and its push may be large.
    full_env = {**(gitcmd.network_env() if network else os.environ), **(env or {})}
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.quotePath=false", *(gitcmd.network_options() if network else []), *args],
        capture_output=True,
        text=True,
        input=stdin,
        env=full_env,
        stdin=None if stdin is not None else subprocess.DEVNULL,
    )


def _git(repo: Path, *args: str, network: bool = False, stdin: Optional[str] = None) -> str:
    """``git`` in ``repo``; its stdout, or a :class:`PruneError` naming the command and git's own words."""
    done = _run(repo, *args, network=network, stdin=stdin)
    if done.returncode != 0:
        raise PruneError(f"{repo.name}: git {' '.join(args)} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def _refs(repo: Path, *namespaces: str) -> Dict[str, str]:
    """Every ref under ``namespaces`` and the object it names; a symbolic ref (``origin/HEAD``) is left out."""
    lines = _git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(symref)", *namespaces).splitlines()
    refs: Dict[str, str] = {}
    for line in lines:
        name, sha, *symref = line.split(" ")
        if not any(symref):
            refs[name] = sha
    return refs


def _origin_refs(repo: Path) -> Dict[str, str]:
    """origin's branches and tags (and :data:`PRUNE_REF`) as ``git ls-remote`` lists them; peeled lines left out."""
    refs: Dict[str, str] = {}
    for line in _git(repo, "ls-remote", "origin", network=True).splitlines():
        sha, name = line.split("\t")
        if name.endswith("^{}") or name == "HEAD":
            continue
        refs[name] = sha
    return refs


def _short(ref: str) -> str:
    """A ref as a person names it: ``main``, ``origin/main``, a tag's name, ``wheels-prune/map``."""
    for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref.removeprefix("refs/")


def _git_dir(repo: Path) -> Path:
    return Path(_git(repo, "rev-parse", "--absolute-git-dir").strip())


def _store_size(repo: Path) -> int:
    """The object store's size in bytes, loose and packed (``git count-objects -v``, which counts KiB)."""
    values = dict(line.split(": ") for line in _git(repo, "count-objects", "-v").splitlines())
    return (int(values["size"]) + int(values["size-pack"])) * 1024


def _tracked_changes(repo: Path) -> List[str]:
    return [line for line in _git(repo, "status", "--porcelain", "--untracked-files=no").splitlines() if line]


def _detached_head(repo: Path) -> Optional[str]:
    """HEAD's commit when it is on no branch, else None."""
    if _run(repo, "symbolic-ref", "-q", "HEAD").returncode == 0:
        return None
    return _git(repo, "rev-parse", "HEAD").strip()


def _check_repository(repo: Path, name: str) -> None:
    if _run(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise PruneError(f"{name} is in no git repository; there is no history to prune.")
    if _run(repo, "remote", "get-url", "origin").returncode != 0:
        raise PruneError(f"{name} has no remote 'origin'; a prune rewrites origin's history with this clone's.")


# --- the wheel versions -----------------------------------------------------------------------------


def _is_wheel(path: str) -> bool:
    return path.startswith(f"{WHEELS_DIR}/") and path.endswith(".whl")


def history_wheels(repo: Path) -> Set[str]:
    """Every wheel version (blob id) any commit of any ref ever held under ``wheels/``.

    Each commit's change against each parent (``-m``) and the root commits' (``--root``) name every blob a path
    took, merges included; renames are off, so a moved wheel counts on both paths.
    """
    out = _git(
        repo, "log", "--all", "--root", "-m", "--raw", "--no-abbrev", "--no-renames", "--format=", "--", WHEELS_DIR
    )
    blobs: Set[str] = set()
    for line in out.splitlines():
        if not line.startswith(":") or "\t" not in line:
            continue
        meta, path = line.split("\t", 1)
        if not _is_wheel(path):
            continue
        fields = meta.split()
        blobs.update(sha for sha in (fields[2], fields[3]) if sha.strip("0"))
    return blobs


def wheels_at(repo: Path, refs: Sequence[str]) -> Dict[str, List[str]]:
    """Each wheel version the tips of ``refs`` hold, and the refs holding it."""
    held: Dict[str, List[str]] = {}
    for ref in refs:
        for entry in _git(repo, "ls-tree", "-r", "-z", ref, "--", f"{WHEELS_DIR}/").split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            kind, sha = meta.split()[1:3]
            if kind == "blob" and _is_wheel(path):
                held.setdefault(sha, []).append(ref)
    return held


def _blob_bytes(repo: Path, blobs: Sequence[str]) -> int:
    if not blobs:
        return 0
    out = _git(repo, "cat-file", "--batch-check=%(objectsize)", stdin="".join(f"{b}\n" for b in blobs))
    return sum(int(line) for line in out.split())


# --- the rewrite ----------------------------------------------------------------------------------


def _filter_repo() -> List[str]:
    """The command that runs git-filter-repo with this interpreter — through :mod:`aisland.filter_repo`, which reads
    a config value of several lines — or a refusal naming the extra."""
    if importlib.util.find_spec("git_filter_repo") is None:
        raise PruneError(
            f"rewriting history needs git-filter-repo, which aisland's [{EXTRA}] extra installs: "
            f"uv pip install 'aisland[{EXTRA}]'"
        )
    return [sys.executable, "-m", "aisland.filter_repo"]


def _restore_command(repo: Path, bundle: Path) -> str:
    return f"git -C {repo} fetch --update-head-ok {bundle} '+refs/*:refs/*' && git -C {repo} reset --hard"


def _backup(repo: Path) -> Tuple[Path, str]:
    """Every ref and every object it reaches, bundled into ``<git dir>/wheels-prune/<stamp>.bundle``."""
    folder = _git_dir(repo) / BACKUP_DIR
    folder.mkdir(exist_ok=True)
    bundle = folder / f"{datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.bundle"
    _git(repo, "bundle", "create", str(bundle), "--all")
    return bundle, _restore_command(repo, bundle)


def strip(repo: Path, blobs: Sequence[str], refs: Sequence[str], restore: str) -> Dict[str, str]:
    """Remove ``blobs`` from every commit ``refs`` reach and return each commit that changed id, ``old -> new``.

    No commit is dropped (an emptied one stays, ``--prune-empty never``) and no message is rewritten
    (``--preserve-commit-hashes``): the result is a function of each commit alone, so two clones stripping the
    same blobs get the same ids. filter-repo's state folder is cleared first — a leftover one makes it ask
    whether this run continues the last.
    """
    state = _git_dir(repo) / "filter-repo"
    if state.is_dir():
        shutil.rmtree(state)
    with tempfile.TemporaryDirectory() as scratch:
        listing = Path(scratch) / "blobs"
        listing.write_text("".join(f"{b}\n" for b in blobs))
        done = subprocess.run(
            [
                *_filter_repo(),
                "--partial",
                "--force",
                "--quiet",
                "--strip-blobs-with-ids",
                str(listing),
                "--prune-empty",
                "never",
                "--prune-degenerate",
                "never",
                "--preserve-commit-hashes",
                "--refs",
                *refs,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    if done.returncode != 0:
        raise PruneError(
            f"{repo.name}: git-filter-repo failed: {done.stderr.strip() or done.stdout.strip()}\n"
            f"Put the refs back with: {restore}"
        )
    changed: Dict[str, str] = {}
    for line in (state / "commit-map").read_text().splitlines()[1:]:
        old, new = line.split()
        if old != new:
            changed[old] = new
    shutil.rmtree(state)
    return changed


def _shrink(repo: Path) -> None:
    """Expire every reflog but the stash's (its entries are work) and drop what nothing reaches any more."""
    # Every ref, symbolic ones too: a clone's `origin/HEAD` keeps a reflog of its own that reaches the old history.
    every = _git(repo, "for-each-ref", "--format=%(refname)").split()
    refs = [ref for ref in ("HEAD", *every) if ref != _STASH and _run(repo, "reflog", "exists", ref).returncode == 0]
    if refs:
        _git(repo, "reflog", "expire", "--expire=now", "--expire-unreachable=now", *refs)
    _git(repo, "gc", "-q", "--prune=now")


def _hold_head(repo: Path) -> List[str]:
    """A detached HEAD's commit, held by :data:`_HEAD_REF` so the rewrite carries it (filter-repo moves refs, never a
    detached HEAD); the refs to add to the rewrite — none when HEAD is on a branch."""
    detached = _detached_head(repo)
    if detached is None:
        return []
    _git(repo, "update-ref", _HEAD_REF, detached)
    return [_HEAD_REF]


def _follow_head(repo: Path, held: Sequence[str]) -> None:
    """Put the work tree on HEAD's rewritten commit: a detached HEAD goes to where :data:`_HEAD_REF` was carried; a
    branch's files are reset to its new tip (an old commit's tree may have lost a wheel). The tree held no tracked
    change — a prune refused one, a reset set it aside — so nothing of the user's is lost."""
    if held:
        _git(repo, "checkout", "-q", "--detach", _HEAD_REF)
        _git(repo, "update-ref", "-d", _HEAD_REF)
    _git(repo, "reset", "-q", "--hard", "HEAD")


# --- aisland wheels prune -----------------------------------------------------------------------------


def _fetch(repo: Path, origin: Dict[str, str]) -> None:
    """origin's branches into ``refs/remotes/origin/`` (gone ones pruned) and its tags this clone lacks."""
    _git(repo, "fetch", "-q", "--prune", "--no-tags", "origin", "+refs/heads/*:refs/remotes/origin/*", network=True)
    local_tags = _refs(repo, "refs/tags/")
    missing = [f"{ref}:{ref}" for ref in origin if ref.startswith("refs/tags/") and ref not in local_tags]
    if missing:
        _git(repo, "fetch", "-q", "--no-tags", "origin", *missing, network=True)


def _problems(repo: Path, origin: Dict[str, str]) -> List[str]:
    """What makes a rewrite of this clone lose or tangle work, each in one line."""
    name = repo.name
    problems = [f"{name} has changes no commit holds: {line.strip()}" for line in _tracked_changes(repo)]
    heads = _refs(repo, "refs/heads/")
    tags = _refs(repo, "refs/tags/")
    for ref, sha in sorted(origin.items()):
        if ref.startswith("refs/heads/"):
            branch = ref[len("refs/heads/") :]
            if ref not in heads:
                continue
            counts = _git(repo, "rev-list", "--left-right", "--count", f"{ref}...{sha}").split()
            ahead, behind = int(counts[0]), int(counts[1])
            if ahead:
                problems.append(
                    f"{name}: {branch} has {ahead} commit{'s' * (ahead != 1)} origin/{branch} lacks "
                    "— push or drop them first"
                )
            if behind:
                problems.append(
                    f"{name}: {branch} is {behind} commit{'s' * (behind != 1)} behind origin/{branch} — pull first"
                )
        elif ref.startswith("refs/tags/") and tags.get(ref, sha) != sha:
            problems.append(f"{name}: tag {_short(ref)} differs from origin's — make them one first")
    return problems


def _record(repo: Path, stripped: Sequence[str], changed: Dict[str, str], parent: Optional[str]) -> str:
    """The prune's record commit: the versions stripped and the commit map, on top of origin's earlier record."""
    tree_lines = []
    for file_name, text in (
        (STRIPPED_FILE, "".join(f"{b}\n" for b in sorted(stripped))),
        (COMMITS_FILE, "".join(f"{old} {new}\n" for old, new in sorted(changed.items()))),
    ):
        blob = _git(repo, "hash-object", "-w", "--stdin", stdin=text).strip()
        tree_lines.append(f"100644 blob {blob}\t{file_name}\n")
    tree = _git(repo, "mktree", stdin="".join(tree_lines)).strip()
    message = f"wheels prune: {len(stripped)} wheel versions stripped, {len(changed)} commits rewritten"
    args = ["commit-tree", tree, "-m", message] + (["-p", parent] if parent else [])
    return _git(repo, *args).strip()


def _push(repo: Path, updates: Dict[str, Tuple[Optional[str], str]], restore: str) -> None:
    """Push every ``ref -> (origin's value, new value)`` in one atomic push, each leased on origin's value."""
    args = ["push", "--atomic", "--porcelain", "origin"]
    for ref, (old, _new) in updates.items():
        args.append(f"--force-with-lease={ref}:{old or ''}")
    args += [f"{new}:{ref}" for ref, (_old, new) in updates.items()]
    done = _run(repo, *args, network=True)
    if done.returncode != 0:
        raise PruneError(
            f"origin refused the push, and being atomic it changed nothing there:\n"
            f"{(done.stdout + done.stderr).strip()}\n"
            f"This clone is rewritten; put it back with: {restore}"
        )


def _root_pointer(workspace_root: Path, repo: Path, changed: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """:func:`_record_root_pointer` for the project in ``repo``, when the workspace root is a repository of its own."""
    if workspace_root == repo or not (workspace_root / ".git").exists():
        return None
    return _record_root_pointer(workspace_root, repo.relative_to(workspace_root).as_posix(), changed)


def _record_root_pointer(workspace_root: Path, path: str, changed: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """Replace the commit the root records for ``path`` by its rewritten twin, committed alone; ``(old, new)``.

    Only the gitlink moves: a scratch index built from the root's HEAD takes the new id, and the root's own index
    takes it too, so whatever else is staged there stays staged and out of the commit.
    """
    if _run(workspace_root, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return None
    entry = _run(workspace_root, "ls-tree", "HEAD", "--", path).stdout.split()
    if len(entry) < 3 or entry[1] != "commit" or entry[2] not in changed:
        return None
    old, new = entry[2], changed[entry[2]]
    head = _git(workspace_root, "rev-parse", "HEAD").strip()
    with tempfile.TemporaryDirectory() as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        for args in (("read-tree", head), ("update-index", "--cacheinfo", f"160000,{new},{path}")):
            done = _run(workspace_root, *args, env=env)
            if done.returncode != 0:
                raise PruneError(f"the root: git {' '.join(args)} failed: {done.stderr.strip()}")
        tree = _run(workspace_root, "write-tree", env=env).stdout.strip()
    message = f"chore: sync submodule pointer — {path}: wheels pruned from its history"
    commit = _git(workspace_root, "commit-tree", tree, "-p", head, "-m", message).strip()
    _git(workspace_root, "update-ref", "-m", message, "HEAD", commit, head)
    _git(workspace_root, "update-index", "--cacheinfo", f"160000,{new},{path}")
    return old, new


def prune(workspace_root: Path, project_name: str, apply: bool = False) -> PruneReport:
    """``aisland wheels prune PROJECT [--apply]``: see the module's docstring."""
    target = find_target(workspace_projects(workspace_root), workspace_root, project_name)
    repo = target.path
    _check_repository(repo, target.name)
    if apply:
        _filter_repo()
    origin = _origin_refs(repo)
    _fetch(repo, origin)
    problems = _problems(repo, origin)
    # Every branch and tag of this clone and every branch of origin keeps its wheels; origin's tags are this
    # clone's, fetched above (a differing one is a problem).
    keep_refs = sorted({**_refs(repo, *_LOCAL_NAMESPACES), **_refs(repo, _ORIGIN_NAMESPACE)})
    held = wheels_at(repo, keep_refs)
    in_history = history_wheels(repo)
    stripped = sorted(in_history - set(held))
    holders = sorted({_short(ref) for refs in held.values() for ref in refs})
    report = PruneReport(
        project=target.name,
        applied=False,
        versions=len(in_history | set(held)),
        stripped=stripped,
        stripped_bytes=_blob_bytes(repo, stripped),
        kept=len(held),
        holders=holders,
        problems=problems,
    )
    if not apply or not stripped:
        return report
    if problems:
        raise PruneError("\n".join([*problems, "Nothing was changed."]))

    report.size_before = _store_size(repo)
    bundle, restore = _backup(repo)
    report.backup, report.restore = str(bundle), restore
    trees = {ref: _git(repo, "rev-parse", f"{ref}^{{tree}}").strip() for ref in keep_refs}
    held_head = _hold_head(repo)
    rewrite_refs = [*keep_refs, *([_STASH] if _STASH in _refs(repo, _STASH) else []), *held_head]
    changed = strip(repo, stripped, rewrite_refs, restore)
    moved_trees = [ref for ref in keep_refs if _git(repo, "rev-parse", f"{ref}^{{tree}}").strip() != trees[ref]]
    if moved_trees:
        raise PruneError(
            f"{target.name}: the rewrite changed the files of {', '.join(_short(r) for r in moved_trees)}; "
            f"nothing was pushed. Put this clone back with: {restore}"
        )
    _follow_head(repo, held_head)
    report.rewritten = len(changed)

    local = _refs(repo, *_LOCAL_NAMESPACES, _ORIGIN_NAMESPACE)
    updates: Dict[str, Tuple[Optional[str], str]] = {}
    for ref, sha in origin.items():
        new = local.get(f"{_ORIGIN_NAMESPACE}{ref[len('refs/heads/'):]}" if ref.startswith("refs/heads/") else ref)
        if new is not None and new != sha and ref != PRUNE_REF:
            updates[ref] = (sha, new)
    previous = origin.get(PRUNE_REF)
    if previous is not None:
        _git(repo, "fetch", "-q", "--no-tags", "origin", f"+{PRUNE_REF}:{PRUNE_REF}", network=True)
    record = _record(repo, stripped, changed, previous)
    updates[PRUNE_REF] = (previous, record)
    _push(repo, updates, restore)
    _git(repo, "update-ref", PRUNE_REF, record)
    report.pushed = sorted(_short(ref) for ref in updates)
    report.applied = True

    _shrink(repo)
    report.size_after = _store_size(repo)
    report.root_pointer = _root_pointer(workspace_root, repo, changed)
    return report


# --- aisland wheels reset -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Prune:
    """One of origin's prunes: its record commit, the versions it stripped and the commits it rewrote."""

    record: str
    stripped: List[str]
    changed: Dict[str, str]


def _prunes(repo: Path) -> List[_Prune]:
    """origin's prunes, oldest first, from the record chain fetched into :data:`PRUNE_REF`."""
    prunes = []
    for record in _git(repo, "rev-list", "--reverse", PRUNE_REF).split():
        stripped = _git(repo, "show", f"{record}:{STRIPPED_FILE}").split()
        pairs = [line.split() for line in _git(repo, "show", f"{record}:{COMMITS_FILE}").splitlines() if line]
        prunes.append(_Prune(record, stripped, {old: new for old, new in pairs}))
    return prunes


def _compose(prunes: Sequence[_Prune]) -> Dict[str, str]:
    """Each commit any prune rewrote and the id it has after the last prune."""
    composed: Dict[str, str] = {}
    for one in prunes:
        composed = {old: one.changed.get(new, new) for old, new in composed.items()}
        composed.update({old: new for old, new in one.changed.items() if old not in composed})
    return composed


def _reachable(repo: Path, refs: Sequence[str]) -> Set[str]:
    return set(_git(repo, "rev-list", *refs).split()) if refs else set()


def _set_aside(repo: Path) -> List[str]:
    """Hold the changes no commit holds in :data:`_SET_ASIDE_REF` and clear them from the work tree (untracked files
    stay); their ``git status`` lines. Staged changes come back staged (:func:`_put_back`)."""
    changes = [line.strip() for line in _tracked_changes(repo)]
    if changes:
        commit = _git(repo, "stash", "create", "aisland wheels reset: changes set aside during the rewrite").strip()
        _git(repo, "update-ref", _SET_ASIDE_REF, commit)
        _git(repo, "reset", "-q", "--hard", "HEAD")
    return changes


def _put_back_command(repo: Path) -> str:
    return f"git -C {repo} stash apply --index {_SET_ASIDE_REF} && git -C {repo} update-ref -d {_SET_ASIDE_REF}"


def _put_back(repo: Path, name: str) -> None:
    """The changes :func:`_set_aside` held, applied on the rewritten HEAD, and the ref removed."""
    if _run(repo, "stash", "apply", "-q", "--index", _SET_ASIDE_REF).returncode != 0:
        raise PruneError(
            f"{name} is on origin's history, but its uncommitted changes did not go back on. "
            f"They are kept; put them back with: {_put_back_command(repo)}"
        )
    _git(repo, "update-ref", "-d", _SET_ASIDE_REF)


def reset(workspace_root: Path, project_name: str) -> ResetReport:
    """``aisland wheels reset PROJECT``: see the module's docstring."""
    target = find_target(workspace_projects(workspace_root), workspace_root, project_name)
    repo = target.path
    _check_repository(repo, target.name)
    report = ResetReport(project=target.name, prunes=0)
    origin = _origin_refs(repo)
    if PRUNE_REF not in origin:
        return report
    _git(repo, "fetch", "-q", "--no-tags", "origin", f"+{PRUNE_REF}:{PRUNE_REF}", network=True)
    prunes = _prunes(repo)
    expected = _compose(prunes)

    reachable = _reachable(repo, [*_refs(repo, *_LOCAL_NAMESPACES, _STASH), "HEAD"])
    if not any(reachable & set(one.changed) for one in prunes):
        _fetch(repo, origin)
        report.root_pointer = _root_pointer(workspace_root, repo, expected)
        return report

    _filter_repo()
    report.set_aside = _set_aside(repo)
    try:
        _rewrite(repo, target.name, origin, prunes, expected, report)
    except PruneError as error:
        if report.set_aside:
            raise PruneError(
                f"{error.message}\nYour uncommitted changes are kept; once the refs are back, put them back with: "
                f"{_put_back_command(repo)}"
            ) from error
        raise
    if report.set_aside:
        _put_back(repo, target.name)
    report.root_pointer = _root_pointer(workspace_root, repo, expected)
    return report


def _rewrite(
    repo: Path,
    name: str,
    origin: Dict[str, str],
    prunes: Sequence[_Prune],
    expected: Dict[str, str],
    report: ResetReport,
) -> None:
    """The reset's rewrite of a clone holding no uncommitted change: the backup, each prune it still reaches, the
    check that every branch that was origin's landed where origin's record says, and the shrink."""
    before = _refs(repo, *_LOCAL_NAMESPACES, _STASH)
    local_refs = sorted(before)
    report.size_before = _store_size(repo)
    bundle, restore = _backup(repo)
    report.backup, report.restore = str(bundle), restore
    held = [*_hold_head(repo), *([_SET_ASIDE_REF] if report.set_aside else [])]
    for one in prunes:
        # A prune this clone already took (or never reached) has no old commit left to rewrite.
        if not _reachable(repo, [*local_refs, *held]) & set(one.changed):
            continue
        strip(repo, one.stripped, [*local_refs, *held], restore)
        report.prunes += 1
    _follow_head(repo, [ref for ref in held if ref == _HEAD_REF])

    _fetch(repo, origin)
    after = _refs(repo, *_LOCAL_NAMESPACES, _STASH)
    wrong = []
    for ref, old in sorted(before.items()):
        new = after.get(ref, old)
        if new != old:
            report.moved.append((_short(ref), old, new))
        if ref.startswith("refs/heads/") and old in expected and new != expected[old]:
            wrong.append(f"{_short(ref)} is {new[:7]} here, origin's record says {expected[old][:7]}")
    if wrong:
        raise PruneError(
            "\n".join([f"{name}: the rewrite here does not match origin's:", *wrong, f"Put it back with: {restore}"])
        )
    for ref in sorted(after):
        if not ref.startswith("refs/heads/"):
            continue
        tracking = f"{_ORIGIN_NAMESPACE}{ref[len('refs/heads/'):]}"
        if _run(repo, "rev-parse", "--verify", "-q", tracking).returncode == 0:
            ahead = int(_git(repo, "rev-list", "--count", f"{tracking}..{ref}").strip())
            if ahead:
                report.ahead[_short(ref)] = ahead
    report.tags_differing = sorted(
        _short(ref) for ref, sha in origin.items() if ref.startswith("refs/tags/") and after.get(ref, sha) != sha
    )
    _shrink(repo)
    report.size_after = _store_size(repo)


# --- the reports --------------------------------------------------------------------------------------


def render_prune(report: PruneReport) -> str:
    """The report a person reads; :meth:`PruneReport.to_dict` is the one a program reads."""
    name = report.project
    lines: List[str] = []
    if not report.stripped:
        lines.append(
            f"{name}: every one of its {report.versions} wheel versions is held by a branch or a tag — nothing to prune"
        )
        return "\n".join(lines)
    count = f"{len(report.stripped)} of {report.versions} wheel versions"
    if not report.applied:
        lines.append(f"{name}: {count} are held by no branch or tag ({human_size(report.stripped_bytes)}) — they go")
        lines.append(f"  kept: {report.kept}, held by {', '.join(report.holders) or 'nothing'}")
        if report.problems:
            lines.append("--apply would stop:")
            lines += [f"  {problem}" for problem in report.problems]
        else:
            lines.append(f"aisland wheels prune {name} --apply   rewrites history and force-pushes origin")
        return "\n".join(lines)
    lines.append(
        f"{name}: {count} stripped ({human_size(report.stripped_bytes)}), {report.rewritten} commits rewritten"
    )
    lines.append(f"  kept: {report.kept}, held by {', '.join(report.holders)}")
    lines.append(f"  pushed to origin: {', '.join(report.pushed)}")
    if report.size_before is not None and report.size_after is not None:
        lines.append(f"  this clone: {human_size(report.size_before)} → {human_size(report.size_after)}")
    if report.root_pointer:
        lines.append(_root_pointer_line(name, report.root_pointer))
    lines.append(f"  backup: {report.backup}")
    lines.append(f"Every other clone of {name}: aisland wheels reset {name}, before its next pull")
    return "\n".join(lines)


def _root_pointer_line(name: str, root_pointer: Tuple[str, str]) -> str:
    old, new = root_pointer
    return f"  the root records {name} at {new[:7]} (was {old[:7]}), committed; push the root as usual"


def render_reset(report: ResetReport) -> str:
    """The report a person reads; :meth:`ResetReport.to_dict` is the one a program reads."""
    name = report.project
    if not report.prunes:
        lines = [f"{name}: already on origin's history — nothing to reset"]
        if report.root_pointer:
            lines.append(_root_pointer_line(name, report.root_pointer))
        return "\n".join(lines)
    lines = [f"{name}: {report.prunes} prune{'s' * (report.prunes != 1)} taken"]
    moved = ", ".join(f"{ref} {old[:7]} → {new[:7]}" for ref, old, new in report.moved)
    lines.append(f"  moved: {moved or 'nothing'}")
    for branch, count in sorted(report.ahead.items()):
        lines.append(f"  {branch}: {count} commit{'s' * (count != 1)} origin lacks, kept on top of origin's")
    if report.tags_differing:
        lines.append(f"  tags that differ from origin's, left as they are: {', '.join(report.tags_differing)}")
    if report.set_aside:
        paths = ", ".join(line.split(None, 1)[1] for line in report.set_aside)
        lines.append(f"  uncommitted changes set aside and put back: {paths}")
    if report.size_before is not None and report.size_after is not None:
        lines.append(f"  this clone: {human_size(report.size_before)} → {human_size(report.size_after)}")
    if report.root_pointer:
        lines.append(_root_pointer_line(name, report.root_pointer))
    lines.append(f"  backup: {report.backup}")
    return "\n".join(lines)
