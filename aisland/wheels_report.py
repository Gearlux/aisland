"""The report of `aisland wheels build`: the shapes a run returns and the text the command prints.

The shapes are JSON-serializable (:meth:`WheelsReport.to_dict`, ``aisland wheels build --json``): what an MCP tool
returns. :func:`render_report` is the text a person reads; :func:`regenerate_command` the command that writes the same
folder again, which every generated file names in its header (:func:`provenance_header`). The run itself is
:mod:`aisland.wheels`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

#: What a run builds: the needed projects PyPI lacks (the default), every needed project with a
#: checkout (``--include-published``), or every workspace project with a checkout (``--all``).
WheelsScope = Literal["unpublished", "needed", "all"]


#: Each scope's flag, as the command line spells it.
_SCOPE_FLAGS: Dict[str, str] = {"unpublished": "", "needed": " --include-published", "all": " --all"}


#: The flag that builds the project's own wheel too, as the command line spells it.
_INCLUDE_PROJECT_FLAG = " --include-project"


class ModuleFailure(TypedDict, total=False):
    """One module the import run (:data:`_IMPORT_SCRIPT`) could not import, as it writes it: the error, the module an
    ``ImportError`` names (``None`` for another error) and the traceback's files, outermost first."""

    error: str
    missing: Optional[str]
    frames: List[str]


class ImportRun(TypedDict):
    """The file the import run writes: every module that failed, by name, and every module that imported."""

    failures: Dict[str, ModuleFailure]
    imported: List[str]


@dataclass
class BuiltWheel:
    """A wheel written into the project's ``wheels/`` folder, and the commit it came from."""

    project: str
    distribution: str
    version: str
    branch: str
    commit: str
    wheel: str
    #: Whether the target depends on it (``--all`` also builds what it does not).
    needed: bool = True
    #: The version PyPI has, when the project is published there.
    on_pypi: Optional[str] = None
    #: ``git status --porcelain`` of a project ``--force`` built from its working tree (its ``commit`` then
    #: ends in ``+uncommitted``); empty for a wheel built from its commit.
    uncommitted: List[str] = field(default_factory=list)


@dataclass
class NotBuilt:
    """A workspace project the scope covers that this machine cannot build (no checkout here)."""

    project: str
    reason: str


@dataclass
class PublishedDependency:
    """A workspace project the target takes from PyPI, compared with its checkout here.

    ``differing_files`` is empty when the wheel this checkout builds holds what PyPI's does;
    ``note`` says why no comparison was made (no checkout here, no pure-Python wheel on PyPI).
    """

    project: str
    distribution: str
    pypi_version: str
    checkout_version: Optional[str]
    differing_files: List[str]
    note: Optional[str] = None


@dataclass
class ImportFailure:
    """A module that did not import in the empty venv.

    ``missing`` is the module the error names; ``distribution`` the project that owns it. ``culprits``
    are the distributions an error that names no module (a ``TypeError`` from an outdated release) is
    attributed to: see :func:`attribute_failure`.
    """

    module: str
    error: str
    missing: Optional[str]
    distribution: Optional[str]
    culprits: List[str] = field(default_factory=list)

    @property
    def blamed(self) -> List[str]:
        """The distributions the failure points at: the one owning the module it misses, else its culprits."""
        return [self.distribution] if self.distribution else self.culprits


@dataclass
class BundleWheel:
    """The one wheel ``--bundle`` merged: the project and the wheels it needs (:mod:`aisland.wheels_bundle`)."""

    #: The file, relative to the workspace.
    wheel: str
    distribution: str
    version: str
    #: The projects inside it, the target first.
    projects: List[str]
    bytes: int


