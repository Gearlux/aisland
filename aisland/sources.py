"""Per-project dependency-source management (``aisland source``).

A workspace project can be consumed in exactly three ways ON THIS MACHINE, and this module owns
the choice plus the artifacts that have to agree with it — the checkout, the shared ``.venv``
and the editor folder list:

========== ================================= =====================================
source     directory                         ``.venv``
========== ================================= =====================================
``local``  checked out, ``git switch <ref>`` ``uv pip install -e <dir>[dev,…]``
``github`` deinit'd (empty)                  ``uv pip install git+…@<ref>``
``pypi``   deinit'd (empty)                  ``uv pip install -U <dist>``
========== ================================= =====================================

**No CI file reads a source.** A runner installs a RELEASED dependency from PyPI and an
unreleased one from GitHub ``main`` (:func:`released_on_pypi`, asked by
:meth:`aisland.services.jenkins.JenkinsService.scaffold_pipelines`) — user ruling 2026-09-26:
"it should always point to released packages, and to GitHub for non-released ones". So
``aisland source set`` / ``unset`` never regenerate a CI file.

``local`` and ``github`` both carry a ``ref`` (branch or tag) and differ ONLY in whether the
working tree survives: ``local`` is "I develop this here", ``github`` is "pin it to a remote
commit, I am not editing it". The names are deliberately far apart because the commands are one
word apart and one of them runs ``git submodule deinit`` — ``git``/``github`` would have been a
three-character difference between keeping and deleting a checkout.

A project with no entry anywhere is *unmanaged*: nothing is switched and nothing is installed
differently — so this module changes no behaviour until a source is set.

Two configuration layers, mirroring the existing ``.gitmodules active`` +
``.git/config submodule.<name>.active`` split (:func:`aisland.manager._local_submodule_active`):

* **committed** — the ``[sources.<project>]`` tables of ``<workspace>/config.toml``, the
  workspace's single config file (tracked, shared between machines; see :mod:`aisland.config`);
* **per-machine** — ``git config --local aisland.source.<project>`` (never committed).

Which layer drives what is load-bearing, because a machine-local choice must never reach a
tracked file:

* *committed source only* -> the registry's ``distribution`` field, which lets the CI scaffolder
  DISCOVER a detached project whose directory is empty (never to choose its install line);
* *effective source* (override, else committed) -> submodule init/deinit + branch switch and the
  editable/PyPI install into the shared ``.venv`` (via :func:`apply_source`) — and, through the
  checkout, the folder list of the generated, gitignored ``*.code-workspace`` file
  (:func:`aisland.manager.write_code_workspace`, rewritten after every source change).
"""

from __future__ import annotations

import functools
import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, cast, get_args

import click
from packaging.version import InvalidVersion, Version

from aisland import locations
from aisland.config import CONFIG_FILE
from aisland.config import load as load_config
from aisland.config import update as update_config
from aisland.manager import _set_local_submodule_active, get_workspace_root, submodule_active_map
from aisland.selection import editable_target

# The closed set of source kinds. Everything that needs to enumerate them (click's
# ``Choice``, docs, future MCP/JSON schemas) derives from THIS annotation via
# ``get_args`` — never a restated parallel list.
SourceKind = Literal["local", "github", "pypi"]
SOURCE_KINDS: Tuple[str, ...] = get_args(SourceKind)

#: The kinds that carry a git ``ref``. ``pypi`` carries a ``version`` instead.
REF_KINDS: Tuple[str, ...] = ("local", "github")

#: The kinds that leave the project directory EMPTY (submodule deinit'd). Consumed by
#: :func:`detached_projects` -> :func:`aisland.manager.reconcile_submodules`.
DETACHED_KINDS: Tuple[str, ...] = ("github", "pypi")

#: The registry's key in the workspace's single committed ``config.toml`` — one
#: ``[sources.<project>]`` table per managed project (see :mod:`aisland.config`).
SOURCES_KEY = "sources"

#: Ref used by a ``local`` / ``github`` source that does not name one — matches the ``@main``
#: that the CI scaffolder emitted unconditionally before this module existed, so an unmanaged
#: project and an explicit ``local:main`` produce byte-identical build files.
DEFAULT_REF = "main"


