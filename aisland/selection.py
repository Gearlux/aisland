"""Which optional extras this workspace installs — the shared two-layer selection machinery.

Workspace packages keep their core minimal and put optional capabilities behind EXTRAS.
That makes "install only what I use" expressible, but only if something decides WHICH.
There are two independent things to decide, and this module implements both from one
mechanism because the mechanism is identical and only the vocabulary differs:

* :data:`FRAMEWORKS` — the ML framework a machine TRAINS WITH (``recordstream[torch]``,
  ``sonair[lightning]`` / ``[keras]``). Nothing trains without one, so a workspace with no
  selection is broken.
* :data:`TRACKERS` — the experiment-tracking backend results GO TO (``matrainer[mlflow]`` /
  ``[aim]`` / ``[tensorboard]`` / ``[wandb]``).

They are **sibling axes, never merged**: what a machine trains with and where its results
go are unrelated choices, and folding them together would install a tracking client
because someone picked a training engine.

Each axis has two halves, deliberately separate:

* **A project DECLARES which of its extras belong to an axis**, in its own
  ``pyproject.toml``::

      [tool.aisland]
      frameworks = ["lightning", "keras"]     # what it can train with
      trackers   = ["mlflow", "aim"]          # what it can track to

  Declaring is required rather than guessed, because extras are a mixed bag — ``dev`` /
  ``http`` / ``viz`` / ``all`` sit in the same table as ``torch`` and ``mlflow``, and a
  name match would install a documentation extra on a machine that asked for a backend.
  Only IMPLEMENTED capabilities belong there: the declaration is the MENU, and the
  workspace's committed SELECTION decides what actually lands.

* **The workspace SELECTS names**, and each project installs the extras it declares that
  are selected. Names line up across projects on purpose (``keras`` is ``keras``
  everywhere), so ONE selection drives every project with no per-project mapping table.

Two configuration layers per axis, mirroring :mod:`aisland.sources` exactly:

* **committed** — the ``frameworks`` / ``trackers`` key of ``<workspace>/config.toml``
  (tracked, shared between machines, and the ONLY layer generated CI reads);
* **per-machine** — ``git config --local aisland.frameworks`` / ``aisland.trackers``
  (never committed).

The committed layer is ONE key in the workspace's single config file (:mod:`aisland.config`),
which this module neither formats nor owns — it hands a list of names to
:func:`aisland.config.update` and reads one back.

A machine-local choice must never reach a committed artifact: one developer narrowing to
``torch`` locally must not stop every runner from testing the Keras backend, a failure
that shows up as a green CI that no longer checks anything.

An UNSET selection is not an EMPTY one. Empty means "none, I mean it"; unset means nobody
has chosen yet, and :func:`resolve_for_setup` prompts (recording the answer) rather than
silently installing either everything or nothing.

:mod:`aisland.frameworks` is the framework-flavoured facade over this module, kept so the
long-standing ``*_frameworks`` call sites and their tests are unaffected.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

import click
from packaging.requirements import InvalidRequirement, Requirement

from aisland.config import CONFIG_FILE
from aisland.config import load as load_config
from aisland.config import update as update_config
from aisland.manager import get_project_metadata, get_workspace_root


@dataclass(frozen=True)
class Axis:
    """One selectable dimension: its vocabulary, its two config layers, and its wording.

    Args:
        name: Singular noun used in messages and as the CLI sub-command (``framework``).
        plural: The ``[tool.aisland]`` declaration key, this axis's key in the committed
            ``config.toml``, and the ``.git/config`` key suffix — one word drives all three
            so they cannot drift.
        question: The setup prompt's question line.
        hint: The line under it explaining what the selection does.
    """

    name: str
    plural: str
    question: str
    hint: str
    #: Where this axis's vocabulary comes from. ``False`` (the default) reads a project's
    #: ``[tool.aisland] <plural>`` declaration — the MENU a project opts into. ``True`` uses
    #: the project's own ``[project.optional-dependencies]`` keys instead, so the axis covers
    #: everything the project defines without the project declaring anything. Only the extras
    #: axis does the latter: "which of your extras do you want" has no menu to opt into, and
    #: nothing on it can be declared-but-undefined.
    from_defined_extras: bool = False

    @property
    def config_key(self) -> str:
        """Per-machine ``.git/config`` key, e.g. ``aisland.frameworks``."""
        return f"aisland.{self.plural}"

    @property
    def table_path(self) -> Tuple[str, ...]:
        """Where a project declares this axis, e.g. ``("tool", "aisland", "frameworks")``."""
        return ("tool", "aisland", self.plural)


FRAMEWORKS = Axis(
    name="framework",
    plural="frameworks",
    question="Which ML frameworks should this machine install?",
    hint="(workspace projects put each framework behind an extra; this selects them)",
)

TRACKERS = Axis(
    name="tracker",
    plural="trackers",
    question="Which experiment-tracking backends should this machine install?",
    hint="(the local-file backend always works; this selects the ones needing a library)",
)

EXTRAS = Axis(
    name="extra",
    plural="extras",
    question="Which extras should this machine install?",
    hint="(every extra the workspace's projects define — dev, local, platform, …; blank = all)",
    from_defined_extras=True,
)

#: Every axis, in the order setup resolves them. Extras comes first: it is the one axis that
#: always has a vocabulary (every project defines some extra), so it is the question an
#: operator expects first on a fresh workspace.
AXES: Tuple[Axis, ...] = (EXTRAS, FRAMEWORKS, TRACKERS)


# --------------------------------------------------------------------------- #
# What each project declares
# --------------------------------------------------------------------------- #


def declared(pyproject_path: Path, axis: Axis) -> List[str]:
    """The extras a project declares for ``axis``, in declaration order.

    A project with no declaration returns ``[]`` and is therefore unaffected by any
    selection — which is what keeps a new axis inert for the projects that ignore it.

    Example::

        declared(Path("sonair/pyproject.toml"), FRAMEWORKS)  # ["lightning", "keras"]

    On an axis whose vocabulary is the project's own extras (:attr:`Axis.from_defined_extras`)
    there is nothing to declare: every ``[project.optional-dependencies]`` key is in scope, so
    no project can be left out by forgetting a declaration.
    """
    if axis.from_defined_extras:
        return _selectable_extras(pyproject_path)
    try:
        data = tomllib.loads(pyproject_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    node: object = data
    for key in axis.table_path:
        if not isinstance(node, dict):
            return []
        node = node.get(key, {})
    if not isinstance(node, list):
        return []
    return [str(name) for name in node]


def declarations(workspace_root: Path, axis: Axis) -> Dict[str, List[str]]:
    """``{project name: declared extras}`` for every project declaring any on ``axis``.

    Discovery goes through :func:`aisland.manager.discover_projects`, so a project detached
    by ``aisland workspace configure source set … pypi`` (empty directory, no pyproject) drops out on its own.
    """
    from aisland.manager import discover_projects

    out: Dict[str, List[str]] = {}
    for pyproject in discover_projects(workspace_root):
        names = declared(pyproject, axis)
        if names:
            out[pyproject.parent.name] = names
    return out


def available(workspace_root: Path, axis: Axis) -> List[str]:
    """Every name declared by any project on ``axis``, sorted — the selectable vocabulary."""
    return sorted({name for names in declarations(workspace_root, axis).values() for name in names})


# --------------------------------------------------------------------------- #
# committed layer: the <plural> key of config.toml
# --------------------------------------------------------------------------- #


def read_committed(workspace_root: Path, axis: Axis) -> Optional[List[str]]:
    """The committed selection, or ``None`` when ``config.toml`` carries no such key.

    ``None`` (unset) is distinct from ``[]`` (explicitly none) — setup prompts for the
    former and honours the latter. That is why an ABSENT key, rather than an absent file, is
    what unset means now that three concerns share one file.
    """
    selected = load_config(workspace_root).get(axis.plural)
    if selected is None:
        return None
    return [str(name) for name in selected] if isinstance(selected, list) else []


def write_committed(workspace_root: Path, names: Sequence[str], axis: Axis) -> Path:
    """Record the committed selection in ``config.toml``, leaving every other section intact."""
    return update_config(workspace_root, axis.plural, list(names))


# --------------------------------------------------------------------------- #
# per-machine layer: .git/config aisland.<plural>
# --------------------------------------------------------------------------- #


def local_override(workspace_root: Path, axis: Axis) -> Optional[List[str]]:
    """The per-machine selection from ``.git/config``, or ``None`` when unset.

    Stored space-separated in ONE key rather than one key per name: the value is a SET
    being replaced wholesale, and a multi-key form would make "none selected"
    indistinguishable from "never chosen" — the very distinction this module is careful
    about.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--local", "--get", axis.config_key],
            cwd=workspace_root,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.split()