@dataclass
class WheelsReport:
    """What ``aisland wheels <target>`` built and found."""

    target: str
    wheels_dir: str
    wheels_bytes: int
    built: List[BuiltWheel]
    published: List[PublishedDependency]
    name_clashes: List[str]
    install_error: Optional[str]
    import_failures: List[ImportFailure]
    imported: List[str] = field(default_factory=list)
    #: The packages whose modules were imported: the target's and its wheels' outermost ones.
    packages: List[str] = field(default_factory=list)
    #: Modules below those packages that miss something no workspace project ships — most often an
    #: optional extra's dependency, which the empty venv does not install. Reported; never an exit 1.
    import_warnings: List[ImportFailure] = field(default_factory=list)
    scope: WheelsScope = "unpublished"
    #: The projects the scope covers that were not built, and why.
    not_built: List[NotBuilt] = field(default_factory=list)
    #: Every workspace project the target needs, built or not.
    needed_projects: List[str] = field(default_factory=list)
    #: The commit ``--commit`` made in the target (short SHA), or why it made none (``commit_note``).
    committed: Optional[str] = None
    commit_note: Optional[str] = None
    #: The zip ``--zip`` wrote, relative to the workspace.
    zip_file: Optional[str] = None
    zip_bytes: int = 0
    #: The zip installs the project's own wheel, so it runs from any folder (else only from the project's).
    zip_standalone: bool = False
    #: Said when the folder is ignored but files of it are still tracked, with the command that untracks them.
    tracked_warning: Optional[str] = None
    #: False under ``--no-check``: nothing was compared with PyPI and nothing installed, so ``ok`` found no failure
    #: because it looked for none.
    checked: bool = True
    #: The target's own wheel (``--include-project``); never in ``built``, which holds what the target needs.
    project_wheel: Optional[BuiltWheel] = None
    #: The one wheel ``--bundle`` wrote, and its own check: installed alone in an empty venv, every module imported.
    bundle: Optional[BundleWheel] = None
    bundle_install_error: Optional[str] = None
    bundle_import_failures: List[ImportFailure] = field(default_factory=list)
    bundle_imported: List[str] = field(default_factory=list)
    #: The extra ``--extra`` wrote into the target's ``pyproject.toml``, and whether that file changed.
    extra: Optional[str] = None
    extra_changed: bool = False
    #: True when the extra was written without ``--extra``: the target's ``pyproject.toml`` holds its generated block.
    extra_remembered: bool = False
    #: The extra a generated block names that this run left as it is (``--include-project``, no ``--extra``).
    extra_left: Optional[str] = None

    @property
    def ok(self) -> bool:
        """The target installed in an empty venv, every package imported, and no module missed a workspace project
        — and so did the bundle, when there is one (an unchecked run: no failure, since nothing was checked)."""
        bundle_ok = self.bundle_install_error is None and not self.bundle_import_failures
        return self.install_error is None and not self.import_failures and bundle_ok

    @property
    def release_first(self) -> List[str]:
        """The published projects a failed import points at: PyPI lacks what the wheels use."""
        owners = {d for f in self.import_failures for d in f.blamed}
        return sorted(p.project for p in self.published if p.distribution in owners)

    @property
    def behind(self) -> List[str]:
        """The published projects whose checkout builds a different wheel than PyPI's."""
        return sorted(p.project for p in self.published if p.differing_files)

    @property
    def behind_only(self) -> List[str]:
        """Behind, yet no failed import points at them."""
        first = set(self.release_first)
        return [p for p in self.behind if p not in first]

    def to_dict(self) -> Dict[str, Any]:  # Any: asdict's JSON-ready nesting, which a reader indexes into
        """The report as JSON-ready data, verdicts included."""
        data = asdict(self)
        data.update(ok=self.ok, release_first=self.release_first, behind=self.behind)
        return data


def bundle_command(target: str, scope: WheelsScope, include_project: bool = False, extra: Optional[str] = None) -> str:
    """The command that writes a bundle — its METADATA's ``Generated by:`` and ``To regenerate:``."""
    return f"{regenerate_command(target, scope, include_project, extra)} --bundle"


def human_size(n_bytes: int) -> str:
    return f"{n_bytes / 1e6:.1f} MB" if n_bytes >= 1e6 else f"{n_bytes / 1e3:.0f} kB"


def _files(files: List[str]) -> str:
    shown = ", ".join(files[:3]) + (", …" if len(files) > 3 else "")
    return f"{len(files)} {'file differs' if len(files) == 1 else 'files differ'} ({shown})"


