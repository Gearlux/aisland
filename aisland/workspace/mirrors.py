"""Per-project push mirrors (``aisland workspace configure mirror``, ``aisland workspace mirror sync``).

A submodule's PRIMARY is the remote ``.gitmodules`` names — the one every machine clones from,
pulls from and pushes to (``origin``). A MIRROR is a second host that receives a copy of the
primary and nothing else: ``aisland workspace mirror sync`` pushes the primary's ``dev/main``, ``main``, every
other branch the local checkout has, and the tags to it, and nothing is ever pulled from it. The use is a project that must be visible on two
hosts (a GitHub organisation and a Bitbucket server, say) while one of them stays the place work
happens.

The copy is deliberately one-way and conservative — the cases a sync meets, and its answer:

=================================================== ==========================================
the mirror's branch …                               sync
=================================================== ==========================================
does not exist                                      pushed (new branch)
is the primary's                                    in sync
is behind the primary                               pushed, fast-forward
has commits the primary does not (moved or diverged) REFUSED, naming the commit and the merge
exists only on the mirror (``feature``)             left alone — never deleted
=================================================== ==========================================

Tags follow the same rule: a missing tag is pushed, a tag of the same name pointing elsewhere is
REFUSED (never overwritten), a mirror-only tag is left alone. Nothing is forced, so a mirror that
someone pushed to directly can never lose that work to a sync.

Which branches is the local checkout's choice (``dev/main`` and ``main`` always, then every branch
under ``refs/heads/``); what is pushed for each is the PRIMARY's state (``git ls-remote origin``):
a commit you have not pushed to the primary does not reach the mirror either, so the two hosts
can only ever differ by the mirror being behind. A local branch the primary has not got is
reported ``not on the primary`` and not pushed; a branch only on the primary is not copied.

Two configuration layers, the same split as :mod:`aisland.workspace.sources`:

* **committed** — the project's ``mirror`` in its table of ``<workspace>/workspace.toml``, shared between
  machines;
* **per-machine** — ``git config --local aisland.mirror.<project>.mirror <url>`` in the workspace root, which
  replaces the committed URL on this machine only.

The effective URL is kept as a remote named ``mirror`` inside the submodule's checkout, marked
``remote.mirror.aislandManaged`` so that ``aisland setup`` and ``… configure mirror unset`` only ever
remove a remote aisland created; ``remote.mirror.tagOpt = --no-tags`` keeps a fetch from the
mirror from importing its tags. Every network call runs with ``GIT_TERMINAL_PROMPT=0``: a host
whose credentials are missing fails the sync with git's message instead of stalling it on a
password prompt.

Only submodules are mirrored; the root repository is not.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

from aisland import console, failures, gitcmd, machine_config
from aisland.errors import AislandError
from aisland.workspace import file as workspace_file
from aisland.workspace import prune_record, root_project
from aisland.workspace.projects import get_workspace_root, submodule_active_map
from aisland.workspace.prune_record import PRUNE_REF, Prune

#: The project's setting this module owns in its ``[project."<name>"]`` table of ``workspace.toml``, and the last part
#: of this machine's ``aisland.mirror.<project>.mirror``.
MIRROR_KEY = "mirror"

#: The name of the remote that carries the mirror inside a submodule's checkout.
MIRROR_REMOTE = "mirror"

#: The remote that carries the primary (``git submodule`` names it so).
PRIMARY_REMOTE = "origin"

#: The branches a sync always copies — the workspace's two long-lived ones, checked out here or not. Every other
#: local branch follows them (``_branches``).
MIRRORED_BRANCHES: Tuple[str, ...] = ("dev/main", "main")

#: Marks a ``mirror`` remote as aisland's own, so nothing else's is ever removed.
_MANAGED_KEY = f"remote.{MIRROR_REMOTE}.aislandManaged"

#: What happened to one branch (or to the tags) in a sync. ``to push`` is a dry run's answer.
RefOutcome = Literal["pushed", "to push", "in sync", "refused", "absent"]

#: The project's overall result: ``refused`` when any ref was, ``failed`` when git was.
SyncState = Literal["synced", "refused", "skipped", "failed"]


@dataclass(frozen=True)
class RefResult:
    """One branch's (or the tags') outcome.

    Args:
        ref: The branch name, or ``"tags"``.
        outcome: What the sync did, or would do on a dry run.
        count: Commits pushed (a branch behind), tags pushed — or, refused, the tags that differ; ``0`` for a new
            branch.
        detail: The tags concerned, or the refusal and its remedy.
    """

    ref: str
    outcome: RefOutcome
    count: int = 0
    detail: str = ""


@dataclass(frozen=True)
class ProjectSync:
    """One project's sync — JSON-shaped, so a tool can report it as well as the CLI."""

    project: str
    url: str
    state: SyncState
    refs: Tuple[RefResult, ...] = field(default_factory=tuple)
    note: str = ""