def set_local_override(workspace_root: Path, names: Optional[Sequence[str]], axis: Axis) -> None:
    """Set (or, with ``names=None``, remove) the per-machine selection."""
    if names is None:
        subprocess.run(
            ["git", "config", "--local", "--unset", axis.config_key],
            cwd=workspace_root,
            capture_output=True,
        )
        return
    subprocess.run(
        ["git", "config", "--local", axis.config_key, " ".join(names)],
        cwd=workspace_root,
        check=True,
        capture_output=True,
    )


# --------------------------------------------------------------------------- #
# the resolved answer
# --------------------------------------------------------------------------- #


def effective(workspace_root: Path, axis: Axis) -> Optional[List[str]]:
    """The selection that drives the venv: the per-machine override, else the committed one.

    ``None`` means nobody has chosen on this machine or in this repo — the caller decides
    what to do about it (setup prompts; CI uses the committed layer).
    """
    override = local_override(workspace_root, axis)
    return override if override is not None else read_committed(workspace_root, axis)


# --------------------------------------------------------------------------- #
# declarations that name an extra nobody defined
# --------------------------------------------------------------------------- #


class UndefinedDeclaration(NamedTuple):
    """One ``[tool.aisland]`` axis name with no matching ``[project.optional-dependencies]`` key."""

    project: str
    axis: str  #: the axis plural, e.g. "trackers"
    name: str  #: the declared extra that does not exist
    defined: List[str]  #: the extras the project DOES define

    def __str__(self) -> str:
        return f"{self.project}  {self.axis}  {self.name}  (defines: {', '.join(self.defined) or 'nothing'})"


