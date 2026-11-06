"""What a machine installs of each project: the selections of ``aisland workspace configure <selection>``.

A project keeps its core minimal and puts optional capabilities behind extras; a SELECTION decides which of them a
machine installs. The projects declare the selections in their own ``pyproject.toml`` (read by
:mod:`aisland.workspace.discovery`, so aisland names none of them): the project whose table has ``question`` DEFINES the
selection, and every project's ``choices`` are its MENU, each an extra of its own::

    [tool.aisland.selection.framework]          # the project that asks
    question = "Which ML frameworks should this machine install?"
    choices = ["torch", "lightning"]

    [tool.aisland.selection.framework]          # any other project offering frameworks
    choices = ["keras", "torch"]

aisland adds one selection of its own, :data:`EXTRAS`, whose menu is every extra a project defines — "which of your
extras" has nothing to declare. Names line up across projects on purpose (``torch`` is ``torch`` everywhere), so ONE
choice drives every project: a project installs its menu ∩ the effective choice, in its menu's order.

Two layers, like every plugin of ``aisland workspace configure``:

* **committed** — ``[workspace] <selection> = [...]`` in ``workspace.toml``, shared, and the ONLY layer generated CI
  reads: one developer narrowing to ``torch`` must never stop every runner from testing the other backends;
* **this machine** — ``git config --local aisland.<selection>.selected`` (space-separated), which wins.

An UNSET selection is not an EMPTY one: empty means "none, deliberately"; unset means nobody has chosen, and
:func:`resolve_for_setup` asks (recording the answer) rather than silently installing everything or nothing.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from packaging.requirements import InvalidRequirement, Requirement

from aisland import console, failures, machine_config
from aisland import pyproject as pyproject_toml
from aisland.errors import AislandError
from aisland.venv import ensure_uv_in_venv
from aisland.workspace import discovery
from aisland.workspace import file as workspace_file
from aisland.workspace.projects import discover_projects, get_install_order, get_project_metadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Axis:
    """One selection: its name (the ``workspace.toml`` key, the plugin's name, ``aisland.<name>.selected``), the
    question setup asks, and the noun its messages use for one choice.

    ``from_defined_extras`` marks aisland's own :data:`EXTRAS`, whose menu is a project's extras rather than its
    declaration."""

    name: str
    question: str
    noun: str = ""
    from_defined_extras: bool = False

    @property
    def one(self) -> str:
        """The noun for one choice (``framework``, ``extra``)."""
        return self.noun or self.name


EXTRAS = Axis(
    name="extras", question="Which extras should this machine install?", noun="extra", from_defined_extras=True
)


def axes(root: Path) -> Tuple[Axis, ...]:
    """Every selection of the workspace: :data:`EXTRAS`, then each one a project defines, in discovery order.

    Raises :class:`aisland.workspace.discovery.DeclarationError`, naming the file and the line, for a declaration it cannot
    use (a choice that is no extra, a question asked twice)."""

    found = discovery.discover(root)
    return (EXTRAS,) + tuple(Axis(name, selection.question) for name, selection in found.selections.items())


# --- what each project offers -------------------------------------------------------------------------------------


def declared(pyproject_path: Path, axis: Axis) -> List[str]:
    """The choices one project offers for ``axis``, in its order; ``[]`` for a project offering none.

    For :data:`EXTRAS` that is every extra the project defines but a self-referential one."""
    if axis.from_defined_extras:
        return _selectable_extras(pyproject_path)
    table = (
        pyproject_toml.read(pyproject_path).get("tool", {}).get("aisland", {}).get("selection", {}).get(axis.name, {})
    )
    choices = table.get("choices", []) if isinstance(table, dict) else []
    return [str(name) for name in choices] if isinstance(choices, list) else []


def declarations(root: Path, axis: Axis) -> Dict[str, List[str]]:
    """``{project: its choices}`` for every project offering any for ``axis``.

    Through :func:`aisland.workspace.projects.discover_projects`, so a project with an empty folder (a ``pypi`` source) drops
    out on its own."""

    out: Dict[str, List[str]] = {}
    for pyproject in discover_projects(root):
        names = declared(pyproject, axis)
        if names:
            out[pyproject.parent.name] = names
    return out


def available(root: Path, axis: Axis) -> List[str]:
    """Every name any project offers for ``axis``, sorted: what can be chosen."""
    return sorted({name for names in declarations(root, axis).values() for name in names})


def manual(root: Path, axis: Axis) -> Set[str]:
    """The choices of ``axis`` installed only when they are named: a blank answer, ``-y`` and ``--all`` leave them out.

    A project marks them in ``[tool.aisland] manual-extras`` (checked by :mod:`aisland.workspace.discovery`) — an extra no
    machine should get by default, such as one pinning a dependency to a remote branch that would replace a local
    checkout of it. One project marking a name is enough: a selection is one name for every project. Only
    :data:`EXTRAS` has them."""
    if not axis.from_defined_extras:
        return set()

    out: Set[str] = set()
    for pyproject in discover_projects(root):
        names = pyproject_toml.read(pyproject).get("tool", {}).get("aisland", {}).get("manual-extras", [])
        out.update(str(name) for name in names if isinstance(names, list))
    return out


def _canonical(name: str) -> str:
    """PEP 503 normalisation, so ``my-project`` and ``my_project`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _selectable_extras(pyproject_path: Path) -> List[str]:
    """The defined extras WITHOUT the self-referential ones — the :data:`EXTRAS` menu.

    A project commonly defines a convenience extra that is nothing but its own other extras
    (``all = ["my-project[dev,local,tools]"]``). Selecting it beside the extras it re-exports puts the distribution
    into its own requirement set, and uv then resolves the project a second time as an ordinary dependency
    (2026-10-20: a project of another workspace pulled a release whose URL dependency uv refuses). The test is
    STRUCTURAL, not a name match: an extra is dropped when one of its requirements is the project's own
    distribution, whatever either is called.
    """
    project = pyproject_toml.read(pyproject_path).get("project", {})
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


# --- the two layers -----------------------------------------------------------------------------------------------


def read_committed(root: Path, name: str) -> Optional[List[str]]:
    """The committed choice — ``[workspace] <name>`` of ``workspace.toml`` — or ``None`` when it has none."""
    value = workspace_file.load(root).settings.get(name)
    if value is None:
        return None
    return [str(item) for item in value] if isinstance(value, list) else []


def write_committed(root: Path, name: str, names: Optional[Sequence[str]]) -> None:
    """Commit ``names`` (or, with ``None``, remove the key), keeping the file's projects and other settings."""
    current = workspace_file.load(root)
    settings = dict(current.settings)
    if names is None:
        settings.pop(name, None)
    else:
        settings[name] = list(names)
    workspace_file.write(root, dataclasses.replace(current, settings=settings))


def _machine_key(name: str) -> str:
    return f"aisland.{name}.selected"


def machine_choice(root: Path, name: str) -> Optional[List[str]]:
    """This machine's choice, or ``None`` when it has made none. One space-separated key: a set replaced whole,
    where ``""`` is "none" and an absent key "never chosen"."""
    chosen = machine_config.get(root, _machine_key(name))
    return None if chosen is None else chosen.split()


def set_machine_choice(root: Path, name: str, names: Optional[Sequence[str]]) -> None:
    """Record (or, with ``None``, forget) this machine's choice. Never committed."""
    machine_config.put(root, _machine_key(name), None if names is None else " ".join(names), check=True)


def effective(root: Path, name: str) -> Optional[List[str]]:
    """What drives this machine's venv: its own choice, else the committed one, else ``None``."""
    choice = machine_choice(root, name)
    return choice if choice is not None else read_committed(root, name)


# --- what a project installs --------------------------------------------------------------------------------------


def extras_for(declared_names: Sequence[str], selected: Optional[Sequence[str]]) -> List[str]:
    """What one project installs of one selection: its menu ∩ the choice, in the MENU's order, so the install
    target is stable however the names were typed. ``None`` (unset) yields nothing."""
    if not selected:
        return []
    chosen = set(selected)
    return [name for name in declared_names if name in chosen]


def all_extras(root: Path, pyproject_path: Path) -> List[str]:
    """Every extra one project installs, across every selection, each once."""
    out: List[str] = []
    for axis in axes(root):
        out += extras_for(declared(pyproject_path, axis), effective(root, axis.name))
    return list(dict.fromkeys(out))


def install_target(project_dir: Path, has_dev: bool, extras: Sequence[str]) -> str:
    """The ``uv pip install -e`` target for one project, e.g. ``/w/models[dev,keras]`` — one spelling for the venv
    and the generated CI, which are compared by eye when a build disagrees with a local run."""
    parts = (["dev"] if has_dev and "dev" not in extras else []) + list(extras)
    return f"{project_dir}[{','.join(parts)}]" if parts else str(project_dir)


def editable_target(root: Path, project_dir: Path, project_name: str) -> str:
    """The ``uv pip install -e`` target for one CHECKOUT: its ``dev`` extra plus every selected extra — the ONE
    spelling ``aisland setup`` and ``aisland workspace configure source … local`` share.

    Once :data:`EXTRAS` has been chosen it owns ``dev`` too: forcing ``[dev]`` back in would make "everything but
    dev" unexpressible. A workspace that never chose it keeps ``[dev]``. A checkout without a ``pyproject.toml``
    yields the bare folder, so uv reports its own error for it."""
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        return str(project_dir)
    has_dev = effective(root, EXTRAS.name) is None and bool(get_project_metadata(pyproject)["has_dev"])
    return install_target(project_dir, has_dev, all_extras(root, pyproject))


# --- setup asks ---------------------------------------------------------------------------------------------------


def prompt_for(root: Path, choices: Sequence[str], axis: Axis) -> List[str]:
    """Ask the selection's question; answers are names or 1-based numbers, and a blank one is EVERYTHING — a fresh
    setup should work before it is trimmed."""
    console.echo(f"\n==> {axis.question}")
    offered = declarations(root, axis)
    only_named = manual(root, axis)
    for index, name in enumerate(choices, start=1):
        users = [project for project, names in offered.items() if name in names]
        mark = " (only when named)" if name in only_named else ""
        console.echo(f"      [{index}] {name:<12} {', '.join(sorted(users))}{mark}")
    blank = "all but those only when named" if only_named & set(choices) else "all"
    answer = console.prompt(
        f"    Select (names or numbers, comma/space separated; blank = {blank})", default="", show_default=False
    )
    return parse_selection(answer, choices, axis, blank=[name for name in choices if name not in only_named])


def parse_selection(
    answer: str, choices: Sequence[str], axis: Axis, blank: Optional[Sequence[str]] = None
) -> List[str]:
    """An answer as names; blank is ``blank`` (all of ``choices`` when not given). An unknown word is DROPPED with a
    warning: this runs in the middle of a long setup, and stopping it over a typo would cost more than going on with
    what was understood."""
    tokens = [token for token in answer.replace(",", " ").split() if token]
    if not tokens:
        return list(choices if blank is None else blank)
    chosen: List[str] = []
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= len(choices):
            name = choices[int(token) - 1]
        elif token in choices:
            name = token
        else:
            failures.warn(f"  - Warning: unknown {axis.one} {token!r}; ignoring.")
            continue
        if name not in chosen:
            chosen.append(name)
    return chosen


def resolve_for_setup(
    root: Path, non_interactive: bool, axis: Axis, select_all: bool = False
) -> Tuple[List[str], bool]:
    """The choice setup installs, and whether it was just made.

    ``select_all`` (``setup --all``, for :data:`EXTRAS`) takes the whole menu and records it; else a recorded choice
    is used as it is; else setup asks — or, under ``-y``, takes everything, since a headless setup must neither hang
    nor leave a workspace that cannot do its job. "Everything" and a blank answer leave out the choices installed
    only when named (:func:`manual`). A new choice is committed."""
    choices = available(root, axis)
    if not choices:
        return [], False
    only_named = [name for name in choices if name in manual(root, axis)]
    everything = [name for name in choices if name not in only_named]
    if select_all:
        write_committed(root, axis.name, everything)
        return everything, True
    existing = effective(root, axis.name)
    if existing is not None:
        return list(existing), False
    if non_interactive:
        left_out = f"; only when named: {', '.join(only_named)}" if only_named else ""
        console.echo(f"==> Nothing chosen for {axis.name}; installing all ({', '.join(everything)}){left_out}.")
        chosen = everything
    else:
        chosen = prompt_for(root, choices, axis)
        console.echo(f"  - {axis.name} = {', '.join(chosen) or '(none)'} (in {workspace_file.FILE_NAME})")
    write_committed(root, axis.name, chosen)
    return chosen, True


# --- `aisland workspace configure <selection>` --------------------------------------------------------------------


def rows(root: Path, axis: Axis) -> List[Dict[str, str]]:
    """One row per name: whether each layer chooses it, and which projects offer it."""
    offered = declarations(root, axis)
    committed, machine, resolved = (
        read_committed(root, axis.name),
        machine_choice(root, axis.name),
        effective(root, axis.name),
    )

    def mark(chosen: Optional[Sequence[str]], name: str) -> str:
        return "-" if chosen is None else ("yes" if name in chosen else "no")

    return [
        {
            axis.name: name,
            "committed": mark(committed, name),
            "this machine": mark(machine, name),
            "effective": mark(resolved, name),
            "projects": ", ".join(sorted(project for project, names in offered.items() if name in names)),
        }
        for name in available(root, axis)
    ]


def apply_selection(root: Path, axis: Axis, uv_cmd: Optional[str] = None) -> int:
    """Reinstall every project offering something for ``axis``, at every selection's effective choice. Returns how
    many failed.

    Only the OFFERING projects are touched: reinstalling the workspace to change one extra would take minutes. The
    order is setup's (:func:`aisland.workspace.projects.get_install_order`): a consumer's extra can name a sibling's
    (``models[keras]`` requiring ``trainer[keras]``), and installed first it resolves against the sibling's OLD
    metadata. uv never uninstalls an extra no longer chosen — which packages "belonged" to it is unknowable, and a
    stale wheel costs disk, not correctness."""

    uv = uv_cmd or ensure_uv_in_venv(root / ".venv" / "bin")
    failures = 0
    for project in get_install_order(root):
        if not declared(project["path"] / "pyproject.toml", axis):
            continue
        target = editable_target(root, project["path"], project["name"])
        console.echo(f"==> Installing {project['name']} ({target})...")
        try:
            logger.debug("running %s pip install -e %s (in %s)", uv, target, root)
            subprocess.check_call([uv, "pip", "install", "-e", target], cwd=root)
        except subprocess.CalledProcessError as exc:
            console.echo(f"  - Error: Failed to install {project['name']}: {exc}", err=True)
            failures += 1
    return failures


def _command(axis: Axis, *words: str) -> str:
    return " ".join(("aisland workspace configure", axis.name) + words)


def set_selection(root: Path, axis: Axis, names: Sequence[str], machine_only: bool) -> int:
    """Commit (or, ``machine_only``, choose here) ``names`` and reinstall the projects offering them."""
    choices = available(root, axis)
    unknown = [name for name in names if name not in choices]
    if unknown:
        console.echo(
            f"Error: no project offers {axis.one} {unknown[0]!r} (the choices: {', '.join(choices) or 'none'})",
            err=True,
        )
        return 1
    _write_choice(root, axis, list(dict.fromkeys(names)), machine_only)
    return apply_selection(root, axis)


def _write_choice(root: Path, axis: Axis, chosen: Sequence[str], machine_only: bool) -> None:
    """Record ``chosen`` as the whole choice of one layer — this machine's, or the committed one — and say so."""
    shown = ", ".join(chosen) or "(none)"
    if machine_only:
        set_machine_choice(root, axis.name, chosen)
        console.echo(f"==> {axis.name} = {shown} on this machine (nothing committed)")
        return
    write_committed(root, axis.name, chosen)
    console.echo(f"==> {axis.name} = {shown} (in {workspace_file.FILE_NAME})")
    machine = machine_choice(root, axis.name)
    if machine is not None:
        console.echo(f"  - Note: this machine's choice ({', '.join(machine) or 'none'}) still wins here;")
        console.echo(f"    drop it with: {_command(axis, 'unset', '--machine-only')}")


def unset_selection(root: Path, axis: Axis, machine_only: bool, names: Sequence[str] = ()) -> int:
    """Forget this machine's choice (``machine_only``) or both layers', and reinstall at what is left.

    With ``names``, forget only those: they are dropped from the layer ``set`` would write — this machine's choice
    (which starts from the committed one when it has none) with ``machine_only``, else the committed one — and the
    rest stays. A name that layer does not choose is refused before anything changes."""
    if names:
        return _drop_names(root, axis, names, machine_only)
    set_machine_choice(root, axis.name, None)
    if machine_only:
        console.echo(f"==> {axis.name}: this machine follows {workspace_file.FILE_NAME} again")
    else:
        write_committed(root, axis.name, None)
        console.echo(f"==> {axis.name}: nothing chosen; setup asks again")
    if effective(root, axis.name) is None:
        return 0
    return apply_selection(root, axis)


def _drop_names(root: Path, axis: Axis, names: Sequence[str], machine_only: bool) -> int:
    """``unset NAME…``: the layer's choice without ``names``, and the offering projects reinstalled at it."""
    base = effective(root, axis.name) if machine_only else read_committed(root, axis.name)
    where = "on this machine" if machine_only else f"in {workspace_file.FILE_NAME}"
    if base is None:
        raise AislandError(f"no {axis.one} is chosen {where} yet")
    missing = [name for name in names if name not in base]
    if missing:
        raise AislandError(f"{axis.one} {missing[0]!r} is not chosen {where} (chosen: {', '.join(base) or 'none'})")
    _write_choice(root, axis, [name for name in base if name not in names], machine_only)
    return apply_selection(root, axis)


def add_command(root: Path, name: str, value: str) -> str:
    """The command that adds ``value`` to selection ``name``, keeping what this machine has chosen — ``set`` takes
    the WHOLE list, so naming only the missing one would drop the rest."""
    current = effective(root, name)
    if current is None:
        # An `extras` never chosen installs each project's `dev` (`editable_target`): keep it, or it goes.
        current = ["dev"] if name == EXTRAS.name else []
    return " ".join(["aisland workspace configure", name, "set", *current, *([] if value in current else [value])])


# --- the names a dev extra lists (read by playwright_browser and the CI scaffolder) -------------------------------


def dev_extra_names(pyproject_data: pyproject_toml.Table) -> Set[str]:
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
