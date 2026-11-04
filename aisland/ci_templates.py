"""The CI files of a project, rendered from a TEMPLATE SET: aisland's own, or one the workspace chooses.

``aisland jenkins scaffold`` works out the facts of each project (:class:`Facts`: its package, extras, slow tests,
the dependencies CI installs from git) and renders every file of the workspace's template set with them. A set is a
folder holding ``template.toml`` and Jinja2 templates:

* ``[files]`` maps each file the set writes, relative to the project, to its template — a set that names only a
  ``Jenkinsfile`` writes only that. A template no file names (``_*.jinja``) is a piece the others include.
* ``[variables]`` declares the set's own settings, ``name = { default = …, help = "…" }``; without a default the
  variable is required. A workspace sets them in ``workspace.toml``'s ``[ci.variables]``, a project in its own
  ``ci = { name = … }``.

The delimiters are ``<[ value ]>``, ``<% logic %>`` and ``<# comment #>``: Groovy's ``[[…]]`` lists of maps, GitHub's
``${{ … }}`` and the shell's ``[[ … ]]`` all appear in CI files, so Jinja's own braces would collide. A value the set
uses but nobody gave is refused (``StrictUndefined``) naming the template and its line.

Which set: ``workspace.toml``'s ``[ci] templates`` — a folder of the workspace, or the name an installed package
registers through the ``aisland.ci_templates`` entry point (its value: the package holding the set) — and a
project's own ``ci = { templates = … }`` before it; without either, aisland's set (:data:`BUILTIN`). aisland writes
each file's provenance header itself, so no template has to.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import shutil
import tomllib
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional

import jinja2

from aisland import locations
from aisland.errors import AislandError
from aisland.toml_places import Where, key_line, table_line
from aisland.workspace import file as workspace_file

logger = logging.getLogger(__name__)

#: The name of aisland's own set, and its folder in the package.
BUILTIN = "aisland"
BUILTIN_DIR = locations.PACKAGE_DIR / "templates" / "ci"
#: The entry point group through which an installed package registers a set: ``name = "<package holding it>"``.
ENTRY_POINT_GROUP = "aisland.ci_templates"
#: The file describing a set.
SET_FILE = "template.toml"
#: The key of ``[ci]`` and of a project's ``ci`` table naming the set; every other key of a project's ``ci`` is a
#: variable's value.
TEMPLATES_KEY = "templates"
VARIABLES_KEY = "variables"


class TemplateError(AislandError, ValueError):
    """A template set aisland cannot use, or a value it lacks; the message names the file and the line."""


@dataclass(frozen=True)
class Dependency:
    """A workspace project CI installs from its git repository before the project (it is not released)."""

    name: str
    url: str
    ref: str


@dataclass(frozen=True)
class Facts:
    """What aisland knows of a project, as a template reads it (``<[ package ]>``)."""

    #: The project's folder, its name shown in a title (``Models``), its distribution and its import package.
    project: str
    title: str
    distribution: str
    package: str
    #: The extras CI installs (``dev`` first), the browser Playwright needs (None: none), whether `slow` is a marker.
    extras: List[str]
    browser: Optional[str]
    slow_tests: bool
    #: The unreleased dependencies, in install order, and their owners as one phrase (``acme``, ``acme, labs``).
    unreleased: List[Dependency]
    unreleased_owners: str
    #: The GitHub owner whose private repositories CI clones with an app token, and its secrets' prefix (None: none).
    token_owner: Optional[str]
    token_secret: Optional[str]
    #: Each long stage's limit in minutes (Jenkins) and on a GitHub runner; the GitHub actions; the line a stage past
    #: its limit writes.
    stage_limits: Dict[str, int]
    runner_limits: Dict[str, int]
    actions: Dict[str, str]
    timed_out_message: str
    #: The workspace's name and the local Jenkins' address.
    workspace: str
    jenkins_url: str
    #: The project's folders holding a `package.json` with a `test` script (POSIX, relative, sorted; empty: none) —
    #: last and defaulted, so a set constructing the facts by position keeps working.
    node_tests: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class Variable:
    """One setting a set declares: its default (None: required) and what it is for."""

    default: object
    help: str


@dataclass(frozen=True)
class OutputFile:
    """A file a set writes: its path in the project, its template, and the comment its header is written in."""

    path: str
    template: str
    comment: Optional[str]


@dataclass(frozen=True)
class TemplateSet:
    """A set: its name (as ``[ci] templates`` names it), its folder, the files it writes and its variables."""

    name: str
    folder: Path
    files: List[OutputFile]
    variables: Dict[str, Variable] = field(default_factory=dict)


# --- finding a set ---------------------------------------------------------------------------------------------------


def _comment(path: str) -> Optional[str]:
    """The comment a file's header is written in, by its name: Groovy for a Jenkinsfile, ``#`` for YAML and TOML."""
    name = Path(path).name
    if name.startswith("Jenkinsfile") or name.endswith(".groovy"):
        return "//"
    if name.endswith((".yml", ".yaml", ".toml", ".sh", ".cfg", ".ini")):
        return "#"
    return None


