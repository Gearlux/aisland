"""What a machine installs of each project: the selections of ``aisland workspace configure <selection>``.

A project keeps its core minimal and puts optional capabilities behind extras; a SELECTION decides which of them a
machine installs. The projects declare the selections in their own ``pyproject.toml`` (read by
:mod:`aisland.discovery`, so aisland names none of them): the project whose table has ``question`` DEFINES the
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

import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import click
from packaging.requirements import InvalidRequirement, Requirement

from aisland import workspace_file
from aisland.manager import get_project_metadata


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

    Raises :class:`aisland.discovery.DeclarationError`, naming the file and the line, for a declaration it cannot
    use (a choice that is no extra, a question asked twice)."""
    from aisland import discovery

    found = discovery.discover(root)
    return (EXTRAS,) + tuple(Axis(name, selection.question) for name, selection in found.selections.items())


def axis_named(root: Path, name: str) -> Optional[Axis]:
    """The selection called ``name``, or ``None``."""
    return {axis.name: axis for axis in axes(root)}.get(name)


# --- what each project offers -------------------------------------------------------------------------------------


def _read(pyproject_path: Path) -> Dict[str, Any]:
    try:
        return tomllib.loads(pyproject_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def declared(pyproject_path: Path, axis: Axis) -> List[str]:
    """The choices one project offers for ``axis``, in its order; ``[]`` for a project offering none.

    For :data:`EXTRAS` that is every extra the project defines but a self-referential one."""
    if axis.from_defined_extras:
        return _selectable_extras(pyproject_path)
    table = _read(pyproject_path).get("tool", {}).get("aisland", {}).get("selection", {}).get(axis.name, {})
    choices = table.get("choices", []) if isinstance(table, dict) else []
    return [str(name) for name in choices] if isinstance(choices, list) else []


def declarations(root: Path, axis: Axis) -> Dict[str, List[str]]:
    """``{project: its choices}`` for every project offering any for ``axis``.

    Through :func:`aisland.manager.discover_projects`, so a project with an empty folder (a ``pypi`` source) drops
    out on its own."""
    from aisland.manager import discover_projects

    out: Dict[str, List[str]] = {}
    for pyproject in discover_projects(root):
        names = declared(pyproject, axis)
        if names:
            out[pyproject.parent.name] = names
    return out


def available(root: Path, axis: Axis) -> List[str]:
    """Every name any project offers for ``axis``, sorted: what can be chosen."""
    return sorted({name for names in declarations(root, axis).values() for name in names})


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
    project = _read(pyproject_path).get("project", {})
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
    workspace_file.write(root, workspace_file.WorkspaceFile(settings, current.projects))


def _machine_key(name: str) -> str:
    return f"aisland.{name}.selected"


def machine_choice(root: Path, name: str) -> Optional[List[str]]:
    """This machine's choice, or ``None`` when it has made none. One space-separated key: a set replaced whole,
    where ``""`` is "none" and an absent key "never chosen"."""
    run = subprocess.run(
        ["git", "config", "--local", "--get", _machine_key(name)], cwd=root, capture_output=True, text=True
    )
    return run.stdout.split() if run.returncode == 0 else None


def set_machine_choice(root: Path, name: str, names: Optional[Sequence[str]]) -> None:
    """Record (or, with ``None``, forget) this machine's choice. Never committed."""
    if names is None:
        subprocess.run(["git", "config", "--local", "--unset", _machine_key(name)], cwd=root, capture_output=True)
        return
    subprocess.run(
        ["git", "config", "--local", _machine_key(name), " ".join(names)], cwd=root, check=True, capture_output=True
    )


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
    click.echo(f"\n==> {axis.question}")
    offered = declarations(root, axis)
    for index, name in enumerate(choices, start=1):
        users = [project for project, names in offered.items() if name in names]
        click.echo(f"      [{index}] {name:<12} {', '.join(sorted(users))}")
    answer = click.prompt(
        "    Select (names or numbers, comma/space separated; blank = all)", default="", show_default=False
    )
    return parse_selection(answer, choices, axis)


def parse_selection(answer: str, choices: Sequence[str], axis: Axis) -> List[str]:
    """An answer as names; blank is all of ``choices``. An unknown word is DROPPED with a warning: this runs in the
    middle of a long setup, and stopping it over a typo would cost more than going on with what was understood."""
    tokens = [token for token in answer.replace(",", " ").split() if token]
    if not tokens:
        return list(choices)
    chosen: List[str] = []
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= len(choices):
            name = choices[int(token) - 1]
        elif token in choices:
            name = token
        else:
            click.echo(f"  - Warning: unknown {axis.one} {token!r}; ignoring.")
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
    nor leave a workspace that cannot do its job. A new choice is committed."""
    choices = available(root, axis)
    if not choices:
        return [], False
    if select_all:
        write_committed(root, axis.name, choices)
        return choices, True
    existing = effective(root, axis.name)
    if existing is not None:
        return list(existing), False
    if non_interactive:
        click.echo(f"==> Nothing chosen for {axis.name}; installing all ({', '.join(choices)}).")
        chosen = choices
    else:
        chosen = prompt_for(root, choices, axis)
        click.echo(f"  - {axis.name} = {', '.join(chosen) or '(none)'} (in {workspace_file.FILE_NAME})")
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
    order is setup's (:func:`aisland.manager.get_install_order`): a consumer's extra can name a sibling's
    (``models[keras]`` requiring ``trainer[keras]``), and installed first it resolves against the sibling's OLD
    metadata. uv never uninstalls an extra no longer chosen — which packages "belonged" to it is unknowable, and a
    stale wheel costs disk, not correctness."""
    from aisland.manager import ensure_uv_in_venv, get_install_order

    uv = uv_cmd or ensure_uv_in_venv(root / ".venv" / "bin")
    failures = 0
    for project in get_install_order(root):
        if not declared(project["path"] / "pyproject.toml", axis):
            continue
        target = editable_target(root, project["path"], project["name"])
        click.echo(f"==> Installing {project['name']} ({target})...")
        try:
            subprocess.check_call([uv, "pip", "install", "-e", target], cwd=root)
        except subprocess.CalledProcessError as exc:
            click.echo(f"  - Error: Failed to install {project['name']}: {exc}")
            failures += 1
    return failures


def _command(axis: Axis, *words: str) -> str:
    return " ".join(("aisland workspace configure", axis.name) + words)


def set_selection(root: Path, axis: Axis, names: Sequence[str], machine_only: bool) -> int:
    """Commit (or, ``machine_only``, choose here) ``names`` and reinstall the projects offering them."""
    choices = available(root, axis)
    unknown = [name for name in names if name not in choices]
    if unknown:
        click.echo(
            f"Error: no project offers {axis.one} {unknown[0]!r} (the choices: {', '.join(choices) or 'none'})",
            err=True,
        )
        return 1
    chosen = list(dict.fromkeys(names))
    shown = ", ".join(chosen) or "(none)"
    if machine_only:
        set_machine_choice(root, axis.name, chosen)
        click.echo(f"==> {axis.name} = {shown} on this machine (nothing committed)")
    else:
        write_committed(root, axis.name, chosen)
        click.echo(f"==> {axis.name} = {shown} (in {workspace_file.FILE_NAME})")
        machine = machine_choice(root, axis.name)
        if machine is not None:
            click.echo(f"  - Note: this machine's choice ({', '.join(machine) or 'none'}) still wins here;")
            click.echo(f"    drop it with: {_command(axis, 'unset', '--machine-only')}")
    return apply_selection(root, axis)


def unset_selection(root: Path, axis: Axis, machine_only: bool) -> int:
    """Forget this machine's choice (``machine_only``) or both layers', and reinstall at what is left."""
    set_machine_choice(root, axis.name, None)
    if machine_only:
        click.echo(f"==> {axis.name}: this machine follows {workspace_file.FILE_NAME} again")
    else:
        write_committed(root, axis.name, None)
        click.echo(f"==> {axis.name}: nothing chosen; setup asks again")
    if effective(root, axis.name) is None:
        return 0
    return apply_selection(root, axis)


def add_command(root: Path, name: str, value: str) -> str:
    """The command that adds ``value`` to selection ``name``, keeping what this machine has chosen — ``set`` takes
    the WHOLE list, so naming only the missing one would drop the rest."""
    current = effective(root, name) or []
    return " ".join(["aisland workspace configure", name, "set", *current, *([] if value in current else [value])])


def missing_message(what: str, name: str, value: str) -> str:
    """What a service says when ``value`` of selection ``name`` (its backend, ``what``) is not installed: the
    command that adds it, keeping the rest. A service never installs its backend — a silent install leaves the venv
    holding something no selection asked for, gone again after the next ``aisland setup``."""
    from aisland import manager

    command = add_command(manager.get_workspace_root(), name, value)
    return (
        f"{what} is not installed in this environment.\n"
        f"  Add it to the {name} selection, so the choice is recorded and survives the next setup:\n"
        f"      {command}\n"
        "  Or, for this machine only:\n"
        f"      {command} --machine-only"
    )


# --- a dev dependency that needs more than pip: playwright's browser ----------------------------------------------

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