@dataclass(frozen=True)
class ProjectSource:
    """Where one workspace project's package is consumed from.

    Args:
        kind: ``"local"`` (checkout on ``ref``, editable install), ``"github"`` (no checkout;
            installed from the remote at ``ref``) or ``"pypi"`` (no checkout; published wheel).
        ref: Branch or tag for the ``local`` / ``github`` kinds. Ignored by ``pypi``.
        version: PEP 440 specifier for the ``pypi`` kind (``">=0.1.0"``). ``None`` => latest.
        distribution: The PyPI / distribution name, which differs from the directory name for
            at least one project (``acme`` -> ``acme-tools``). Recorded in the committed
            file because a detached project's directory is empty and can no longer be read for it.
    """

    kind: SourceKind
    ref: str = DEFAULT_REF
    version: Optional[str] = None
    distribution: Optional[str] = None

    @property
    def detached(self) -> bool:
        """Whether this source leaves the project directory EMPTY (no checkout)."""
        return self.kind in DETACHED_KINDS

    def spec(self) -> str:
        """Compact round-trippable form: ``"local:dev/main"`` / ``"github:main"`` / ``"pypi:>=0.1.0"``."""
        if self.kind == "pypi":
            return f"pypi:{self.version}" if self.version else "pypi"
        return f"{self.kind}:{self.ref}"

    @classmethod
    def parse(cls, raw: str, *, distribution: Optional[str] = None) -> "ProjectSource":
        """Inverse of :meth:`spec`. Raises ``ValueError`` on an unknown kind."""
        kind_raw, _, rest = raw.strip().partition(":")
        kind_raw = kind_raw.strip().lower()
        rest = rest.strip()
        if kind_raw not in SOURCE_KINDS:
            raise ValueError(f"unknown source kind {kind_raw!r}; expected one of {', '.join(SOURCE_KINDS)}")
        if kind_raw == "pypi":
            return cls(kind="pypi", version=rest or None, distribution=distribution)
        return cls(kind=cast(SourceKind, kind_raw), ref=rest or DEFAULT_REF, distribution=distribution)

    def requirement(self, project: str) -> str:
        """The ``uv pip install`` argument for a ``pypi`` source (``"loggair>=0.1.0"``)."""
        return f"{self.distribution or project}{self.version or ''}"


# --- committed layer: the [sources.*] tables of <workspace>/config.toml ------------


def read_committed_sources(workspace_root: Path) -> Dict[str, ProjectSource]:
    """Parse the committed registry. A missing / unreadable entry yields ``{}`` (unmanaged).

    Never raises: a malformed registry must degrade to "nothing is managed" rather than break
    ``aisland setup`` or a CI scaffold, both of which call this on every run.
    """
    data = load_config(workspace_root).get(SOURCES_KEY)
    if not isinstance(data, dict):
        return {}

    result: Dict[str, ProjectSource] = {}
    for project, entry in data.items():
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("source", "")).strip().lower()
        if kind not in SOURCE_KINDS:
            expected = ", ".join(SOURCE_KINDS)
            click.echo(
                f"  - Warning: {CONFIG_FILE}: [{SOURCES_KEY}.{project}] has source={kind!r}; expected one of {expected}"
            )
            continue
        result[project] = ProjectSource(
            kind=cast(SourceKind, kind),
            ref=str(entry.get("ref") or DEFAULT_REF),
            version=str(entry["version"]) if entry.get("version") else None,
            distribution=str(entry["distribution"]) if entry.get("distribution") else None,
        )
    return result


def write_committed_sources(workspace_root: Path, sources: Dict[str, ProjectSource]) -> Path:
    """Rewrite the ``[sources.*]`` tables from ``sources``, leaving the other sections intact.

    Only the plain TOML values are built here; :mod:`aisland.config` owns the file's layout and
    its comments. An EMPTY mapping drops the section entirely — which is what makes a rolled-back
    ``aisland source set`` leave a workspace that was previously unmanaged exactly as it found it.
    """
    table = {
        project: {
            "source": src.kind,
            **({"distribution": src.distribution} if src.distribution else {}),
            **({"ref": src.ref} if src.kind in REF_KINDS else {"version": src.version} if src.version else {}),
        }
        for project, src in sorted(sources.items())
    }
    return update_config(workspace_root, SOURCES_KEY, table or None)