def load_set(folder: Path, name: str) -> TemplateSet:
    """The set in ``folder``, checked: every file names a template the folder has and a comment aisland can write its
    header in. Raises :class:`TemplateError` naming ``template.toml`` and the line."""
    path = folder / SET_FILE
    if not path.is_file():
        raise TemplateError(f"{folder} holds no {SET_FILE}: it is no template set")
    text = path.read_text()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise TemplateError(f"{path}: {error}") from error
    files_header = table_line(text, "files")
    files = data.get("files")
    if not isinstance(files, dict) or not files:
        raise TemplateError(f"{Where(path, files_header)}: [files] must map each file the set writes to its template")
    outputs: List[OutputFile] = []
    for output, spec in files.items():
        where = Where(path, key_line(text, files_header, output) if files_header else None)
        template, comment = (spec, _comment(output)) if isinstance(spec, str) else (None, None)
        if isinstance(spec, dict):
            template, comment = spec.get("template"), spec.get("comment", _comment(output))
        if not isinstance(template, str) or not (folder / template).is_file():
            raise TemplateError(f"{where}: {output} names no template of {folder} ({template!r})")
        if comment is None:
            raise TemplateError(
                f"{where}: aisland cannot tell how {output} writes a comment — write {output!r} = "
                f'{{ template = "{template}", comment = "#" }}'
            )
        outputs.append(OutputFile(output, template, comment))
    variables: Dict[str, Variable] = {}
    declared = data.get("variables", {})
    variables_header = table_line(text, "variables")
    for variable, spec in declared.items() if isinstance(declared, dict) else []:
        if not isinstance(spec, dict) or not isinstance(spec.get("help", ""), str):
            where = Where(path, key_line(text, variables_header, variable) if variables_header else None)
            raise TemplateError(f'{where}: variable {variable!r} must be {{ default = …, help = "…" }}')
        variables[variable] = Variable(spec.get("default"), spec.get("help", ""))
    return TemplateSet(name, folder, outputs, variables)


def registered() -> Dict[str, Path]:
    """Every set an installed package registers (``aisland.ci_templates``): its name and its folder."""
    sets: Dict[str, Path] = {}
    for entry in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            module = importlib.import_module(entry.value)
        except ImportError:
            continue
        location = getattr(module, "__file__", None)
        if location:
            sets[entry.name] = Path(location).parent
    return sets


def find_set(root: Path, name: Optional[str], where: Optional[Where] = None) -> TemplateSet:
    """The set ``name`` names: none → aisland's; a folder of the workspace; a registered name. A name that is none
    of these is refused naming ``where`` (the line of ``workspace.toml`` that wrote it) and the sets there are."""
    if not name or name == BUILTIN:
        return load_set(BUILTIN_DIR, BUILTIN)
    folder = root / name
    if folder.is_dir():
        return load_set(folder, name)
    sets = registered()
    if name in sets:
        return load_set(sets[name], name)
    known = ", ".join(sorted({BUILTIN, *sets}))
    raise TemplateError(
        f"{where or root / workspace_file.FILE_NAME}: {TEMPLATES_KEY} = {name!r} is no folder of the workspace and no "
        f"template set an installed package registers (the sets: {known})"
    )


@dataclass(frozen=True)
class Choice:
    """The set a project's CI files come from and the variables it is rendered with."""

    template_set: TemplateSet
    variables: Dict[str, object]


