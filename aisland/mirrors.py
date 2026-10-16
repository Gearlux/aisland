"""Per-project push mirrors (``aisland mirror``).

A submodule's PRIMARY is the remote ``.gitmodules`` names — the one every machine clones from,
pulls from and pushes to (``origin``). A MIRROR is a second host that receives a copy of the
primary and nothing else: ``aisland mirror sync`` pushes the primary's ``dev/main``, ``main`` and
tags to it, and nothing is ever pulled from it. The use is a project that must be visible on two
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

What is pushed is the PRIMARY's state (``git ls-remote origin``), not the local branches: a
commit you have not pushed to the primary does not reach the mirror either, so the two hosts
can only ever differ by the mirror being behind.

Two configuration layers, the same split as :mod:`aisland.sources`:

* **committed** — the ``[mirrors.<project>]`` tables of ``<workspace>/config.toml`` (``url``),
  shared between machines;
* **per-machine** — ``git config --local aisland.mirror.<project> <url>`` in the workspace
  root, which replaces the committed URL on this machine only.

The effective URL is kept as a remote named ``mirror`` inside the submodule's checkout, marked
``remote.mirror.aislandManaged`` so that ``aisland setup`` and ``aisland mirror unset`` only ever
remove a remote aisland created; ``remote.mirror.tagOpt = --no-tags`` keeps a fetch from the
mirror from importing its tags. Every network call runs with ``GIT_TERMINAL_PROMPT=0``: a host
whose credentials are missing fails the sync with git's message instead of stalling it on a
password prompt.

Only submodules are mirrored (user choice 2026-10-16); the root repository is not.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

import click

from aisland.config import CONFIG_FILE
from aisland.config import load as load_config
from aisland.config import update as update_config
from aisland.manager import get_workspace_root, submodule_active_map

#: The registry's key in the committed ``config.toml`` — one ``[mirrors.<project>]`` table each.
MIRRORS_KEY = "mirrors"

#: The name of the remote that carries the mirror inside a submodule's checkout.
MIRROR_REMOTE = "mirror"

#: The remote that carries the primary (``git submodule`` names it so).
PRIMARY_REMOTE = "origin"

#: The branches a sync copies — the workspace's two long-lived ones.
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
        count: Commits pushed (a branch behind), tags pushed; ``0`` for a new branch.
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


# --- committed layer: the [mirrors.*] tables of <workspace>/config.toml --------------------


def read_committed_mirrors(workspace_root: Path) -> Dict[str, str]:
    """``{project: url}`` from ``config.toml``; a malformed entry is warned about and skipped."""
    data = load_config(workspace_root).get(MIRRORS_KEY)
    if not isinstance(data, dict):
        return {}
    result: Dict[str, str] = {}
    for project, entry in data.items():
        url = entry.get("url") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.strip():
            click.echo(f"  - Warning: {CONFIG_FILE}: [{MIRRORS_KEY}.{project}] has no url; ignored")
            continue
        result[project] = url.strip()
    return result


def write_committed_mirrors(workspace_root: Path, mirrors: Dict[str, str]) -> Path:
    """Rewrite the ``[mirrors.*]`` tables, leaving the other sections of the file intact."""
    table = {project: {"url": url} for project, url in sorted(mirrors.items())}
    return update_config(workspace_root, MIRRORS_KEY, table or None)


# --- per-machine layer: .git/config aisland.mirror.<project> -----------------------------


def _override_key(project: str) -> str:
    return f"aisland.mirror.{project}"


def local_override(workspace_root: Path, project: str) -> Optional[str]:
    """This machine's mirror URL for ``project``, or ``None`` when it follows the committed one."""
    value = _git_out(workspace_root, "config", "--local", "--get", _override_key(project))
    return value or None


def set_local_override(workspace_root: Path, project: str, url: Optional[str]) -> None:
    """Record (or clear, when ``url`` is ``None``) this machine's URL. Never committed."""
    if url is None:
        _git(workspace_root, "config", "--local", "--unset", _override_key(project))
    else:
        _git(workspace_root, "config", "--local", _override_key(project), url)


def _overridden_projects(workspace_root: Path) -> List[str]:
    listing = _git_out(workspace_root, "config", "--local", "--get-regexp", r"^aisland\.mirror\.")
    prefix = "aisland.mirror."
    return sorted(line.split(" ", 1)[0][len(prefix) :] for line in listing.splitlines() if line.startswith(prefix))


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
    """Run git in ``repo``. A network call never prompts: missing credentials fail at once."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"} if network else None
    return subprocess.run(["git", *args], cwd=str(repo), text=True, capture_output=True, env=env)


def _git_out(repo: Path, *args: str) -> str:
    """git's stdout, stripped; empty when the command failed (an unset key, a missing ref)."""
    res = _git(repo, *args)
    return res.stdout.strip() if res.returncode == 0 else ""


def _last_line(res: "subprocess.CompletedProcess[str]") -> str:
    lines = [line for line in (res.stderr or res.stdout).splitlines() if line.strip()]
    return lines[-1].strip() if lines else f"git exited {res.returncode}"