def _defined_extras(pyproject_path: Path) -> List[str]:
    """The ``[project.optional-dependencies]`` keys of one pyproject, sorted."""
    try:
        data = tomllib.loads(pyproject_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return sorted(data.get("project", {}).get("optional-dependencies", {}))


def _canonical(name: str) -> str:
    """PEP 503 normalisation, so ``my-project`` and ``my_project`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _selectable_extras(pyproject_path: Path) -> List[str]:
    """The defined extras WITHOUT the self-referential ones — the extras axis's vocabulary.

    A project commonly defines a convenience extra that is nothing but its own other extras
    (``all = ["my-project[dev,local,tools]"]``). Offering
    it beside the extras it re-exports is not merely redundant, it is harmful: selecting both
    puts the distribution into its own requirement set, and uv then resolves the project a
    second time as an ordinary dependency. Observed 2026-10-20 on a project of another workspace, where
    selecting `all` beside the extras it re-exports pulled `platform-client==0.1.36` — whose
    URL dependency uv refuses outright — while the same list without `all` resolved 310
    packages cleanly on 0.1.38.

    The test is STRUCTURAL, not a name match: an extra is dropped when one of its requirements
    is the project's own distribution, whatever either is called. A project that happens to
    name an extra `all` while putting real packages in it keeps it; `everything = ["foo[dev]"]`
    in project `foo` is dropped. Name matching is what this module warns against elsewhere, and
    it would both miss the second case and break the first.
    """
    try:
        data = tomllib.loads(pyproject_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    project = data.get("project", {})
    me = _canonical(str(project.get("name", "")))
    out: List[str] = []
    for extra, requirements in sorted(project.get("optional-dependencies", {}).items()):
        if not me:
            out.append(extra)
            continue
        for raw in requirements:
            try:
                if _canonical(Requirement(str(raw)).name) == me:
                    break
            except InvalidRequirement:
                continue
        else:
            out.append(extra)
    return out


def undefined_declarations(workspace_root: Path, axes: Tuple[Axis, ...] = AXES) -> List[UndefinedDeclaration]:
    """Every axis name declared across the workspace that is not a real extra.

    :func:`extras_for` is set intersection and trusts the declaration, so a name declared
    here but never defined as an extra reaches uv, which warns and installs nothing --
    leaving ``aisland tracker set mlflow`` a silent no-op for that project. Checking the
    WHOLE workspace in one pass is deliberate: the operator gets every project to fix from
    one run instead of failing at the twelfth and going again.
    """
    from aisland.manager import discover_projects

    out: List[UndefinedDeclaration] = []
    for pyproject in discover_projects(workspace_root):
        extras = _defined_extras(pyproject)
        try:
            name = tomllib.loads(pyproject.read_text()).get("project", {}).get("name") or pyproject.parent.name
        except (OSError, tomllib.TOMLDecodeError):
            name = pyproject.parent.name
        for axis in axes:
            if axis.from_defined_extras:
                continue  # its vocabulary IS `extras`, so every name is defined by construction
            for declaration in declared(pyproject, axis):
                if declaration not in extras:
                    out.append(UndefinedDeclaration(name, axis.plural, declaration, extras))
    return sorted(out)


def require_defined_declarations(workspace_root: Path, axes: Tuple[Axis, ...] = AXES) -> None:
    """Refuse to install while any project declares an extra it never defined.

    User ruling 2026-08-25 (option B): stop and NAME the project and the name, rather than
    letting uv emit a warning nobody reads in the middle of a twenty-project install. Raised
    before anything is installed, from both entry points -- ``aisland setup`` and
    ``aisland tracker/framework set``.
    """
    offenders = undefined_declarations(workspace_root, axes)
    if not offenders:
        return
    # Align the columns: an operator scanning this list is looking for the project name.
    widths = [max(len(getattr(o, f)) for o in offenders) for f in ("project", "axis", "name")]
    lines = [
        f"  {o.project:<{widths[0]}}  {o.axis:<{widths[1]}}  {o.name:<{widths[2]}}"
        f"  (defines: {', '.join(o.defined) or 'nothing'})"
        for o in offenders
    ]
    headline = (
        "1 declaration names an extra that does not exist"
        if len(offenders) == 1
        else f"{len(offenders)} declarations name extras that do not exist"
    )
    raise click.ClickException(
        headline
        + ":\n\n"
        + "\n".join(lines)
        + "\n\nEach name above is listed under [tool.aisland] in that project's pyproject.toml\n"
        "but has no matching entry under [project.optional-dependencies]. Either define the\n"
        "extra there, or drop the name from the declaration -- until then, selecting it\n"
        "installs nothing for these projects."
    )


def extras_for(project_name: str, declared_names: Sequence[str], selected: Optional[Sequence[str]]) -> List[str]:
    """The extras to install for one project: what it declares ∩ what is selected.

    Order follows the project's DECLARATION, not the selection, so the emitted install
    target is stable regardless of the order names were typed in. An unset selection
    (``None``) yields nothing — callers that mean "everything" pass the full list
    explicitly, so the ambiguity never travels.

    Axis-free: it is set intersection, and both axes intersect identically.

    Example::

        extras_for("sonair", ["lightning", "keras"], ["keras", "torch"])  # ["keras"]
    """
    if not selected:
        return []
    chosen = set(selected)
    return [name for name in declared_names if name in chosen]


def all_extras(workspace_root: Path, pyproject_path: Path, project_name: str) -> List[str]:
    """Every selected extra for one project, ACROSS BOTH AXES.

    The install target must carry both, because a reinstall names the extras it wants and
    a target listing only one axis describes a package the operator did not ask for. A
    project declaring only frameworks is unaffected — the other axis contributes nothing.
    """
    out: List[str] = []
    for axis in AXES:
        out += extras_for(project_name, declared(pyproject_path, axis), effective(workspace_root, axis) or [])
    return out


def install_target(project_dir: Path, has_dev: bool, extras: Sequence[str]) -> str:
    """The ``uv pip install -e`` target for one project, e.g. ``/w/sonair[dev,lightning]``.

    Kept here rather than inlined at the call sites (the venv install and the generated CI
    lines) so both spell the extras list identically — they are compared by eye when a
    build disagrees with a local run.
    """
    parts = (["dev"] if has_dev and "dev" not in extras else []) + list(extras)
    return f"{project_dir}[{','.join(parts)}]" if parts else str(project_dir)


def editable_target(workspace_root: Path, project_dir: Path, project_name: str) -> str:
    """The ``uv pip install -e`` target for one CHECKOUT: its ``dev`` extra plus every selected
    extra on both axes — the ONE spelling ``aisland setup`` and ``aisland workspace configure source … local`` share.

    Before 2026-08-22 the source path installed the bare directory, so switching a project to
    ``local`` dropped ``[dev]`` (a dev-only test dependency vanished and its pin skipped) and a
    framework project lost the backends the machine had selected. A checkout with no
    ``pyproject.toml`` yields the bare directory so uv reports its own error for it.
    """
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        return str(project_dir)
    # Since 2026-10-20 the EXTRAS axis offers `dev` like any other extra. Once it has been
    # resolved its answer is the whole answer: forcing `[dev]` back in would make "everything
    # but dev" unexpressible, and the operator who deselected it would get it anyway. A
    # workspace that has never resolved that axis keeps the old unconditional `[dev]`.
    extras = all_extras(workspace_root, pyproject, project_name)
    has_dev = effective(workspace_root, EXTRAS) is None and bool(get_project_metadata(pyproject)["has_dev"])
    return install_target(project_dir, has_dev, extras)


# --------------------------------------------------------------------------- #
# the setup prompt
# --------------------------------------------------------------------------- #


def prompt_for(workspace_root: Path, choices: Sequence[str], axis: Axis) -> List[str]:
    """Ask which names to install, returning the chosen ones.

    A blank answer selects EVERYTHING, because the alternative default (nothing) hands
    back a workspace whose projects cannot do what they were installed for — a fresh setup
    should work before it is optimised. Answers are matched by name or by 1-based index, so
    both ``torch keras`` and ``1,3`` work.
    """
    click.echo(f"\n==> {axis.question}")
    click.echo(f"    {axis.hint}")
    decls = declarations(workspace_root, axis)
    for index, name in enumerate(choices, start=1):
        users = [p for p, names in decls.items() if name in names]
        click.echo(f"      [{index}] {name:<12} {', '.join(sorted(users))}")
    answer = click.prompt(
        "    Select (names or numbers, comma/space separated; blank = all)",
        default="",
        show_default=False,
    )
    return parse_selection(answer, choices, axis)


def parse_selection(answer: str, choices: Sequence[str], axis: Axis = FRAMEWORKS) -> List[str]:
    """Parse a prompt answer into names; blank means all of ``choices``.

    Unknown tokens are DROPPED with a warning rather than raising: this runs in the middle
    of a long ``aisland setup``, and aborting a provisioning run over a typo would cost more
    than proceeding with the names that were understood.
    """
    tokens = [tok for tok in answer.replace(",", " ").split() if tok]
    if not tokens:
        return list(choices)
    chosen: List[str] = []
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= len(choices):
            name = choices[int(token) - 1]
        elif token in choices:
            name = token
        else:
            click.echo(f"  - Warning: unknown {axis.name} {token!r}; ignoring.")
            continue
        if name not in chosen:
            chosen.append(name)
    return chosen


def resolve_for_setup(
    workspace_root: Path,
    requested: Optional[Sequence[str]],
    non_interactive: bool,
    axis: Axis = FRAMEWORKS,
    select_all: bool = False,
) -> Tuple[List[str], bool]:
    """The selection ``setup`` should install, plus whether it was just chosen.

    Precedence: ``select_all`` (``--all``) wins, then an explicit flag — both are RECORDED, so
    the next run needs no flag; else an existing selection is used as-is; else the operator is
    prompted, unless the run is non-interactive, in which case everything available is selected
    — a headless setup must not hang, and must not silently produce a workspace that cannot do
    its job.

    ``select_all`` is the whole vocabulary, computed from what the projects DEFINE. It does not
    look for an extra named ``all``: that convention is a project's own shorthand, several
    projects here spell it differently or not at all, and a flag that silently installed less
    on the project that omitted it would be worse than no flag.
    """
    choices = available(workspace_root, axis)
    if not choices:
        return [], False

    if select_all:
        write_committed(workspace_root, choices, axis)
        return choices, True

    if requested:
        chosen = parse_selection(" ".join(requested), choices, axis)
        write_committed(workspace_root, chosen, axis)
        return chosen, True

    existing = effective(workspace_root, axis)
    if existing is not None:
        return list(existing), False

    if non_interactive:
        click.echo(f"==> No {axis.name} selection recorded; installing all ({', '.join(choices)}).")
        write_committed(workspace_root, choices, axis)
        return choices, True

    chosen = prompt_for(workspace_root, choices, axis)
    write_committed(workspace_root, chosen, axis)
    click.echo(f"  - Recorded as `{axis.plural}` in {CONFIG_FILE}: {', '.join(chosen) or '(none)'}")
    return chosen, True


# --------------------------------------------------------------------------- #
# the CLI surface
# --------------------------------------------------------------------------- #


def describe(workspace_root: Optional[Path] = None, axis: Axis = FRAMEWORKS) -> List[Dict[str, str]]:
    """One row per declared name: who offers it, and whether each layer selects it.

    Rows are per NAME rather than per project because that is the unit an operator selects;
    the projects affected are shown as a column so the effect of a choice is visible without
    a second command.
    """
    root = workspace_root or get_workspace_root()
    decls = declarations(root, axis)
    committed = read_committed(root, axis)
    override = local_override(root, axis)
    resolved = effective(root, axis)

    def mark(selection: Optional[Sequence[str]], name: str) -> str:
        if selection is None:
            return "-"
        return "yes" if name in selection else "no"

    return [
        {
            axis.name: name,
            "committed": mark(committed, name),
            "override": mark(override, name),
            "effective": mark(resolved, name),
            "projects": ", ".join(sorted(p for p, names in decls.items() if name in names)),
        }
        for name in available(root, axis)
    ]


def apply_selection(
    workspace_root: Path,
    selected: Sequence[str],
    axis: Axis = FRAMEWORKS,
    uv_cmd: Optional[str] = None,
) -> int:
    """Reinstall every project declaring something on ``axis``, at the current selection.

    Only DECLARING projects are touched: reinstalling the whole workspace to change one
    extra would take minutes and rebuild things the selection cannot affect. Returns the
    number of projects that failed to install, so the CLI can exit non-zero.

    The install target carries BOTH axes' selected extras (:func:`all_extras`), not just
    this one's — a target naming only the axis being changed would describe a package the
    operator never asked for.

    Order comes from :func:`aisland.manager.get_install_order` — the same topological order
    ``aisland setup`` uses — NOT from discovery order. That is load-bearing rather than
    tidy: a consumer's extra can name a sibling's (``sonair[keras]`` requires
    ``matrainer[keras]>=0.1.0``), so installing the consumer first resolves it against the
    sibling's ALREADY-INSTALLED metadata and uv warns ``does not have an extra named
    'keras'`` while quietly installing without it. Observed on the first real run.

    uv does not UNINSTALL an extra that is no longer selected — dropping ``keras`` leaves
    the keras wheel in the venv until something removes it. Deliberate: guessing which
    packages "belonged" to an extra is unsound (they are ordinary dependencies, possibly
    shared), and a stale wheel costs disk, not correctness. Recreate the venv for a truly
    clean set.
    """
    from aisland.manager import ensure_uv_in_venv, get_install_order

    require_defined_declarations(workspace_root)

    uv = uv_cmd or ensure_uv_in_venv(workspace_root / ".venv" / "bin")
    failures = 0
    for project in get_install_order(workspace_root):
        pyproject = project["path"] / "pyproject.toml"
        if not declared(pyproject, axis):
            continue
        extras = extras_for(project["name"], declared(pyproject, axis), selected)
        other = [name for name in all_extras(workspace_root, pyproject, project["name"]) if name not in extras]
        target = install_target(project["path"], project["has_dev"], extras + other)
        click.echo(f"==> Installing {project['name']} ({target})...")
        try:
            subprocess.check_call([uv, "pip", "install", "-e", target], cwd=workspace_root)
        except subprocess.CalledProcessError as exc:
            click.echo(f"  - Error: Failed to install {project['name']}: {exc}")
            failures += 1
    return failures


def set_selection(
    names: Sequence[str],
    axis: Axis = FRAMEWORKS,
    machine_only: bool = False,
    apply: bool = True,
) -> int:
    """Record a selection and (by default) install it. Returns an exit code."""
    workspace_root = get_workspace_root()
    choices = available(workspace_root, axis)
    if not choices:
        click.echo(f"No project declares any {axis.name} extras ([tool.aisland] {axis.plural}); nothing to select.")
        return 1

    unknown = [name for name in names if name not in choices]
    if unknown:
        click.echo(f"Error: unknown {axis.name}(s) {', '.join(unknown)}. Available: {', '.join(choices)}")
        return 1

    chosen = list(dict.fromkeys(names))
    if machine_only:
        set_local_override(workspace_root, chosen, axis)
        click.echo(f"==> This machine only: {', '.join(chosen) or '(none)'}")
    else:
        path = write_committed(workspace_root, chosen, axis)
        click.echo(f"==> Recorded as `{axis.plural}` in {path.name}: {', '.join(chosen) or '(none)'}")
        if local_override(workspace_root, axis) is not None:
            click.echo("  - Note: a per-machine override is set and still WINS here.")
            click.echo(f"    Drop it with: aisland {axis.name} unset --machine-only")

    if not apply:
        return 0
    return apply_selection(workspace_root, effective(workspace_root, axis) or [], axis)


def unset_selection(axis: Axis = FRAMEWORKS, machine_only: bool = False, apply: bool = True) -> int:
    """Clear a selection: the per-machine override, or (default) the committed key too.

    Removing the KEY rather than the file is what keeps *unset* expressible now that the
    axes share ``config.toml`` with each other and with ``[sources.*]`` — the file survives
    for its other sections, and only vanishes when nothing at all is left in it.
    """
    workspace_root = get_workspace_root()
    set_local_override(workspace_root, None, axis)
    if machine_only:
        click.echo("==> Cleared this machine's override; the committed selection applies.")
    else:
        update_config(workspace_root, axis.plural, None)
        click.echo(f"==> Cleared the override and dropped `{axis.plural}` from {CONFIG_FILE}; setup will prompt again.")

    resolved = effective(workspace_root, axis)
    if resolved is None or not apply:
        return 0
    return apply_selection(workspace_root, resolved, axis)


# --------------------------------------------------------------------------- #
# a dev dependency that needs more than pip: playwright's browser
# --------------------------------------------------------------------------- #

#: The one browser a workspace test asks for — Playwright's package alone cannot open a page.
PLAYWRIGHT_BROWSER = "chromium"


def dev_extra_names(pyproject_data: Mapping[str, Any]) -> Set[str]:
    """The distribution names a parsed ``pyproject.toml`` lists under its ``dev`` extra, lower-cased.

    Each spec is read with ``packaging.Requirement``, so ``playwright>=1.40``, ``foo[bar]; python_version
    > '3'`` and ``x @ git+…`` all yield their bare name; a spec that does not parse is skipped rather
    than failing the read — declaring is the project's job, judging the spelling is uv's.
    """
    names: Set[str] = set()
    for spec in pyproject_data.get("project", {}).get("optional-dependencies", {}).get("dev", []) or []:
        try:
            names.add(Requirement(str(spec)).name.lower())
        except InvalidRequirement:
            continue
    return names


def browser_install_command(pyproject: Path, venv_bin: Path) -> Optional[List[str]]:
    """The command that fetches the browser a ``playwright`` DEV dependency needs, or ``None``.

    Listing ``playwright`` under ``dev`` is the whole declaration — the shape of the ``slow`` pytest
    marker, never an aisland knob. ``aisland setup`` runs this right after the project's editable
    install, and the scaffolded CI emits its equivalent after ``.[dev]``, so a browser-driven test
    runs everywhere the extra is installed instead of ``skip``-ping into a green build that checks
    less. A project without the dependency gets exactly what it always did.
    """
    if not pyproject.exists():
        return None
    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)
    if "playwright" not in dev_extra_names(data):
        return None
    return [str(venv_bin / "playwright"), "install", PLAYWRIGHT_BROWSER]