def choice(root: Path, project: str) -> Choice:
    """The set and variables of ``project``: its own ``ci`` table first, then ``[ci]``, then the set's defaults. A
    project whose ``ci`` names a set of its own takes from ``[ci.variables]`` only what that set declares.

    Refused, naming ``workspace.toml`` and the line: a set that cannot be found, a variable the set does not declare,
    and one it requires that nobody set."""
    path = root / workspace_file.FILE_NAME
    current = workspace_file.load(root)
    text = path.read_text() if path.is_file() else ""
    ci_header = table_line(text, workspace_file.CI_SECTION)
    shared = dict(current.ci.get(VARIABLES_KEY) or {})
    listed = next((found for found in current.projects.values() if found.path == project), None)
    own = dict(listed.others.get(workspace_file.CI_SECTION) or {}) if listed else {}
    own_set = bool(own.get(TEMPLATES_KEY)) and own.get(TEMPLATES_KEY) != current.ci.get(TEMPLATES_KEY)
    if own.get(TEMPLATES_KEY):
        name, where = own.pop(TEMPLATES_KEY), listed.where if listed else None
        chooser = "named by the project's ci"
    else:
        own.pop(TEMPLATES_KEY, None)
        name = current.ci.get(TEMPLATES_KEY)
        where = Where(path, key_line(text, ci_header, TEMPLATES_KEY) if ci_header else None)
        chooser = f"named by [{workspace_file.CI_SECTION}] {TEMPLATES_KEY}" if name else "aisland's, nobody names one"
    template_set = find_set(root, name, where)
    if own_set:
        # [ci.variables] are the workspace's set's: a project on a set of its own takes the ones its set declares.
        shared = {variable: value for variable, value in shared.items() if variable in template_set.variables}
    for given, place in (
        (shared, f"[{workspace_file.CI_SECTION}.{VARIABLES_KEY}]"),
        (own, f"project {project!r}'s ci"),
    ):
        for variable in given:
            if variable not in template_set.variables:
                declared = ", ".join(sorted(template_set.variables)) or "none"
                raise TemplateError(
                    f"{path}: {place}: {variable} is no variable of the {template_set.name!r} template set "
                    f"(its variables: {declared})"
                )
    values = {name: variable.default for name, variable in template_set.variables.items()}
    values.update(shared)
    values.update(own)
    missing = [name for name, value in values.items() if value is None]
    if missing:
        needed = "; ".join(f"{name}: {template_set.variables[name].help}" for name in missing)
        raise TemplateError(
            f"{path}: the {template_set.name!r} template set needs {', '.join(missing)} — set it in "
            f"[{workspace_file.CI_SECTION}.{VARIABLES_KEY}] ({needed})"
        )
    logger.debug(
        "%s: template set %r (%s, %s), variables %s", project, template_set.name, template_set.folder, chooser, values
    )
    return Choice(template_set, values)


# --- rendering -------------------------------------------------------------------------------------------------------


def _environment(folder: Path) -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(folder)),
        variable_start_string="<[",
        variable_end_string="]>",
        block_start_string="<%",
        block_end_string="%>",
        comment_start_string="<#",
        comment_end_string="#>",
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )


def _template_line(error: BaseException, folder: Path) -> str:
    """``<template>:<line>`` of the innermost frame of ``error``'s traceback that is in one of the set's templates."""
    place = ""
    for frame in traceback.extract_tb(error.__traceback__):
        if frame.filename.startswith(str(folder)):
            place = f"{frame.filename}:{frame.lineno}"
    return place or str(folder)


def header(comment: str, project: str, template_set: TemplateSet, template: str) -> str:
    """The provenance header aisland writes above each file: what made it, from which template, how to remake it."""
    rule = f"{comment} " + "=" * 73
    return "\n".join(
        [
            rule,
            f"{comment} AUTO-GENERATED FILE — DO NOT EDIT BY HAND",
            f"{comment} Generated by: aisland jenkins scaffold {project}",
            f"{comment} Source template: {template} of the {template_set.name!r} template set",
            f"{comment} To regenerate:  aisland jenkins scaffold {project} --force",
            rule,
            "",
        ]
    )


def render(chosen: Choice, facts: Facts) -> Dict[str, str]:
    """Every file of the set, its header first, by its path in the project. Raises :class:`TemplateError` naming the
    template and its line for a template that does not parse or uses a value nobody gave."""
    environment = _environment(chosen.template_set.folder)
    values: Mapping[str, object] = {**asdict(facts), "variables": chosen.variables}
    rendered: Dict[str, str] = {}
    for output in chosen.template_set.files:
        try:
            body = environment.get_template(output.template).render(values)
        except jinja2.TemplateSyntaxError as error:
            raise TemplateError(f"{error.filename}:{error.lineno}: {error.message}") from error
        except jinja2.UndefinedError as error:
            raise TemplateError(f"{_template_line(error, chosen.template_set.folder)}: {error.message}") from error
        rendered[output.path] = (
            header(output.comment or "#", facts.project, chosen.template_set, output.template) + body
        )
    return rendered


def export(target: Path) -> List[Path]:
    """Copy aisland's own set into ``target`` (a new or empty folder) to start a set of one's own; the files copied."""
    if target.exists() and any(target.iterdir()):
        raise TemplateError(f"{target} is not empty: export aisland's template set into a new folder")
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for source in sorted(BUILTIN_DIR.iterdir()):
        if source.is_file():
            copied.append(Path(shutil.copy2(source, target / source.name)))
    return copied