def _submodules(workspace_root: Path) -> Dict[str, str]:
    """``{name: relative path}`` for every submodule ``.gitmodules`` declares."""
    return {name: path for name, path, _active in submodule_active_map(workspace_root)}


def _has_checkout(path: Path) -> bool:
    return (path / ".git").exists()


def primary_url(workspace_root: Path, project: str) -> str:
    """The submodule's remote as ``.gitmodules`` declares it (empty when undeclared)."""
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
        _git(path, "remote", "add", MIRROR_REMOTE, url)
    elif current != url:
        _git(path, "remote", "set-url", MIRROR_REMOTE, url)
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
        click.echo(f"  - Warning: {project} already has a remote named `{MIRROR_REMOTE}` ({foreign}); left as it is")
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
        click.echo(f"Error: '{project}' is not a submodule of this workspace; a mirror is set per submodule.", err=True)
        return 1
    if not url:
        click.echo("Error: the mirror URL is empty.", err=True)
        return 1
    primary = primary_url(root, project)
    if _same_repo(url, primary):
        click.echo(f"Error: {url} is {project}'s primary (.gitmodules); a mirror is a second host.", err=True)
        return 1
    rel = subs[project]
    path = root / rel
    if _has_checkout(path):
        foreign = _foreign_remote(path, url)
        if foreign:
            click.echo(
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
        where = f"recorded in {CONFIG_FILE} (commit it)"
    apply_mirror(root, project)

    click.echo(f"==> {project}: mirror → {url}")
    remote = f"remote `{MIRROR_REMOTE}` added to {rel}" if _has_checkout(path) else "no checkout yet"
    click.echo(f"    {where}; {remote}")
    branches = ", ".join(MIRRORED_BRANCHES)
    click.echo(
        f"    Note: each sync copies {project}'s {branches} and tags from {host_of(primary)} to {host_of(url)}"
        " — make sure its code may live there."
    )
    click.echo(f"    Push to it with: aisland mirror sync {project}")
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
        click.echo(f"==> {project}: mirror → {url} (the committed one)")
    else:
        click.echo(f"==> {project}: no mirror")
    return 0


# --- sync ------------------------------------------------------------------------------------


def _ls_remote(path: Path, remote: str) -> Tuple[Optional[Dict[str, str]], str]:
    """``({ref: sha}, "")`` for a remote's branches and tags, or ``(None, git's message)``."""
    res = _git(path, "ls-remote", "--heads", "--tags", "--refs", remote, network=True)
    if res.returncode != 0:
        return None, _last_line(res)
    refs: Dict[str, str] = {}
    for line in res.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref] = sha
    return refs, ""


def _missing_objects(path: Path, shas: List[str]) -> List[str]:
    return [sha for sha in shas if _git(path, "cat-file", "-e", sha).returncode != 0]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _branch_result(
    path: Path, rel: str, branch: str, primary: Optional[str], mirror: Optional[str], push: bool
) -> Tuple[RefResult, Optional[str]]:
    """The branch's outcome and, when it is to be pushed, its refspec."""
    pushed: RefOutcome = "pushed" if push else "to push"
    if primary is None:
        return RefResult(branch, "absent", detail="not on the primary"), None
    spec = f"{primary}:refs/heads/{branch}"
    if mirror is None:
        return RefResult(branch, pushed, detail="new branch"), spec
    if mirror == primary:
        return RefResult(branch, "in sync"), None
    ahead = int(_git_out(path, "rev-list", "--count", f"{primary}..{mirror}") or "0")
    if ahead:
        newest = _git_out(path, "log", "-1", '--format=%h "%s"', mirror)
        detail = (
            f"the mirror has {_plural(ahead, 'commit')} the primary does not ({newest})\n"
            f"merge it first: git -C {rel} fetch {MIRROR_REMOTE} && git -C {rel} merge {MIRROR_REMOTE}/{branch}"
        )
        return RefResult(branch, "refused", detail=detail), None
    behind = int(_git_out(path, "rev-list", "--count", f"{mirror}..{primary}") or "0")
    return RefResult(branch, pushed, count=behind), spec


