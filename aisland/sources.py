"""Per-project dependency-source management (``aisland source``).

A workspace project can be consumed in exactly three ways, and this module owns the
choice plus every artifact that has to agree with it:

========== ================================= ============================== ==============================
source     directory                         ``.venv``                      consumer CI install line
========== ================================= ============================== ==============================
``local``  checked out, ``git switch <ref>`` ``uv pip install -e <dir>``    ``git+…/<name>.git@<ref>``
``github`` deinit'd (empty)                  ``uv pip install git+…@<ref>`` ``git+…/<name>.git@<ref>``
``pypi``   deinit'd (empty)                  ``uv pip install -U <dist>``   *(none — ``.[dev]`` resolves it from PyPI)*
========== ================================= ============================== ==============================

``local`` and ``github`` both carry a ``ref`` (branch or tag) and differ ONLY in whether the
working tree survives: ``local`` is "I develop this here", ``github`` is "pin it to a remote
commit, I am not editing it". The names are deliberately far apart because the commands are one
word apart and one of them runs ``git submodule deinit`` — ``git``/``github`` would have been a
three-character difference between keeping and deleting a checkout.

A project with no entry anywhere is *unmanaged*: nothing is switched, nothing is installed
differently, and CI keeps the historical ``@main`` — so this module changes no behaviour until
a source is set.

Two configuration layers, mirroring the existing ``.gitmodules active`` +
``.git/config submodule.<name>.active`` split (:func:`aisland.manager._local_submodule_active`):

* **committed** — the ``[sources.<project>]`` tables of ``<workspace>/config.toml``, the
  workspace's single config file (tracked, shared between machines; see :mod:`aisland.config`);
* **per-machine** — ``git config --local aisland.source.<project>`` (never committed).

Which layer drives what is load-bearing, because every downstream artifact EXCEPT the venv and
the checkout is itself committed — a machine-local choice must never reach one:

* *committed source only* -> ``Jenkinsfile`` / ``Jenkinsfile.local`` / ``.github/workflows/ci.yml``
  (via :meth:`aisland.services.jenkins.JenkinsService.scaffold_pipelines`) and the
  ``*.code-workspace`` ``folders`` list (via :func:`sync_code_workspace`);
* *effective source* (override, else committed) -> submodule init/deinit + branch switch and the
  editable/PyPI install into the shared ``.venv`` (via :func:`apply_source`).

Because a per-machine override can empty a directory the COMMITTED ``Jenkinsfile.local`` still
names, every dep's generated editable-install line carries a ``[ -f <dir>/pyproject.toml ]``
guard that falls back to the committed source's git URL. The predicate tests the FILE, not the
directory: ``git submodule deinit`` leaves an empty directory behind, so a ``-d`` test would be
true for a detached dep and run the editable install against nothing. See ``jenkins.py``'s
``local_pip_cmds`` loop.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple, cast, get_args

import click

from aisland.config import CONFIG_FILE
from aisland.config import load as load_config
from aisland.config import update as update_config
from aisland.manager import _set_local_submodule_active, get_workspace_root, submodule_active_map

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
            at least one project (``aisland`` -> ``aisland-manager``). Recorded in the committed
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
    (``aisland``) instead of the distribution (``aisland-manager``).
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


def source_for_dependency(committed: Dict[str, ProjectSource], dep_name: str) -> Optional[ProjectSource]:
    """Resolve a DISTRIBUTION name (as it appears in a ``pyproject.toml`` dependency) to its entry.

    The registry is keyed by project (== submodule/directory) name, but CI scaffolding walks
    dependency specifiers. Three conventions are accepted, matching
    :func:`aisland.services.jenkins.pyproject_to_workspace_dir`: identity, an explicit
    ``distribution`` field, and the dash-stripped form.
    """
    if dep_name in committed:
        return committed[dep_name]
    for project, src in committed.items():
        if src.distribution == dep_name:
            return src
    dashless = dep_name.replace("-", "")
    return committed.get(dashless)


# --- filesystem helpers -------------------------------------------------------------------


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
    works for the submodules that are NOT on GitHub (``vendorcli`` / ``signals-bridge`` live on
    an internal Bitbucket), and so it always matches the remote the operator's git auth is
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
        return _run([uv_cmd, "pip", "install", "-e", str(path)], workspace_root)

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


# --- *.code-workspace `folders` synchronisation --------------------------------------------
#
# The workspace file is JSONC, not JSON: `settings` carries trailing commas, so `json.load`
# fails on it outright. Rather than reformat a hand-maintained committed file, the `folders`
# array is edited LINE-WISE and a removed project is COMMENTED OUT in place rather than
# deleted. That buys two properties nothing else does: the entry keeps its original position
# in the (dependency-ordered, not alphabetical) list, and commenting then uncommenting is
# byte-for-byte lossless — so flipping a project to `pypi` and back leaves `git diff` clean.

_MARKER = "// aisland source:"
_COMMENT_PREFIX_RE = re.compile(r"^(\s*)//\s?(.*)$")
_MARKER_RE = re.compile(r"^\s*//\s*aisland source:")
_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]*)"')
_PATH_RE = re.compile(r'"path"\s*:\s*"([^"]*)"')
_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


@dataclass
class _FolderEntry:
    """One ``folders`` element: its canonical (uncommented) lines plus whether it is disabled."""

    name: Optional[str]
    path: Optional[str]
    lines: List[str]
    commented: bool
    lead: List[str] = field(default_factory=list)


def _uncomment(line: str) -> Tuple[str, bool]:
    """``"    // {"`` -> ``("    {", True)``; a non-comment line is returned unchanged."""
    m = _COMMENT_PREFIX_RE.match(line)
    if m:
        return m.group(1) + m.group(2), True
    return line, False


def _comment(line: str, base_indent: str) -> str:
    """Inverse of :func:`_uncomment`.

    ``base_indent`` is the block's OUTER indent, so every ``//`` in a commented-out entry lines
    up while the relative indentation of the inner lines is carried after the marker — which is
    what makes uncommenting recover the original bytes.
    """
    rest = line[len(base_indent) :] if line.startswith(base_indent) else line.lstrip(" ")
    return f"{base_indent}// {rest}" if rest else f"{base_indent}//"


def _brace_delta(payload: str) -> int:
    """Net brace depth of a line, ignoring anything inside a JSON string."""
    bare = _STRING_RE.sub("", payload)
    return bare.count("{") - bare.count("}")


def _find_folders_span(text: str) -> Optional[Tuple[int, int]]:
    """Character span of the ``"folders": [ … ]`` array, inclusive of both brackets.

    A hand-rolled scanner rather than a JSON parse: the surrounding document is JSONC and
    already contains ``//`` comments and trailing commas that no strict parser accepts.
    """
    key = re.search(r'"folders"\s*:\s*\[', text)
    if not key:
        return None
    start = key.end() - 1
    depth = 0
    in_string = False
    in_comment = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_comment:
            if ch == "\n":
                in_comment = False
        elif in_string:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "/" and text[i + 1 : i + 2] == "/":
            in_comment = True
            i += 1
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return start, i + 1
        i += 1
    return None


def _parse_entries(body: str) -> Tuple[List[_FolderEntry], List[str]]:
    """Split the array body into entries (plus any trailing filler lines).

    Commented-out entries are recognised by uncommenting each line before counting braces, so a
    disabled entry has exactly the same structure as an enabled one.
    """
    entries: List[_FolderEntry] = []
    lead: List[str] = []
    current: List[str] = []
    all_commented = True
    depth = 0
    for line in body.splitlines():
        payload, was_comment = _uncomment(line)
        if depth == 0 and not current and (not payload.strip() or _MARKER_RE.match(line)):
            lead.append(line)
            continue
        current.append(payload)
        all_commented = all_commented and was_comment
        depth += _brace_delta(payload)
        if depth == 0:
            joined = "\n".join(current)
            name_m = _NAME_RE.search(joined)
            path_m = _PATH_RE.search(joined)
            entries.append(
                _FolderEntry(
                    name=name_m.group(1) if name_m else None,
                    path=path_m.group(1) if path_m else None,
                    lines=current,
                    commented=all_commented,
                    lead=[ln for ln in lead if not _MARKER_RE.match(ln)],
                )
            )
            lead, current, all_commented = [], [], True
    return entries, lead


def _render_entries(entries: List[_FolderEntry]) -> str:
    """Re-emit the array body, normalising trailing commas over the ENABLED entries only."""
    active = [e for e in entries if not e.commented]
    last_active = active[-1] if active else None
    out: List[str] = []
    for entry in entries:
        out.extend(entry.lead)
        base = _leading_indent(entry.lines[0])
        if entry.commented and entry.name:
            out.append(
                f"{base}{_MARKER} '{entry.name}' is consumed as a package (no checkout); "
                f"restore it with `aisland source set {entry.name} local`."
            )
        lines = list(entry.lines)
        # The closing brace carries the separator: every enabled entry but the last needs one.
        closing = lines[-1].rstrip()
        want_comma = entry is not last_active
        if closing.endswith("},"):
            lines[-1] = closing if want_comma else closing[:-1]
        elif closing.endswith("}"):
            lines[-1] = closing + "," if want_comma else closing
        out.extend(_comment(ln, base) if entry.commented else ln for ln in lines)
    return "\n".join(out)


def _leading_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" "))]


def _new_entry(template: Optional[_FolderEntry], project: str) -> _FolderEntry:
    """Build a fresh ``{"name": …, "path": …}`` element indented like its neighbours."""
    outer = _leading_indent(template.lines[0]) if template and template.lines else " " * 8
    inner = outer + " " * 4
    return _FolderEntry(
        name=project,
        path=project,
        lines=[f"{outer}{{", f'{inner}"name": "{project}",', f'{inner}"path": "{project}"', f"{outer}}}"],
        commented=False,
    )


def find_code_workspace(workspace_root: Path) -> Optional[Path]:
    """The single ``*.code-workspace`` file at the workspace root, if there is exactly one."""
    matches = sorted(workspace_root.glob("*.code-workspace"))
    if len(matches) != 1:
        return None
    return matches[0]


def sync_code_workspace(workspace_root: Path, committed: Dict[str, ProjectSource]) -> Optional[Path]:
    """Enable/disable ``folders`` entries to match the COMMITTED sources. Returns the file if changed.

    Driven by the committed layer, never the per-machine override: the workspace file is
    tracked, so honouring an override here would dirty the tree differently on every machine.
    Entries for projects that are NOT in the registry (``vendorcli``, ``signals-bridge``, …)
    are left exactly as they are.
    """
    path = find_code_workspace(workspace_root)
    if path is None:
        return None
    text = path.read_text()
    span = _find_folders_span(text)
    if span is None:
        click.echo(f"  - Warning: no `folders` array found in {path.name}; skipping folder sync.")
        return None

    start, end = span
    body = text[start + 1 : end - 1]
    entries, trailing = _parse_entries(body)
    by_name = {e.name: e for e in entries if e.name}

    for project, source in committed.items():
        want_enabled = not source.detached
        entry = by_name.get(project)
        if entry is None:
            if want_enabled:
                # Keep out-of-tree entries (e.g. {"path": "../logs"}) last.
                template = next((e for e in entries if e.path and not e.path.startswith("..")), None)
                insert_at = next(
                    (i for i, e in enumerate(entries) if e.path and e.path.startswith("..")),
                    len(entries),
                )
                entries.insert(insert_at, _new_entry(template, project))
            continue
        entry.commented = not want_enabled

    # `body` starts with the newline after `[`, so splitlines() already yielded a leading ""
    # element that `_render_entries` re-emits — no extra newline is prepended here.
    rendered = _render_entries(entries)
    if trailing:
        rendered += "\n" + "\n".join(trailing)
    new_text = text[: start + 1] + rendered + text[end - 1 :]
    if new_text == text:
        return None
    path.write_text(new_text)
    click.echo(f"==> Updated {path.name} folders ({sum(1 for e in entries if not e.commented)} enabled)")
    return path


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
    """Every project ``aisland source`` can manage: declared submodules plus any sibling with a pyproject."""
    names = {name for name, _path, _active in submodule_active_map(workspace_root)}
    for child in workspace_root.iterdir():
        if child.is_dir() and (child / "pyproject.toml").exists():
            names.add(child.name)
    return sorted(names)


#: Provenance marker every workspace project carries in its published metadata, via the
#: ``[project.urls] Repository = "https://github.com/Gearlux/<name>.git"`` convention.
_GEARLUX_URL_FRAGMENT = "github.com/Gearlux/"


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
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{distribution}/json", timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "is not published on PyPI"
        return None, f"PyPI returned HTTP {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        return None, f"could not reach PyPI ({e})"

    info = data.get("info") or {}
    urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
    if not any(_GEARLUX_URL_FRAGMENT in str(url) for url in urls):
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


def dependent_projects(workspace_root: Path, project: str, distribution: str) -> List[str]:
    """Workspace directory names whose dependency closure reaches ``project``.

    Only these need their ``Jenkinsfile`` / ``Jenkinsfile.local`` / ``ci.yml`` regenerated after a
    source change — re-scaffolding the whole workspace would be byte-identical for everyone else
    but costs a Jenkins linter round-trip per file.
    """
    from aisland.manager import get_install_order

    # A dependency specifier names a DISTRIBUTION ("aisland-manager"), while the CI scaffolder is
    # keyed by DIRECTORY ("aisland") — so each directory carries every alias it answers to.
    aliases: Dict[str, set[str]] = {}
    deps_by_dir: Dict[str, set[str]] = {}
    for meta in get_install_order(workspace_root):
        directory = Path(meta["path"]).name
        dist = str(meta["name"])
        aliases[directory] = {directory, dist, dist.replace("-", ""), dist.removesuffix("-manager")}
        deps_by_dir[directory] = {re.split(r"[>=<;\[ ]", spec)[0].strip() for spec in meta["dependencies"]}

    targets = {project, distribution, distribution.replace("-", "")}
    # Fixed point: a project depending on a dependent is itself a dependent.
    dependents: set[str] = set()
    changed = True
    while changed:
        changed = False
        reach = set(targets)
        for name in dependents:
            reach |= aliases.get(name, {name})
        for directory, deps in deps_by_dir.items():
            if directory in dependents or directory == project:
                continue
            if deps & reach:
                dependents.add(directory)
                changed = True
    return sorted(dependents)


def set_project_source(
    project: str,
    kind: str,
    ref: Optional[str] = None,
    version: Optional[str] = None,
    machine_only: bool = False,
    scaffold: bool = True,
    workspace_root: Optional[Path] = None,
) -> int:
    """Record and apply a project's source. Returns a process exit code (0 = success).

    Order matters: validate first, then write configuration, then apply to the checkout + venv,
    then regenerate the committed artifacts the COMMITTED layer drives (editor folders, CI).
    A failed apply ROLLS THE CONFIGURATION BACK — a registry claiming a state the working tree
    does not have is worse than no change at all (and `aisland setup` would keep retrying it).
    """
    from aisland.manager import ensure_uv_in_venv

    root = workspace_root or get_workspace_root()
    if project not in known_projects(root):
        click.echo(f"Error: '{project}' is not a workspace project. Known: {', '.join(known_projects(root))}", err=True)
        return 1
    if kind not in SOURCE_KINDS:
        click.echo(f"Error: unknown source kind '{kind}'; expected one of {', '.join(SOURCE_KINDS)}", err=True)
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

    # Committed artifacts follow the COMMITTED layer only — a --machine-only change must not
    # dirty the editor workspace or any project's CI files.
    if machine_only:
        click.echo("  - Skipped editor/CI regeneration (--machine-only affects this machine's venv only).")
        return 0

    sync_code_workspace(root, committed)
    # CI reads ONE bit of this registry — is the dep published? — and ignores the ref entirely
    # (see jenkins.scaffold_pipelines). So a ref change, or any switch between the two git-backed
    # kinds, cannot alter a tracked CI file; only crossing the pypi boundary can. Skipping the
    # regeneration when nothing would change avoids rewriting every dependent's three files (and
    # absorbing their unrelated template drift) on a routine `--ref` switch.
    if scaffold and _publishedness_changed(previous, source):
        _rescaffold_dependents(root, project, distribution)
    return 0


def _publishedness_changed(previous: Optional[ProjectSource], new: ProjectSource) -> bool:
    """Whether the pypi/not-pypi bit — the only thing CI reads — differs between two sources."""
    was_pypi = previous is not None and previous.kind == "pypi"
    return was_pypi != (new.kind == "pypi")


def unset_project_source(
    project: str,
    machine_only: bool = False,
    scaffold: bool = True,
    workspace_root: Optional[Path] = None,
) -> int:
    """Return a project to *unmanaged*. Returns a process exit code (0 = success).

    Deliberately does NOT touch the checkout or the venv: "unmanaged" means aisland holds no
    opinion, not that the current state is wrong. Dropping a ``pypi`` entry therefore leaves the
    directory empty until the next ``aisland setup`` (whose ``.gitmodules``-driven
    :func:`aisland.manager.reconcile_submodules` re-inits it) — set an explicit ``git`` source
    instead if you want the checkout back now.
    """
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
    previous = committed.pop(project, None)
    path = write_committed_sources(root, committed)
    click.echo(f"==> {project}: removed from {path.name} — now unmanaged.")

    # Re-enable the editor folder entry: "unmanaged" means the historical default, and a
    # lingering `// aisland source:` block would point at configuration that no longer exists.
    # Nothing is recorded — the synthetic entry only tells the sync which state to restore.
    sync_code_workspace(root, {**committed, project: ProjectSource(kind="local")})
    # Unmanaged reads as not-published to CI, so only dropping a `pypi` entry changes anything
    # there (see `_publishedness_changed`).
    if scaffold and _publishedness_changed(previous, ProjectSource(kind="local")):
        distribution = (previous.distribution if previous else None) or project
        _rescaffold_dependents(root, project, distribution)
    return 0


def _rescaffold_dependents(workspace_root: Path, project: str, distribution: str) -> None:
    """Regenerate the CI files of every project that depends on ``project``."""
    from aisland.services.jenkins import JenkinsService

    dependents = dependent_projects(workspace_root, project, distribution)
    if not dependents:
        click.echo("  - No dependent projects; no CI files to regenerate.")
        return
    click.echo(f"==> Regenerating CI files for {len(dependents)} dependent project(s): {', '.join(dependents)}")
    service = JenkinsService()
    for dependent in dependents:
        service.scaffold_pipelines(project_name=dependent, force=True)


def apply_configured(project: Optional[str] = None, workspace_root: Optional[Path] = None) -> int:
    """Re-apply configured source(s) without changing configuration. Returns an exit code."""
    from aisland.manager import ensure_uv_in_venv

    root = workspace_root or get_workspace_root()
    venv_bin = root / ".venv" / "bin"
    if not venv_bin.exists():
        click.echo(f"Error: no virtual environment at {root / '.venv'}. Run aisland/setup.sh first.", err=True)
        return 1
    uv_cmd = ensure_uv_in_venv(venv_bin)

    if project is None:
        apply_all(root, uv_cmd)
        sync_code_workspace(root, read_committed_sources(root))
        return 0

    source = effective_source(root, project)
    if source is None:
        click.echo(f"'{project}' has no configured source — it is unmanaged. Nothing to apply.")
        return 0
    return 0 if apply_source(root, project, source, uv_cmd) else 1


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