# --- committed layer: each project's `mirror` in <workspace>/workspace.toml -----------------


def read_committed_mirrors(workspace_root: Path) -> Dict[str, str]:
    """``{project: url}`` from ``workspace.toml``; a mirror that is no text is warned about, naming its line."""
    result: Dict[str, str] = {}
    for name, project in workspace_file.load(workspace_root).projects.items():
        url = project.others.get(MIRROR_KEY)
        if url is None:
            continue
        if not isinstance(url, str) or not url.strip():
            failures.warn(f"  - Warning: {project.where}: project {name!r} has a mirror that is no address; ignored")
            continue
        result[name] = url.strip()
    return result


def write_committed_mirrors(workspace_root: Path, mirrors: Dict[str, str]) -> Path:
    """Set each project's ``mirror`` in ``workspace.toml`` to ``mirrors``, leaving every other setting alone."""
    values = {project: {MIRROR_KEY: url} for project, url in mirrors.items()}
    return workspace_file.set_settings(workspace_root, (MIRROR_KEY,), values)


# --- per-machine layer: .git/config aisland.mirror.<project>.mirror ---------------------------


def _override_key(project: str) -> str:
    return f"aisland.mirror.{project}.{MIRROR_KEY}"


def local_override(workspace_root: Path, project: str) -> Optional[str]:
    """This machine's mirror URL for ``project``, or ``None`` when it follows the committed one."""
    return machine_config.get(workspace_root, _override_key(project)) or None


def set_local_override(workspace_root: Path, project: str, url: Optional[str]) -> None:
    """Record (or clear, when ``url`` is ``None``) this machine's URL. Never committed."""
    machine_config.put(workspace_root, _override_key(project), url)


def _overridden_projects(workspace_root: Path) -> List[str]:
    prefix, suffix = "aisland.mirror.", f".{MIRROR_KEY}"
    keys = list(machine_config.get_all(workspace_root, r"^aisland\.mirror\."))
    return sorted(key[len(prefix) : -len(suffix)] for key in keys if key.startswith(prefix) and key.endswith(suffix))