def _tags_result(primary: Dict[str, str], mirror: Dict[str, str], push: bool) -> Tuple[RefResult, List[str]]:
    """The tags' outcome and the refspecs of the tags to push."""
    missing = sorted(tag for tag in primary if tag not in mirror)
    differ = sorted(tag for tag in primary if tag in mirror and mirror[tag] != primary[tag])
    specs = [f"{primary[tag]}:{tag}" for tag in missing]
    names = [tag[len("refs/tags/") :] for tag in missing]
    if differ:
        shown = ", ".join(tag[len("refs/tags/") :] for tag in differ)
        detail = f"{shown} differ{'s' if len(differ) == 1 else ''} on the mirror; a tag is never overwritten"
        if names:
            detail += f" (pushed {', '.join(names)})" if push else f" ({len(names)} to push)"
        return RefResult("tags", "refused", count=len(missing), detail=detail), specs
    if missing:
        return RefResult("tags", "pushed" if push else "to push", count=len(missing), detail=", ".join(names)), specs
    return RefResult("tags", "in sync"), specs


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

    primary_refs, error = _ls_remote(path, PRIMARY_REMOTE)
    if primary_refs is None:
        return ProjectSync(project, url, "failed", note=f"the primary: {error}")
    mirror_refs, error = _ls_remote(path, MIRROR_REMOTE)
    if mirror_refs is None:
        return ProjectSync(project, url, "failed", note=f"the mirror: {error}")

    # The objects the comparison and the push need, fetched by id: the primary's tips and tags
    # (a local checkout may lack a tag or a commit another machine pushed), then the mirror's
    # branches into refs/remotes/mirror/*, which the refusal's merge command names.
    wanted = [sha for ref, sha in primary_refs.items() if ref.startswith(("refs/heads/", "refs/tags/"))]
    missing = _missing_objects(path, wanted)
    if missing:
        res = _git(path, "fetch", "--quiet", "--no-tags", PRIMARY_REMOTE, *missing, network=True)
        if res.returncode != 0:
            return ProjectSync(project, url, "failed", note=f"the primary: {_last_line(res)}")
    if any(ref.startswith("refs/heads/") for ref in mirror_refs):
        res = _git(path, "fetch", "--quiet", MIRROR_REMOTE, network=True)
        if res.returncode != 0:
            return ProjectSync(project, url, "failed", note=f"the mirror: {_last_line(res)}")

    results: List[RefResult] = []
    specs: List[str] = []
    for branch in MIRRORED_BRANCHES:
        ref = f"refs/heads/{branch}"
        result, spec = _branch_result(path, rel, branch, primary_refs.get(ref), mirror_refs.get(ref), push)
        results.append(result)
        if spec:
            specs.append(spec)
    primary_tags = {ref: sha for ref, sha in primary_refs.items() if ref.startswith("refs/tags/")}
    mirror_tags = {ref: sha for ref, sha in mirror_refs.items() if ref.startswith("refs/tags/")}
    tags, tag_specs = _tags_result(primary_tags, mirror_tags, push)
    results.append(tags)
    specs.extend(tag_specs)

    if push and specs:
        res = _git(path, "push", "--quiet", MIRROR_REMOTE, *specs, network=True)
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


def _sync_line(result: RefResult) -> str:
    """One row of ``aisland mirror sync``: ``dev/main  pushed (3 commits)``."""
    if result.outcome == "refused":
        first, *rest = result.detail.splitlines()
        text = f"REFUSED: {first}" + "".join(f"\n{'':14}{line}" for line in rest)
    elif result.ref == "tags" and result.outcome in ("pushed", "to push"):
        text = f"{result.outcome} {result.detail}"
    elif result.outcome in ("pushed", "to push"):
        how = "new branch" if result.detail == "new branch" else _plural(result.count, "commit")
        text = f"{result.outcome} ({how})"
    elif result.outcome == "absent":
        text = "not on the primary"
    else:
        text = result.outcome
    return f"    {result.ref:<10}{text}"


def _state_word(result: RefResult) -> str:
    """One ref's state in ``aisland mirror list``: ``dev/main 3 behind``."""
    if result.ref == "tags":
        if result.outcome == "refused":
            return "tags differ"
        if result.outcome == "to push":
            return f"{_plural(result.count, 'tag')} missing"
        return "tags in sync"
    if result.outcome == "to push":
        return f"{result.ref} new" if result.detail == "new branch" else f"{result.ref} {result.count} behind"
    if result.outcome == "refused":
        return f"{result.ref} mirror ahead"
    if result.outcome == "absent":
        return f"{result.ref} not on the primary"
    return f"{result.ref} in sync"


def run_sync(project: Optional[str] = None) -> int:
    """``aisland mirror sync``: push and report. Exit 1 when anything was refused or failed."""
    results = sync(project)
    if not results:
        click.echo("No mirrors configured. Set one with: aisland mirror set <project> <url>")
        return 0
    for result in results:
        click.echo(f"==> {result.project} → {result.url}")
        if result.state == "skipped":
            click.echo(f"    skipped: {result.note}")
            continue
        if result.state == "failed":
            click.echo(f"    FAILED: {result.note}")
        for ref in result.refs:
            click.echo(_sync_line(ref))
    return 1 if any(r.state in ("refused", "failed") for r in results) else 0


def run_list() -> int:
    """``aisland mirror list``: both layers and what a sync would do. Pushes nothing."""
    root = get_workspace_root()
    projects = mirrored_projects(root)
    if not projects:
        click.echo("No mirrors configured. Set one with: aisland mirror set <project> <url>")
        return 0
    committed = read_committed_mirrors(root)
    click.echo(click.style(f"  {'PROJECT':<22} {'COMMITTED':<46} {'OVERRIDE':<20} STATE", bold=True))
    for project in projects:
        result = sync_project(root, project, push=False)
        if result.state in ("skipped", "failed"):
            state = f"{result.state}: {result.note}"
        else:
            state = " · ".join(_state_word(ref) for ref in result.refs)
        click.echo(
            f"  {project:<22} {committed.get(project, '-'):<46} {local_override(root, project) or '-':<20} {state}"
        )
    return 0