# --- per-machine layer: .git/config aisland.source.<project> ------------------------------


def _config_key(project: str) -> str:
    return f"aisland.source.{project}"


def local_override(workspace_root: Path, project: str) -> Optional[ProjectSource]:
    """The per-machine source override for ``project``, or ``None`` when undecided.

    Read from ``--local`` only, so the committed registry is NOT consulted here — that is the
    caller's fallback (see :func:`effective_source`). Mirrors
    :func:`aisland.manager._local_submodule_active`.
    """
    res = subprocess.run(
        ["git", "config", "--local", "--get", _config_key(project)],
        cwd=str(workspace_root),
        text=True,
        capture_output=True,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None
    try:
        return ProjectSource.parse(res.stdout.strip())
    except ValueError as e:
        click.echo(f"  - Warning: ignoring {_config_key(project)}: {e}")
        return None


def set_local_override(workspace_root: Path, project: str, source: Optional[ProjectSource]) -> None:
    """Record (or clear, when ``source`` is ``None``) the per-machine override. Never committed."""
    if source is None:
        subprocess.run(
            ["git", "config", "--local", "--unset", _config_key(project)],
            cwd=str(workspace_root),
            check=False,
            capture_output=True,
        )
        return
    subprocess.run(
        ["git", "config", "--local", _config_key(project), source.spec()],
        cwd=str(workspace_root),
        check=False,
    )


def effective_source(
    workspace_root: Path,
    project: str,
    committed: Optional[Dict[str, ProjectSource]] = None,
) -> Optional[ProjectSource]:
    """The source that governs THIS machine's checkout and venv: override, else committed, else ``None``.

    A compact override (``"pypi"``) carries no distribution name, so it inherits the committed
    entry's — otherwise switching a machine to PyPI would try to install the directory name
    (``acme``) instead of the distribution (``acme-tools``).
    """
    if committed is None:
        committed = read_committed_sources(workspace_root)
    override = local_override(workspace_root, project)
    if override is None:
        return committed.get(project)
    if override.distribution is None and project in committed:
        return ProjectSource(
            kind=override.kind,
            ref=override.ref,
            version=override.version,
            distribution=committed[project].distribution,
        )
    return override


def project_path(workspace_root: Path, project: str) -> Path:
    """Absolute path of ``project``'s directory, honouring a ``.gitmodules`` path that differs from its name."""
    for name, path, _active in submodule_active_map(workspace_root):
        if name == project:
            return workspace_root / path
    return workspace_root / project


def discover_distribution(project_dir: Path) -> Optional[str]:
    """The ``[project] name`` of a checked-out project, or ``None`` when it cannot be read.

    Called BEFORE a ``pypi`` switch deinits the directory — afterwards there is no
    ``pyproject.toml`` left to ask, which is exactly why the answer is persisted into the
    committed registry.
    """
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        with open(pyproject, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    name = (data.get("project") or {}).get("name")
    return name if isinstance(name, str) and name else None


def remote_url(workspace_root: Path, project: str) -> Optional[str]:
    """The submodule's configured remote from ``.gitmodules``, or ``None`` when undeclared.

    Read rather than hardcoded to ``github.com/Gearlux/<name>`` so a ``github`` source also
    works for the submodules that are NOT on GitHub (``vendorcli`` lives on an internal Bitbucket), and so it always matches the remote the operator's git auth is
    already set up for.
    """
    gitmodules = workspace_root / ".gitmodules"
    if not gitmodules.exists():
        return None
    url = _capture(
        ["git", "config", "-f", str(gitmodules), "--get", f"submodule.{project}.url"],
        workspace_root,
    )
    return url or None


def pip_vcs_url(url: str, ref: str) -> str:
    """Turn a git remote into a ``uv pip install`` VCS requirement pinned at ``ref``.

    ``git@host:org/repo.git`` is scp-style syntax that pip cannot parse — the colon must become
    a ``/`` under an explicit ``ssh://`` scheme. An https remote just gains the ``git+`` prefix.
    A ref containing a slash (``dev/main``) is fine after the ``@``; pip splits on the LAST one.
    """
    if url.startswith("git@"):
        host, _, path = url.partition(":")
        url = f"ssh://{host}/{path}"
    if not url.startswith("git+"):
        url = f"git+{url}"
    return f"{url}@{ref}"


def _run(cmd: List[str], cwd: Path, *, quiet: bool = False) -> bool:
    """Run ``cmd``; return True on success. A failure is reported, never raised.

    One project's unreachable remote or dirty checkout must not abort a whole
    ``aisland setup`` / ``aisland source apply`` sweep — the same non-fatal contract as
    :func:`aisland.manager.reconcile_submodules`.
    """
    try:
        subprocess.check_call(cmd, cwd=str(cwd))
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        if not quiet:
            click.echo(f"  - Error: `{' '.join(cmd)}` failed: {e}", err=True)
        return False


def _capture(cmd: List[str], cwd: Path) -> str:
    """Run ``cmd`` and return its stripped stdout; empty when it could not answer.

    Diagnostics only: a probe that cannot run must degrade to "unknown" so the caller falls
    back to a generic message, never raise into the middle of a source switch.
    """
    try:
        res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    return res.stdout.strip()


def _deinit_remedy(workspace_root: Path, path: Path, rel: str) -> str:
    """Name the remedy for a refused ``git submodule deinit`` — matching the ACTUAL blocker.

    Deinit refuses on two different conditions, and git reports both with the same wording
    ("contains local modifications") even though the fix lives in a DIFFERENT repository for
    each:

    * a dirty work tree INSIDE the submodule — fixed there, by committing or stashing;
    * a **stale gitlink** — the submodule is checked out at a commit other than the one the
      SUPERPROJECT records. This refuses even when the submodule's own tree is spotless, and
      it is the common case right after someone commits in the submodule without bumping the
      pointer in the workspace repo.

    Guessing the wrong one sends the operator hunting through a clean tree (the message used
    to say "commit or stash the changes in <sub>" unconditionally), so probe for each and
    report only what is true — and when neither probe can answer, stay generic rather than
    assert something unverified.
    """
    clauses: List[str] = []
    if _capture(["git", "status", "--porcelain"], path):
        clauses.append(f"Commit or stash the changes in {rel}.")
    head = _capture(["git", "rev-parse", "HEAD"], path)
    recorded = _capture(["git", "rev-parse", f"HEAD:{rel}"], workspace_root)
    if head and recorded and head != recorded:
        clauses.append(
            f"The superproject records {recorded[:7]} for {rel} but the checkout is at "
            f"{head[:7]} — commit the pointer with `git add {rel} && git commit -- {rel}`."
        )
    if not clauses:
        return "Resolve the local modifications in the checkout or the superproject, then re-run."
    return " ".join(clauses) + " Then re-run."


# --- applying a source --------------------------------------------------------------------


def apply_source(workspace_root: Path, project: str, source: ProjectSource, uv_cmd: str) -> bool:
    """Bring the checkout and the shared ``.venv`` in line with ``source``. Returns success.

    ``local``: activate the submodule, initialize it if needed, fetch, switch to ``ref``, install
    editable. ``github`` / ``pypi``: resolve the distribution name while the directory is still
    readable, deinit (protectively — no ``-f``, so uncommitted work refuses and aborts BEFORE the
    venv is touched), then install from the remote at ``ref`` / from PyPI.
    """
    path = project_path(workspace_root, project)
    rel = str(path.relative_to(workspace_root))

    if source.kind == "local":
        click.echo(f"==> {project}: local checkout @ {source.ref}")
        _set_local_submodule_active(workspace_root, project, True)
        if not (path / ".git").exists():
            if not _run(["git", "submodule", "update", "--init", rel], workspace_root):
                return False
        # Non-fatal: an offline machine can still switch to a branch it already has.
        _run(["git", "fetch", "--quiet", "origin"], path, quiet=True)
        # No ``-f``: a switch that would discard uncommitted work refuses, protecting it. The
        # bare ref relies on git's DWIM (--guess) to create a tracking branch from origin/<ref>.
        if not _run(["git", "switch", source.ref], path):
            click.echo(f"  - {project}: left on its current branch; resolve local changes and re-run.", err=True)
            return False
        # The SAME target `aisland setup` installs (dev extra + the selected framework/tracker
        # extras) — the bare directory used to drop every extra the machine had selected.
        return _run([uv_cmd, "pip", "install", "-e", editable_target(workspace_root, path, project)], workspace_root)

    # github / pypi — both detach. Read the distribution name while the directory still exists
    # (afterwards there is no pyproject.toml left to ask), then empty it.
    distribution = source.distribution or discover_distribution(path) or project
    if source.kind == "github":
        url = remote_url(workspace_root, project)
        if url is None:
            click.echo(
                f"Error: {project} has no submodule URL in .gitmodules, so a github source has "
                f"nothing to install from. Use: aisland source set {project} local",
                err=True,
            )
            return False
        target = pip_vcs_url(url, source.ref)
        click.echo(f"==> {project}: github @ {source.ref} ({target})")
    else:
        target = ProjectSource(kind="pypi", version=source.version, distribution=distribution).requirement(project)
        click.echo(f"==> {project}: pypi ({target})")

    if (path / ".git").exists():
        if not _run(["git", "submodule", "deinit", rel], workspace_root):
            click.echo(
                f"  - {project}: deinit refused; the venv was left untouched. "
                f"{_deinit_remedy(workspace_root, path, rel)}",
                err=True,
            )
            return False
    _set_local_submodule_active(workspace_root, project, False)
    # Uninstall first, unconditionally: an editable install would otherwise keep its .pth entry,
    # and re-pinning github:main -> github:dev/main reports the SAME version, which uv would
    # treat as already satisfied and skip.
    _run([uv_cmd, "pip", "uninstall", distribution], workspace_root, quiet=True)
    return _run([uv_cmd, "pip", "install", "-U", target], workspace_root)


def apply_all(workspace_root: Path, uv_cmd: str) -> None:
    """Apply every managed project's EFFECTIVE source. Unmanaged projects are not touched."""
    committed = read_committed_sources(workspace_root)
    managed = sorted(set(committed) | set(_overridden_projects(workspace_root)))
    if not managed:
        return
    click.echo(f"==> Applying per-project sources ({CONFIG_FILE} [sources] + local overrides)...")
    for project in managed:
        source = effective_source(workspace_root, project, committed)
        if source is not None:
            apply_source(workspace_root, project, source, uv_cmd)


def _overridden_projects(workspace_root: Path) -> List[str]:
    """Project names carrying a per-machine ``aisland.source.*`` override in ``.git/config``."""
    res = subprocess.run(
        ["git", "config", "--local", "--get-regexp", r"^aisland\.source\."],
        cwd=str(workspace_root),
        text=True,
        capture_output=True,
    )
    names: List[str] = []
    for line in res.stdout.splitlines():
        key, _, _value = line.partition(" ")
        names.append(key[len("aisland.source.") :])
    return names


def detached_projects(workspace_root: Path) -> set[str]:
    """Projects whose EFFECTIVE source empties their directory (``github`` or ``pypi``).

    Consumed by :func:`aisland.manager.reconcile_submodules` so the activation machinery and the
    source machinery cannot fight over the same submodule.
    """
    committed = read_committed_sources(workspace_root)
    managed = set(committed) | set(_overridden_projects(workspace_root))
    result: set[str] = set()
    for project in managed:
        source = effective_source(workspace_root, project, committed)
        if source is not None and source.detached:
            result.add(project)
    return result


# --- reporting ----------------------------------------------------------------------------


def checkout_state(workspace_root: Path, project: str) -> str:
    """Human-readable on-disk state: the checked-out branch, or ``empty`` for a deinit'd directory."""
    path = project_path(workspace_root, project)
    if not (path / ".git").exists():
        return "empty"
    res = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(path),
        text=True,
        capture_output=True,
    )
    branch = res.stdout.strip()
    return f"on {branch}" if branch else "checked out"


def known_projects(workspace_root: Path) -> List[str]:
    """Every project ``aisland source`` can manage: declared submodules plus any sibling with a pyproject (a link to
    another folder of the workspace is none, :func:`aisland.locations.is_alias`)."""
    names = {name for name, _path, _active in submodule_active_map(workspace_root)}
    for child in workspace_root.iterdir():
        if child.is_dir() and (child / "pyproject.toml").exists() and not locations.is_alias(child, workspace_root):
            names.add(child.name)
    return sorted(names)


#: Provenance marker every workspace project carries in its published metadata, via the
#: ``[project.urls] Repository = "https://github.com/Gearlux/<name>.git"`` convention.
_GEARLUX_URL_FRAGMENT = "github.com/Gearlux/"


class ReleaseProbeError(click.ClickException):
    """PyPI could not answer whether ``distribution`` is released — the scaffold must stop, not guess."""

    def __init__(self, distribution: str, reason: str) -> None:
        super().__init__(
            f"cannot tell whether {distribution!r} is released: {reason}. The CI files were not written — "
            f"generating them needs PyPI, because a guess would flip every released dependency to a "
            f"GitHub install line. Retry when PyPI is reachable."
        )
        self.distribution = distribution


def _pypi_json(distribution: str) -> Tuple[Literal["ok", "missing", "unreachable"], Any]:
    """PyPI's JSON document for ``distribution``: ``("ok", data)``, ``("missing", None)`` on a 404,
    or ``("unreachable", reason)`` — shared by the provenance probe and the release probe."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{distribution}/json", timeout=5) as resp:
            return "ok", json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "missing", None
        return "unreachable", f"PyPI returned HTTP {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return "unreachable", f"could not reach PyPI ({e})"


def _points_at_gearlux(info: Dict[str, Any]) -> bool:
    """The provenance marker: the published metadata names a ``github.com/Gearlux/…`` URL."""
    urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
    return any(_GEARLUX_URL_FRAGMENT in str(url) for url in urls)


@functools.lru_cache(maxsize=None)
def released_on_pypi(distribution: str) -> bool:
    """Whether ``distribution`` is RELEASED: PyPI holds at least one FINAL (non-pre-release)
    version with a file that is not yanked, and its metadata points at ``github.com/Gearlux/``.

    The ONE question the CI scaffolder asks per internal dependency (user ruling 2026-09-26):
    released -> the runner installs it from PyPI (``.[dev]`` resolves it); not released -> from
    GitHub ``main``. The workspace SOURCE of the dependency (``config.toml``) plays no part —
    it describes one machine's checkout, never what a runner builds against. A pre-release
    alone (``recordstream`` 0.1.0a1) is not a release: a plain ``>=0.1.0`` excludes it, and the
    project is still developed from ``main``. A stranger's package under the same global name
    is not ours (the provenance rule :func:`_pypi_provenance` applies).

    Unlike :func:`_pypi_provenance` — where an unreachable index warns and continues — this
    RAISES :class:`ReleaseProbeError`: a guess here would rewrite committed CI files, and
    "assume unreleased" flips every released dependency to a GitHub line. Cached per process —
    a workspace scaffold asks about the same few distributions for every project.
    """
    status, payload = _pypi_json(distribution)
    if status == "missing":
        return False
    if status == "unreachable":
        raise ReleaseProbeError(distribution, str(payload))
    if not _points_at_gearlux(payload.get("info") or {}):
        return False
    for version, files in (payload.get("releases") or {}).items():
        try:
            final = not Version(version).is_prerelease
        except InvalidVersion:
            continue
        if final and any(not f.get("yanked", False) for f in files or []):
            return True
    return False


def _pypi_provenance(distribution: str) -> Tuple[Optional[bool], str]:
    """Whether PyPI's ``distribution`` really is THIS workspace's project.

    Returns ``(True, version)``; ``(False, reason)`` when the name is absent or belongs to an
    unrelated project; ``(None, reason)`` when PyPI could not be reached.

    Two failures this prevents, both of which would otherwise land AFTER the directory had
    already been deinit'd. The obvious one: an unpublished project fails deep inside
    ``uv pip install`` with "not found in the package registry". The subtle and worse one: PyPI
    names are GLOBAL, so a workspace project can share a name with a stranger's package, and a
    bare existence check would happily install that stranger's code into the shared venv. The
    workspace convention that every pyproject declares a ``github.com/Gearlux/…`` repository URL
    makes the published metadata a usable provenance marker, so it is checked rather than assumed.

    An unreachable index yields ``None`` (warn and continue) — never a false negative that would
    block an offline machine.
    """
    status, data = _pypi_json(distribution)
    if status == "missing":
        return False, "is not published on PyPI"
    if status == "unreachable":
        return None, str(data)

    info = data.get("info") or {}
    if not _points_at_gearlux(info):
        return False, (
            f"is published on PyPI (version {info.get('version')}) but its metadata does not point at "
            f"{_GEARLUX_URL_FRAGMENT}… — that name is owned by a different project"
        )
    return True, str(info.get("version") or "unknown")


def _remote_ref_exists(workspace_root: Path, project: str, ref: str) -> bool:
    """Whether ``ref`` resolves on ``project``'s remote. Unreachable remote => ``True``.

    The counterpart of :func:`_pypi_provenance` for the ``github`` kind: a ref that does not
    exist would fail INSIDE ``uv pip install``, i.e. after the deinit had already emptied the
    directory. An unreachable remote (offline, auth prompt) is inconclusive, not a rejection —
    same rule as the PyPI probe: never block a machine on a check it cannot perform.
    """
    url = remote_url(workspace_root, project)
    if url is None:
        return True
    # One call answers both questions: a non-zero exit means the remote could not be reached,
    # while exit 0 with empty output means it answered and has no such ref.
    probe = subprocess.run(
        ["git", "ls-remote", "--heads", "--tags", url, ref],
        cwd=str(workspace_root),
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        click.echo(f"  - Warning: could not reach {project}'s remote to verify '{ref}'; continuing.")
        return True
    return bool(probe.stdout.strip())


def set_project_source(
    project: str,
    kind: str,
    ref: Optional[str] = None,
    version: Optional[str] = None,
    machine_only: bool = False,
    workspace_root: Optional[Path] = None,
) -> int:
    """Record and apply a project's source. Returns a process exit code (0 = success).

    Order matters: validate first, then write configuration, then apply to the checkout + venv,
    then rewrite this machine's workspace file, whose folder list follows the checkouts.
    No CI file is touched: CI installs what is RELEASED, never what this machine checked out
    (see :func:`released_on_pypi`).
    A failed apply ROLLS THE CONFIGURATION BACK — a registry claiming a state the working tree
    does not have is worse than no change at all (and `aisland setup` would keep retrying it).
    """
    from aisland.manager import ensure_uv_in_venv, write_code_workspace

    root = workspace_root or get_workspace_root()
    if project not in known_projects(root):
        click.echo(f"Error: '{project}' is not a workspace project. Known: {', '.join(known_projects(root))}", err=True)
        return 1
    if kind not in SOURCE_KINDS:
        click.echo(f"Error: unknown source kind '{kind}'; expected one of {', '.join(SOURCE_KINDS)}", err=True)
        return 1
    if kind in DETACHED_KINDS and project not in {name for name, _path, _active in submodule_active_map(root)}:
        # A folder project (no .gitmodules entry) has no submodule to deinit and none to check out again.
        click.echo(
            f"Error: '{project}' is a folder of the workspace, not a submodule: a {kind} source empties its folder, "
            "and nothing could check it out again. Keep it local.",
            err=True,
        )
        return 1

    venv_bin = root / ".venv" / "bin"
    if not venv_bin.exists():
        click.echo(f"Error: no virtual environment at {root / '.venv'}. Run aisland/setup.sh first.", err=True)
        return 1

    original = read_committed_sources(root)
    previous_override = local_override(root, project)
    previous = original.get(project)
    distribution = (
        discover_distribution(project_path(root, project)) or (previous.distribution if previous else None) or project
    )

    if kind == "pypi":
        published, detail = _pypi_provenance(distribution)
        if published is False:
            click.echo(
                f"Error: '{distribution}' {detail}. Publish it first, or use: " f"aisland source set {project} github",
                err=True,
            )
            return 1
        if published is None:
            click.echo(f"  - Warning: {detail}; continuing without verifying '{distribution}'.")
        else:
            click.echo(f"  - Verified {distribution} {detail} on PyPI (Gearlux provenance).")
        source = ProjectSource(kind="pypi", version=version, distribution=distribution)
    else:
        source = ProjectSource(kind=cast(SourceKind, kind), ref=ref or DEFAULT_REF, distribution=distribution)
        # A github source DETACHES, so a bad ref would only surface after the directory was
        # already emptied — the same asymmetry the PyPI provenance check guards against.
        if kind == "github" and not _remote_ref_exists(root, project, source.ref):
            click.echo(
                f"Error: '{source.ref}' does not exist on {project}'s remote, so a github source "
                f"could never install. Push the branch first, or pick an existing ref.",
                err=True,
            )
            return 1

    committed = dict(original)
    if machine_only:
        set_local_override(root, project, source)
        click.echo(f"==> {project}: per-machine override set to {source.spec()} (nothing committed).")
    else:
        # A committed decision supersedes any stale per-machine deviation for this project.
        set_local_override(root, project, None)
        committed[project] = source
        path = write_committed_sources(root, committed)
        click.echo(f"==> {project}: committed source set to {source.spec()} in {path.name}")

    uv_cmd = ensure_uv_in_venv(venv_bin)
    if not apply_source(root, project, effective_source(root, project, committed) or source, uv_cmd):
        set_local_override(root, project, previous_override)
        if not machine_only:
            # Restoring the ORIGINAL mapping is the whole rollback: an empty one drops the
            # `[sources]` section again, so a workspace that had no registry keeps none.
            write_committed_sources(root, original)
        click.echo(f"  - {project}: source change reverted; configuration is unchanged.", err=True)
        return 1

    # The workspace file is per machine and gitignored, so a --machine-only change rewrites it too.
    write_code_workspace(root)
    return 0


def unset_project_source(
    project: str,
    machine_only: bool = False,
    workspace_root: Optional[Path] = None,
) -> int:
    """Return a project to *unmanaged*. Returns a process exit code (0 = success).

    Deliberately does NOT touch the checkout or the venv: "unmanaged" means aisland holds no
    opinion, not that the current state is wrong. Dropping a ``pypi`` entry therefore leaves the
    directory empty until the next ``aisland setup`` (whose ``.gitmodules``-driven
    :func:`aisland.manager.reconcile_submodules` re-inits it) — set an explicit ``git`` source
    instead if you want the checkout back now.
    """
    from aisland.manager import write_code_workspace

    root = workspace_root or get_workspace_root()
    committed = read_committed_sources(root)

    if machine_only:
        if local_override(root, project) is None:
            click.echo(f"'{project}' has no per-machine override; nothing to clear.")
            return 0
        set_local_override(root, project, None)
        click.echo(f"==> {project}: per-machine override cleared (committed source still applies).")
        return 0

    if project not in committed and local_override(root, project) is None:
        click.echo(f"'{project}' is already unmanaged.")
        return 0

    set_local_override(root, project, None)
    committed.pop(project, None)
    path = write_committed_sources(root, committed)
    click.echo(f"==> {project}: removed from {path.name} — now unmanaged.")

    write_code_workspace(root)
    return 0


def apply_configured(project: Optional[str] = None, workspace_root: Optional[Path] = None) -> int:
    """Re-apply configured source(s) without changing configuration. Returns an exit code."""
    from aisland.manager import ensure_uv_in_venv, write_code_workspace

    root = workspace_root or get_workspace_root()
    venv_bin = root / ".venv" / "bin"
    if not venv_bin.exists():
        click.echo(f"Error: no virtual environment at {root / '.venv'}. Run aisland/setup.sh first.", err=True)
        return 1
    uv_cmd = ensure_uv_in_venv(venv_bin)

    if project is None:
        apply_all(root, uv_cmd)
        write_code_workspace(root)
        return 0

    source = effective_source(root, project)
    if source is None:
        click.echo(f"'{project}' has no configured source — it is unmanaged. Nothing to apply.")
        return 0
    applied = apply_source(root, project, source, uv_cmd)
    write_code_workspace(root)
    return 0 if applied else 1


def describe(workspace_root: Optional[Path] = None) -> List[Dict[str, str]]:
    """One row per managed project for ``aisland source list``."""
    root = workspace_root or get_workspace_root()
    committed = read_committed_sources(root)
    rows: List[Dict[str, str]] = []
    for project in sorted(set(committed) | set(_overridden_projects(root))):
        override = local_override(root, project)
        effective = effective_source(root, project, committed)
        rows.append(
            {
                "project": project,
                "committed": committed[project].spec() if project in committed else "-",
                "override": override.spec() if override else "-",
                "effective": effective.spec() if effective else "-",
                "state": checkout_state(root, project),
            }
        )
    return rows