def effective_mirror(workspace_root: Path, project: str, committed: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The URL that governs THIS machine: the override, else the committed one, else ``None``."""
    override = local_override(workspace_root, project)
    if override is not None:
        return override
    if committed is None:
        committed = read_committed_mirrors(workspace_root)
    return committed.get(project)


def mirrored_projects(workspace_root: Path) -> List[str]:
    """Every project with a mirror on this machine, in name order."""
    committed = read_committed_mirrors(workspace_root)
    return sorted(set(committed) | set(_overridden_projects(workspace_root)))


# --- git plumbing --------------------------------------------------------------------------


def _git(repo: Path, *args: str, network: bool = False) -> "subprocess.CompletedProcess[str]":
    """``gitcmd.git`` in ``repo``; a network call with no overall limit — a first push to a mirror may be large — but
    given up on a remote that stops sending."""
    return gitcmd.git(repo, *args, network=network, timeout=gitcmd.NO_LIMIT if network else None)


def _git_out(repo: Path, *args: str) -> str:
    """git's stdout, stripped; empty when the command failed (an unset key, a missing ref)."""
    return gitcmd.read(repo, *args) or ""


def _last_line(res: "subprocess.CompletedProcess[str]") -> str:
    lines = [line for line in (res.stderr or res.stdout).splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"git exited {res.returncode}"


def _submodules(workspace_root: Path) -> Dict[str, str]:
    """``{name: relative path}`` for every submodule ``.gitmodules`` declares — in a workspace with a root project,
    which has none, every project ``workspace.toml`` lists but the root project: each is a clone of its own."""
    if root_project.rooted(workspace_root):
        own = root_project.name(workspace_root)
        rows = workspace_file.load(workspace_root).projects.values()
        return {row.name: row.path for row in rows if row.path != own}
    return {name: path for name, path, _active in submodule_active_map(workspace_root)}


def _has_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def primary_url(workspace_root: Path, project: str) -> str:
    """The submodule's remote as ``.gitmodules`` declares it — in a workspace with a root project, the url its row of
    ``workspace.toml`` gives — empty when undeclared."""
    if root_project.rooted(workspace_root):
        row = workspace_file.load(workspace_root).projects.get(project)
        return row.url or "" if row is not None else ""
    return _git_out(workspace_root, "config", "-f", ".gitmodules", "--get", f"submodule.{project}.url")


def _same_repo(a: str, b: str) -> bool:
    """Whether two remote spellings name one repository (``.git`` and a trailing ``/`` aside)."""

    def norm(url: str) -> str:
        url = url.strip().rstrip("/")
        return url[: -len(".git")] if url.endswith(".git") else url

    return bool(a) and bool(b) and norm(a) == norm(b)


_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)")


def host_of(url: str) -> str:
    """The host a remote lives on, for messages: ``github.com``, or ``a local path``."""
    scp = _SCP_LIKE.match(url)
    if scp and "://" not in url:
        return scp.group(1)
    parsed = urlparse(url)
    if parsed.scheme and parsed.hostname:
        return parsed.hostname
    return "a local path"


# --- the mirror remote inside a checkout -----------------------------------------------------


def _remote_state(path: Path) -> Tuple[str, bool]:
    """``(url, managed)`` of the checkout's ``mirror`` remote; ``("", False)`` when absent."""
    url = _git_out(path, "config", "--get", f"remote.{MIRROR_REMOTE}.url")
    managed = _git_out(path, "config", "--get", _MANAGED_KEY) == "true"
    return url, managed


def _foreign_remote(path: Path, url: str) -> Optional[str]:
    """The URL of a ``mirror`` remote aisland did not create and must not take over, if any."""
    current, managed = _remote_state(path)
    if current and not managed and not _same_repo(current, url):
        return current
    return None


def _ensure_remote(path: Path, url: str) -> None:
    current, _managed = _remote_state(path)
    if not current:
        _git(path, "remote", "add", "--", MIRROR_REMOTE, url)
    elif current != url:
        _git(path, "remote", "set-url", "--", MIRROR_REMOTE, url)
    _git(path, "config", f"remote.{MIRROR_REMOTE}.tagOpt", "--no-tags")
    _git(path, "config", _MANAGED_KEY, "true")


def _drop_remote(path: Path) -> bool:
    """Remove the ``mirror`` remote when aisland created it. Returns whether one was removed."""
    current, managed = _remote_state(path)
    if current and managed:
        _git(path, "remote", "remove", MIRROR_REMOTE)
        return True
    return False


def apply_mirror(workspace_root: Path, project: str, committed: Optional[Dict[str, str]] = None) -> None:
    """Make the checkout's ``mirror`` remote match the effective URL (or remove aisland's own)."""
    rel = _submodules(workspace_root).get(project, project)
    path = workspace_root / rel
    if not _has_checkout(path):
        return
    url = effective_mirror(workspace_root, project, committed)
    if url is None:
        _drop_remote(path)
        return
    foreign = _foreign_remote(path, url)
    if foreign:
        failures.warn(f"  - Warning: {project} already has a remote named `{MIRROR_REMOTE}` ({foreign}); left as it is")
        return
    _ensure_remote(path, url)


def apply_all(workspace_root: Path) -> None:
    """``aisland setup``'s step: every checked-out submodule's ``mirror`` remote follows the config."""
    committed = read_committed_mirrors(workspace_root)
    for project in _submodules(workspace_root):
        apply_mirror(workspace_root, project, committed)


# --- set / unset -----------------------------------------------------------------------------


def set_mirror(
    project: str,
    url: str,
    machine_only: bool = False,
    workspace_root: Optional[Path] = None,
) -> int:
    """Record ``url`` as ``project``'s mirror and add the remote. Returns an exit code.

    Refused, with nothing recorded: a name that is not a submodule, the primary itself, and a
    checkout that already has a ``mirror`` remote of its own (taking it over would point that
    remote somewhere its owner did not choose).
    """
    root = workspace_root or get_workspace_root()
    url = url.strip()
    subs = _submodules(root)
    if project not in subs:
        console.echo(
            f"Error: '{project}' is not a submodule of this workspace; a mirror is set per submodule.", err=True
        )
        return 1
    if not url:
        console.echo("Error: the mirror URL is empty.", err=True)
        return 1
    primary = primary_url(root, project)
    if _same_repo(url, primary):
        console.echo(f"Error: {url} is {project}'s primary (.gitmodules); a mirror is a second host.", err=True)
        return 1
    rel = subs[project]
    path = root / rel
    if _has_checkout(path):
        foreign = _foreign_remote(path, url)
        if foreign:
            console.echo(
                f"Error: {project} already has a remote named `{MIRROR_REMOTE}` ({foreign}) that aisland did not "
                f"create. Rename it first: git -C {rel} remote rename {MIRROR_REMOTE} <another-name>",
                err=True,
            )
            return 1

    if machine_only:
        set_local_override(root, project, url)
        where = f"recorded for this machine only (.git/config {_override_key(project)})"
    else:
        committed = read_committed_mirrors(root)
        committed[project] = url
        write_committed_mirrors(root, committed)
        where = f"recorded in {workspace_file.FILE_NAME} (commit it)"
    apply_mirror(root, project)

    console.echo(f"==> {project}: mirror → {url}")
    remote = f"remote `{MIRROR_REMOTE}` added to {rel}" if _has_checkout(path) else "no checkout yet"
    console.echo(f"    {where}; {remote}")
    branches = ", ".join(MIRRORED_BRANCHES)
    console.echo(
        f"    Note: each sync copies {project}'s {branches}, every other branch checked out here and tags from {host_of(primary)} to {host_of(url)}"
        " — make sure its code may live there."
    )
    console.echo(f"    Push to it with: aisland workspace mirror sync {project}")
    return 0


def unset_mirror(project: str, machine_only: bool = False, workspace_root: Optional[Path] = None) -> int:
    """Drop ``project``'s mirror (or only this machine's override) and re-apply the remote."""
    root = workspace_root or get_workspace_root()
    if machine_only:
        set_local_override(root, project, None)
    else:
        committed = read_committed_mirrors(root)
        if committed.pop(project, None) is not None:
            write_committed_mirrors(root, committed)
    apply_mirror(root, project)
    url = effective_mirror(root, project)
    if url:
        console.echo(f"==> {project}: mirror → {url} (the committed one)")
    else:
        console.echo(f"==> {project}: no mirror")
    return 0


# --- sync ------------------------------------------------------------------------------------


def _ls_remote(path: Path, remote: str) -> Tuple[Optional[Dict[str, str]], Dict[str, str], str]:
    """``({ref: sha}, {tag: its commit}, "")`` for a remote's branches, tags and wheels-prune record
    (:data:`PRUNE_REF`) — an annotated tag's commit is the one git lists it peeled to, a plain tag's its own — or
    ``(None, {}, git's message)``."""
    # The patterns leave out every other namespace a host keeps (a pull request's refs/pull/*); a peeled tag is kept.
    res = _git(path, "ls-remote", remote, "refs/heads/*", "refs/tags/*", "refs/wheels-prune/*", network=True)
    if res.returncode != 0:
        return None, {}, _last_line(res)
    refs: Dict[str, str] = {}
    peeled: Dict[str, str] = {}
    for line in res.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/tags/") and ref.endswith("^{}"):
            peeled[ref[: -len("^{}")]] = sha
        elif ref.startswith(("refs/heads/", "refs/tags/")) or ref == PRUNE_REF:
            refs[ref] = sha
    commits = {ref: peeled.get(ref, sha) for ref, sha in refs.items() if ref.startswith("refs/tags/")}
    return refs, commits, ""


#: A branch or tag of the mirror still on the history the primary had before ``aisland wheels prune`` rewrote it.
_PRUNED = "the primary's history after a wheels prune"


def _lease(ref: str, expected: Optional[str]) -> str:
    """The push option that lets ``ref`` move only from ``expected`` (absent: only while the mirror has none)."""
    return f"--force-with-lease={ref}:{expected or ''}"


def _missing_objects(path: Path, shas: List[str]) -> List[str]:
    return [sha for sha in shas if _git(path, "cat-file", "-e", sha).returncode != 0]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _branches(path: Path) -> List[str]:
    """The branches a sync copies: ``MIRRORED_BRANCHES``, then every other local branch by name."""
    listing = _git_out(path, "for-each-ref", "--format=%(refname)", "refs/heads/")
    local = {ref[len("refs/heads/") :] for ref in listing.splitlines() if ref}
    return list(MIRRORED_BRANCHES) + sorted(local - set(MIRRORED_BRANCHES))


def _branch_result(
    path: Path,
    rel: str,
    branch: str,
    primary: Optional[str],
    mirror: Optional[str],
    push: bool,
    rewritten: Dict[str, str],
) -> Tuple[RefResult, Optional[str], Optional[str]]:
    """The branch's outcome and, when it is to be pushed, its refspec and the lease a rewrite needs.

    A mirror's branch at a commit a wheels prune rewrote (``rewritten``: old -> new, from the primary's record) is
    the primary's own history before the prune, not work of the mirror's: it receives the primary's commit — a
    descendant of the rewritten twin — leased on the commit the mirror has, so a push someone made there meanwhile
    refuses it. Every other branch the mirror moved stays refused."""
    pushed: RefOutcome = "pushed" if push else "to push"
    if primary is None:
        return RefResult(branch, "absent", detail="not on the primary"), None, None
    ref = f"refs/heads/{branch}"
    spec = f"{primary}:{ref}"
    if mirror is None:
        return RefResult(branch, pushed, detail="new branch"), spec, None
    if mirror == primary:
        return RefResult(branch, "in sync"), None, None
    twin = rewritten.get(mirror)
    if twin is not None:
        if twin == primary or _git(path, "merge-base", "--is-ancestor", twin, primary).returncode == 0:
            return RefResult(branch, pushed, detail=_PRUNED), spec, _lease(ref, mirror)
        detail = (
            f"the mirror is on the history before a wheels prune ({mirror[:7]}), and the primary's {branch} no longer "
            f"holds its rewritten commit {twin[:7]}\nmake origin's {branch} hold it again, then sync"
        )
        return RefResult(branch, "refused", detail=detail), None, None
    ahead_commits = _git_out(path, "rev-list", f"{primary}..{mirror}").split()
    ahead = len(ahead_commits)
    old = next((commit for commit in ahead_commits if commit in rewritten), None)
    if old is not None:
        # Commits pushed to the mirror on top of the history before a prune: a merge would bring the stripped wheels
        # back, so the remedy takes only those commits onto the primary's rewritten branch.
        own = ahead_commits.index(old)
        detail = (
            f"the mirror has {_plural(own, 'commit')} on top of the history before a wheels prune\n"
            f"put them on the primary's {branch} first: git -C {rel} fetch {MIRROR_REMOTE} && "
            f"git -C {rel} cherry-pick {old[:12]}..{MIRROR_REMOTE}/{branch}"
        )
        return RefResult(branch, "refused", detail=detail), None, None
    if ahead:
        newest = _git_out(path, "log", "-1", '--format=%h "%s"', mirror)
        detail = (
            f"the mirror has {_plural(ahead, 'commit')} the primary does not ({newest})\n"
            f"merge it first: git -C {rel} fetch {MIRROR_REMOTE} && git -C {rel} merge {MIRROR_REMOTE}/{branch}"
        )
        return RefResult(branch, "refused", detail=detail), None, None
    behind = int(_git_out(path, "rev-list", "--count", f"{mirror}..{primary}") or "0")
    return RefResult(branch, pushed, count=behind), spec, None


def _tags_result(
    primary: Dict[str, str],
    mirror: Dict[str, str],
    push: bool,
    rewritten: Dict[str, str],
    commits: Tuple[Dict[str, str], Dict[str, str]] = ({}, {}),
) -> Tuple[RefResult, List[str], List[str]]:
    """The tags' outcome, the refspecs of the tags to push and the leases of those a wheels prune moved.

    A tag the mirror has on a commit a prune rewrote into the commit of the primary's tag (``rewritten``; each tag's
    commit in ``commits``, the primary's and the mirror's) is that tag before the prune: it is moved, leased on the
    mirror's value. Every other tag that differs is never overwritten."""
    primary_commits, mirror_commits = commits
    missing = sorted(tag for tag in primary if tag not in mirror)
    moved = sorted(
        tag
        for tag in primary
        if tag in mirror
        and mirror[tag] != primary[tag]
        and rewritten.get(mirror_commits.get(tag, mirror[tag])) == primary_commits.get(tag, primary[tag])
    )
    differ = sorted(tag for tag in primary if tag in mirror and mirror[tag] != primary[tag] and tag not in moved)
    specs = [f"{primary[tag]}:{tag}" for tag in missing + moved]
    leases = [_lease(tag, mirror[tag]) for tag in moved]
    names = [tag[len("refs/tags/") :] for tag in missing + moved]
    if differ:
        shown = ", ".join(tag[len("refs/tags/") :] for tag in differ)
        detail = f"{shown} differ{'s' if len(differ) == 1 else ''} on the mirror; a tag is never overwritten"
        if names:
            detail += f" (pushed {', '.join(names)})" if push else f" ({len(names)} to push)"
        return RefResult("tags", "refused", count=len(differ), detail=detail), specs, leases
    if names:
        shown = ", ".join(names) + (f" ({_PRUNED})" if moved else "")
        return RefResult("tags", "pushed" if push else "to push", count=len(names), detail=shown), specs, leases
    return RefResult("tags", "in sync"), specs, leases


def _record_result(
    primary: Optional[str], mirror: Optional[str], chain: List[Prune], push: bool
) -> Tuple[Optional[RefResult], Optional[str], Optional[str]]:
    """The wheels-prune record (:data:`PRUNE_REF`): the mirror gets the primary's, so a clone of the mirror can follow
    the prunes with ``aisland wheels reset``. A chain only grows, so the mirror's must be one of the primary's records;
    a record the primary's chain does not hold is refused."""
    if primary is None or primary == mirror:
        return None, None, None
    if mirror is not None and mirror not in {one.record for one in chain}:
        detail = "the mirror's wheels-prune record is none of the primary's; it is never overwritten"
        return RefResult("prune record", "refused", detail=detail), None, None
    pushed: RefOutcome = "pushed" if push else "to push"
    return RefResult("prune record", pushed, detail=_PRUNED), f"{primary}:{PRUNE_REF}", _lease(PRUNE_REF, mirror)


def sync_project(workspace_root: Path, project: str, push: bool = True) -> ProjectSync:
    """Copy the primary's branches and tags of ``project`` to its mirror (``push=False``: plan only)."""
    url = effective_mirror(workspace_root, project) or ""
    rel = _submodules(workspace_root).get(project, project)
    path = workspace_root / rel
    if not url:
        return ProjectSync(project, url, "skipped", note="no mirror configured")
    if not _has_checkout(path):
        return ProjectSync(project, url, "skipped", note="no checkout here (its source is github or pypi)")
    foreign = _foreign_remote(path, url)
    if foreign:
        return ProjectSync(project, url, "failed", note=f"a remote `{MIRROR_REMOTE}` aisland did not create: {foreign}")
    _ensure_remote(path, url)

    primary_refs, primary_commits, error = _ls_remote(path, PRIMARY_REMOTE)
    if primary_refs is None:
        return ProjectSync(project, url, "failed", note=f"the primary: {error}")
    mirror_refs, mirror_commits, error = _ls_remote(path, MIRROR_REMOTE)
    if mirror_refs is None:
        return ProjectSync(project, url, "failed", note=f"the mirror: {error}")

    # The objects the comparison and the push need, fetched by id: the primary's tips, tags and wheels-prune record
    # (a local checkout may lack a tag or a commit another machine pushed).
    missing = _missing_objects(path, list(primary_refs.values()))
    if missing:
        res = _git(path, "fetch", "--quiet", "--no-tags", PRIMARY_REMOTE, *missing, network=True)
        if res.returncode != 0:
            return ProjectSync(project, url, "failed", note=f"the primary: {_last_line(res)}")
    # The primary's prunes: a mirror's ref at a commit one rewrote is the primary's old history, sent the new one.
    try:
        chain = prune_record.prunes(path, primary_refs[PRUNE_REF]) if PRUNE_REF in primary_refs else []
    except AislandError as unreadable:
        return ProjectSync(project, url, "failed", note=f"the primary's wheels-prune record: {unreadable.message}")
    rewritten = prune_record.compose(chain)
    # The mirror's branches a sync compares into refs/remotes/mirror/*, which the refusal's merge command names — but
    # never one on the history a prune rewrote, which would bring the stripped wheels back into this clone.
    branches = _branches(path)
    compared = [
        f"+refs/heads/{branch}:refs/remotes/{MIRROR_REMOTE}/{branch}"
        for branch in branches
        if f"refs/heads/{branch}" in mirror_refs and mirror_refs[f"refs/heads/{branch}"] not in rewritten
    ]
    if compared:
        res = _git(path, "fetch", "--quiet", "--no-tags", MIRROR_REMOTE, *compared, network=True)
        if res.returncode != 0:
            return ProjectSync(project, url, "failed", note=f"the mirror: {_last_line(res)}")

    results: List[RefResult] = []
    specs: List[str] = []
    leases: List[str] = []
    for branch in branches:
        ref = f"refs/heads/{branch}"
        result, spec, lease = _branch_result(
            path, rel, branch, primary_refs.get(ref), mirror_refs.get(ref), push, rewritten
        )
        results.append(result)
        specs += [spec] if spec else []
        leases += [lease] if lease else []
    primary_tags = {ref: sha for ref, sha in primary_refs.items() if ref.startswith("refs/tags/")}
    mirror_tags = {ref: sha for ref, sha in mirror_refs.items() if ref.startswith("refs/tags/")}
    tags, tag_specs, tag_leases = _tags_result(
        primary_tags, mirror_tags, push, rewritten, (primary_commits, mirror_commits)
    )
    results.append(tags)
    specs.extend(tag_specs)
    leases.extend(tag_leases)
    record, record_spec, record_lease = _record_result(
        primary_refs.get(PRUNE_REF), mirror_refs.get(PRUNE_REF), chain, push
    )
    if record is not None:
        results.append(record)
        specs += [record_spec] if record_spec else []
        leases += [record_lease] if record_lease else []

    if push and specs:
        # A lease is no `+`: a `+` refspec overrides it. Only what a prune rewrote carries one.
        res = _git(path, "push", "--quiet", *leases, MIRROR_REMOTE, *specs, network=True)
        if res.returncode != 0:
            return ProjectSync(project, url, "failed", tuple(results), note=f"the push: {_last_line(res)}")
    state: SyncState = "refused" if any(r.outcome == "refused" for r in results) else "synced"
    return ProjectSync(project, url, state, tuple(results))


def sync(project: Optional[str] = None, workspace_root: Optional[Path] = None, push: bool = True) -> List[ProjectSync]:
    """Sync one project, or every project with a mirror. A project without one is never contacted."""
    root = workspace_root or get_workspace_root()
    projects = [project] if project else mirrored_projects(root)
    return [sync_project(root, name, push=push) for name in projects]


# --- rendering (the CLI's) -------------------------------------------------------------------


def _sync_line(result: RefResult, width: int = 10) -> str:
    """One row of ``aisland workspace mirror sync``: ``dev/main  pushed (3 commits)``, the name padded to ``width``."""
    if result.outcome == "refused":
        first, *rest = result.detail.splitlines()
        text = f"REFUSED: {first}" + "".join(f"\n{'':{width + 4}}{line}" for line in rest)
    elif result.ref == "tags" and result.outcome in ("pushed", "to push"):
        text = f"{result.outcome} {result.detail}"
    elif result.outcome in ("pushed", "to push"):
        how = result.detail if result.detail in ("new branch", _PRUNED) else _plural(result.count, "commit")
        text = f"{result.outcome} ({how})"
    elif result.outcome == "absent":
        text = "not on the primary"
    else:
        text = result.outcome
    return f"    {result.ref:<{width}}{text}"


def _state_word(result: RefResult) -> str:
    """One ref's state in ``aisland workspace configure mirror``: ``dev/main 3 behind``."""
    if result.ref == "tags":
        if result.outcome == "refused":
            return "tags differ"
        if result.outcome == "to push":
            return f"{_plural(result.count, 'tag')} missing"
        return "tags in sync"
    if result.outcome == "to push":
        if result.detail == _PRUNED:
            return f"{result.ref} pruned on the primary"
        return f"{result.ref} new" if result.detail == "new branch" else f"{result.ref} {result.count} behind"
    if result.outcome == "refused":
        return f"{result.ref} mirror ahead"
    if result.outcome == "absent":
        return f"{result.ref} not on the primary"
    return f"{result.ref} in sync"


def run_sync(project: Optional[str] = None, push: bool = True) -> int:
    """``aisland workspace mirror sync``: push and report (``push=False``, ``--dry-run``: what it would push). Exit 1
    when anything was refused or failed."""
    results = sync(project, push=push)
    if not results:
        console.echo("No mirrors configured. Set one with: aisland workspace configure mirror set <project> <url>")
        return 0
    for result in results:
        console.echo(f"==> {result.project} → {result.url}")
        if result.state == "skipped":
            console.echo(f"    skipped: {result.note}")
            continue
        if result.state == "failed":
            console.echo(f"    FAILED: {result.note}")
        width = max([10, *(len(ref.ref) + 2 for ref in result.refs)])
        for ref in result.refs:
            console.echo(_sync_line(ref, width))
    return 1 if any(r.state in ("refused", "failed") for r in results) else 0


def _remote_word(workspace_root: Path, project: str, url: Optional[str]) -> str:
    """The checkout's ``mirror`` remote against ``url``, read from its config alone."""
    path = workspace_root / _submodules(workspace_root).get(project, project)
    if url is None:
        return "-"
    if not _has_checkout(path):
        return "no checkout here"
    current, managed = _remote_state(path)
    if not current:
        return "remote not added yet (aisland setup adds it)"
    if not managed and not _same_repo(current, url):
        return f"a remote `{MIRROR_REMOTE}` aisland did not create: {current}"
    return "remote set" if current == url else "remote differs (aisland setup updates it)"


def rows(workspace_root: Path) -> List[Dict[str, str]]:
    """One row per mirrored project for ``aisland workspace configure mirror``: both layers and the checkout's remote,
    read from the configuration alone — no remote is contacted and nothing is written. What a sync would push is
    ``aisland workspace mirror sync --dry-run``."""
    committed = read_committed_mirrors(workspace_root)
    return [
        {
            "project": project,
            "committed": committed.get(project, "-"),
            "override": local_override(workspace_root, project) or "-",
            "state": _remote_word(workspace_root, project, effective_mirror(workspace_root, project, committed)),
        }
        for project in mirrored_projects(workspace_root)
    ]