def _table(rows: List[List[str]]) -> List[str]:
    """Rows as two-space-indented, left-aligned columns (the last column unpadded)."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return ["  " + "  ".join([c.ljust(w) for c, w in zip(row, widths)] + [row[-1]]).rstrip() for row in rows]


def _built_lines(report: WheelsReport) -> List[str]:
    """The report's first block: what was built, for the scope it ran with."""
    target, n = report.target, len(report.built)
    where = f"{report.wheels_dir} ({human_size(report.wheels_bytes)})"
    if report.scope == "unpublished":
        if not n:
            return [f"{target} needs no workspace project that is not on PyPI; {report.wheels_dir} holds no wheel."]
        noun = (
            "1 workspace project that is not on PyPI. Its wheel is"
            if n == 1
            else f"{n} workspace projects that are not on PyPI. Their wheels are"
        )
        lines = [f"{target} needs {noun} in {where}:"]
        return lines + _table([[b.project, b.version, f"{b.branch} {b.commit}"] for b in report.built])
    count = len(report.needed_projects)
    needs = f"{target} needs {count} workspace project{'s' if count != 1 else ''}"
    if report.scope == "needed":
        lines = [f"{needs}; {n} {'is' if n == 1 else 'are'} built from this checkout into {where}:"]
    else:
        lines = [f"Every workspace project with a checkout here is built into {where}; {needs}:"]
    rows = []
    for b in report.built:
        notes = ([f"on PyPI {b.on_pypi}"] if b.on_pypi else []) + ([] if b.needed else ["not needed"])
        rows.append([b.project, b.version, f"{b.branch} {b.commit}", ", ".join(notes)])
    lines += _table(rows) if rows else ["  (no wheel)"]
    if report.not_built:
        lines.append("Not built: " + ", ".join(f"{nb.project} ({nb.reason})" for nb in report.not_built))
    return lines


def render_report(report: WheelsReport) -> str:
    """The text ``aisland wheels`` prints."""
    target = report.target
    lines = _built_lines(report)
    own = report.project_wheel
    if own is not None:
        lines.append(f"{target}'s own wheel (--include-project), installed in place of its folder:")
        lines += _table([[own.project, own.version, f"{own.branch} {own.commit}"]])
    lines.append("")

    forced = [b for b in report.built + ([own] if own else []) if b.uncommitted]
    if forced:
        lines.append("Built with changes no commit holds (--force); no commit reproduces these wheels:")
        for b in forced:
            lines.append(f"  {b.project}")
            lines += [f"    {line}" for line in b.uncommitted]
        lines.append("")

    if report.published and not report.checked:
        count = len(report.published)
        taken = ", ".join(f"{p.project} {p.pypi_version}" for p in report.published)
        lines.append(
            f"It takes {count} workspace project{'s' if count > 1 else ''} from PyPI, not compared (--no-check): {taken}"
        )
    elif report.published:
        count = len(report.published)
        lines.append(
            f"It takes {count} workspace project{'s' if count > 1 else ''} from PyPI, compared with this checkout:"
        )
        rows = []
        for p in report.published:
            version = f"PyPI {p.pypi_version}"
            if p.checkout_version and p.checkout_version != p.pypi_version:
                version += f", checkout {p.checkout_version}"
            if p.note:
                verdict = f"not compared: {p.note}"
            elif p.differing_files:
                verdict = f"behind: {_files(p.differing_files)}"
            else:
                verdict = "up to date"
            rows.append([p.project, version, verdict])
        lines += _table(rows)
    else:
        lines.append("It takes no workspace project from PyPI.")
    lines.append("")

    if report.name_clashes:
        lines.append("Names PyPI gives to other projects:")
        lines += [f"  {clash}" for clash in report.name_clashes]
        lines.append("")

    if not report.checked:
        lines.append(
            f"Not checked (--no-check): {target} was not installed in an empty venv, so nothing says these wheels import."
        )
    elif report.install_error is not None:
        lines.append(f"Installing {target} in an empty venv failed:")
        lines += [f"    {line}" for line in report.install_error.splitlines()]
    else:
        n_packages = len(report.packages)
        lines.append(
            f"Installed {target} in an empty venv (its wheels + PyPI) and imported every module of its "
            f"{n_packages} package{'s' if n_packages != 1 else ''}:"
        )
        n_imported = len(report.imported)
        modules = f"module{'s' if n_imported != 1 else ''}"
        if report.import_failures:
            rows = [
                [f.module, f.error + (f"  ({', '.join(f.blamed)})" if f.blamed else "")] for f in report.import_failures
            ]
            lines += _table(rows)
            lines.append(f"  {n_imported} other {modules} imported")
        elif report.import_warnings:
            lines.append(f"  {n_imported} {modules} imported")
        else:
            lines.append(f"  all {n_imported} {modules} imported")
        if report.import_warnings:
            lines.append("")
            lines.append("Not imported, missing what no workspace project ships (an optional extra's?):")
            lines += _table([[f.module, f.error] for f in report.import_warnings])
    lines.append("")

    if report.install_error is not None:
        if report.behind:
            lines.append(f"Behind: {', '.join(report.behind)}")
    elif report.checked:
        if report.release_first:
            lines.append(f"Release first: {', '.join(report.release_first)}")
        elif report.import_failures:
            lines.append(f"{target} does not import, and no project it takes from PyPI is the cause.")
        if report.behind_only:
            lines.append(f"Behind, nothing breaks: {', '.join(report.behind_only)}")
        if not report.import_failures and not report.behind:
            lines.append(f"PyPI is up to date for {target}.")
    tail = []
    if report.extra is not None:
        n_wheels = len(report.built)
        state = "written into" if report.extra_changed else "unchanged in"
        kept = ", which holds its generated block" if report.extra_remembered else ""
        tail.append(
            f"The extra {report.extra} ({n_wheels} wheel{'s' if n_wheels != 1 else ''}) {state} "
            f"{target}/pyproject.toml{kept}; install it with: uv pip install -e ./{target}[{report.extra}]"
        )
    if report.extra_left is not None:
        tail.append(
            f"The extra {report.extra_left} in {target}/pyproject.toml was left as it is: --include-project builds "
            f"{target}'s own wheel. Pass --extra {report.extra_left} to write it for these wheels."
        )
    if report.tracked_warning:
        tail.append(report.tracked_warning)
    if report.committed:
        tail.append(f"Committed {report.wheels_dir}: {report.committed}")
    elif report.commit_note:
        tail.append(report.commit_note[0].upper() + report.commit_note[1:])
    if report.zip_file:
        zip_name = Path(report.zip_file).name
        where = "any folder" if report.zip_standalone else f"the {target} folder"
        tail.append(
            f"Zip: {report.zip_file} ({human_size(report.zip_bytes)}) — install it from {where} with: python {zip_name}"
        )
    tail += _bundle_lines(report)
    if tail:
        lines += ["", *tail]
    return "\n".join(lines).rstrip() + "\n"


def _bundle_lines(report: WheelsReport) -> List[str]:
    """The bundle ``--bundle`` wrote: where it is, what it holds, how it installs and what its own check found."""
    bundle = report.bundle
    if bundle is None:
        return []
    count = len(bundle.projects)
    lines = [
        f"Bundle: {bundle.wheel} ({human_size(bundle.bytes)}) — {count} project{'s' if count != 1 else ''} in one wheel "
        f"({', '.join(bundle.projects)}); install it with: pip install {Path(bundle.wheel).name}"
    ]
    if not report.checked:
        return lines + ["  Not checked (--no-check): the bundle was not installed in an empty venv."]
    if report.bundle_install_error is not None:
        lines.append("  Installing the bundle alone in an empty venv failed:")
        return lines + [f"    {line}" for line in report.bundle_install_error.splitlines()]
    n_imported = len(report.bundle_imported)
    modules = f"module{'s' if n_imported != 1 else ''}"
    if not report.bundle_import_failures:
        return lines + [f"  Installed alone in an empty venv (it + PyPI): all {n_imported} {modules} imported"]
    lines.append("  Installed alone in an empty venv (it + PyPI); these did not import:")
    lines += ["  " + row for row in _table([[f.module, f.error] for f in report.bundle_import_failures])]
    return lines + [f"    {n_imported} other {modules} imported"]


def regenerate_command(
    target: str, scope: WheelsScope, include_project: bool = False, extra: Optional[str] = None
) -> str:
    """The command that wrote a generated file — its header's ``Generated by:`` and ``To regenerate:``."""
    own = _INCLUDE_PROJECT_FLAG if include_project else ""
    return f"aisland wheels build {target}{_SCOPE_FLAGS[scope]}{own}{f' --extra {extra}' if extra else ''}"


def provenance_header(prefix: str, command: str, source: str) -> List[str]:
    """The generator-provenance lines every generated file carries, each behind ``prefix``."""
    return [
        f"{prefix}AUTO-GENERATED FILE — DO NOT EDIT BY HAND",
        f"{prefix}Generated by: {command}",
        f"{prefix}Source: {source}",
        f"{prefix}To regenerate: {command}",
    ]
