"""The user docs: a short README that indexes the pages, pages whose links resolve, and no project of this workspace.

The README is what PyPI shows, where a relative link resolves nowhere, so every link in it is absolute; the pages
under ``docs/`` link each other relatively, and every link must land on a file and, with an anchor, on a heading
of it. A user page names no project of the workspace aisland is developed in — those mean nothing to a reader of
the package — but the tools aisland itself drives (process-compose, Jenkins, liquifai's completion, loggair's log
folder) keep their names. ``docs/architecture.md`` is the decision record, not a user page. The same holds for
what the installed package shows: every command's ``--help`` and the usage text of the zip ``aisland wheels zip``
writes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

import click
import pytest

from aisland import wheels_unzip
from aisland.cli import main
from aisland.services import registry

PROJECT = Path(__file__).resolve().parents[1]
README = PROJECT / "README.md"
DOCS = PROJECT / "docs"
EXAMPLES = PROJECT / "examples"
REPOSITORY = "https://github.com/Gearlux/aisland/blob/main/"

#: The projects of the workspace aisland is developed in, and the company names a published page never carries.
#: liquifai and loggair are not here: aisland drives them by name (completion, the editor's log folder).
WORKSPACE_NAMES = (
    "acme",
    "navigaitor",
    "sonair",
    "recordstream",
    "confluid",
    "matrainer",
    "raidar",
    "annotaide",
    "curaitor",
    "purifai",
    "streamstudio",
    "deltaid",
    "distilaide",
    "torpaido",
    "traidwind",
    "chainwind",
    "hydraide",
    "vendorcli",
    "company",
)
README_MAX_LINES = 250

_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")


def _user_pages() -> List[Path]:
    return sorted(page for page in DOCS.glob("*.md") if page.name != "architecture.md")


def _outside_code(text: str) -> Iterator[Tuple[int, str]]:
    """Each line that is not inside a fenced code block, with its number."""
    fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if not fence:
            yield number, line


def _anchors(page: Path) -> Set[str]:
    """GitHub's anchor for every heading of ``page``: lower case, punctuation dropped, spaces as hyphens."""
    seen: Dict[str, int] = {}
    anchors = set()
    for _, line in _outside_code(page.read_text(encoding="utf-8")):
        if not re.match(r"#{1,6} ", line):
            continue
        slug = re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _links(page: Path) -> Iterator[Tuple[int, str]]:
    for number, line in _outside_code(page.read_text(encoding="utf-8")):
        for target in _LINK.findall(line):
            yield number, target


def test_the_readme_is_a_short_quickstart() -> None:
    lines = len(README.read_text(encoding="utf-8").splitlines())
    assert lines <= README_MAX_LINES, f"README.md has {lines} lines; a topic belongs in docs/<topic>.md"


def test_every_link_in_the_readme_is_absolute_or_an_anchor_of_its_own() -> None:
    """A package index's page resolves no relative link."""
    anchors = _anchors(README)
    wrong = []
    for number, target in _links(README):
        if target.startswith("#"):
            if target[1:] not in anchors:
                wrong.append(f"README.md:{number}: no heading for {target}")
        elif not target.startswith(("https://", "http://", "mailto:")):
            wrong.append(f"README.md:{number}: {target} is relative")
    assert wrong == []


def test_every_repository_link_in_the_readme_names_a_file_that_exists() -> None:
    wrong = []
    for number, target in _links(README):
        if target.startswith(REPOSITORY):
            path, _, anchor = target[len(REPOSITORY) :].partition("#")
            if not (PROJECT / path).exists():
                wrong.append(f"README.md:{number}: {path} does not exist")
            elif anchor and anchor not in _anchors(PROJECT / path):
                wrong.append(f"README.md:{number}: {path} has no heading for #{anchor}")
    assert wrong == []


@pytest.mark.parametrize("page", _user_pages(), ids=lambda page: page.name)
def test_every_link_of_a_page_lands_on_a_file_and_a_heading(page: Path) -> None:
    wrong = []
    for number, target in _links(page):
        if target.startswith(("https://", "http://", "mailto:")):
            continue
        path, _, anchor = target.partition("#")
        landing = (page.parent / path).resolve() if path else page
        if not landing.exists():
            wrong.append(f"{page.name}:{number}: {path} does not exist")
        elif anchor and landing.suffix == ".md" and anchor not in _anchors(landing):
            wrong.append(f"{page.name}:{number}: {landing.name} has no heading for #{anchor}")
    assert wrong == []


def test_the_readme_indexes_every_page() -> None:
    text = README.read_text(encoding="utf-8")
    missing = [page.name for page in _user_pages() if f"{REPOSITORY}docs/{page.name}" not in text]
    assert missing == []


_WORKSPACE_NAME = re.compile(r"\b(" + "|".join(WORKSPACE_NAMES) + r")\b", re.IGNORECASE)

#: The user's choice (2026-10-29): the example of a project's service. The one place a page names a project of the
#: workspace, and only inside this link.
SERVICES_EXAMPLE_LINK = (
    "[matrainer's MLflow, Aim and ClearML servers]"
    "(https://github.com/Gearlux/matrainer/blob/main/docs/backends.md"
    "#the-tracking-servers-as-workspace-services-matrainerservices)"
)


def _commands(group: click.Group, path: Tuple[str, ...] = ("aisland",)) -> Iterator[Tuple[str, click.Command]]:
    """Every command under ``group``, the group included, with the words that call it."""
    yield " ".join(path), group
    for name, command in sorted(group.commands.items()):
        if isinstance(command, click.Group):
            yield from _commands(command, path + (name,))
        else:
            yield " ".join(path + (name,)), command


@pytest.mark.parametrize("page", [README, *_user_pages()], ids=lambda page: page.name)
def test_a_user_page_names_no_project_of_the_workspace(page: Path) -> None:
    pattern = _WORKSPACE_NAME
    text = page.read_text(encoding="utf-8")
    if page.name == "services.md":
        text = text.replace(SERVICES_EXAMPLE_LINK, "")
    hits = [
        f"{page.name}:{number}: {line.strip()}"
        for number, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line)
    ]
    assert hits == []


@pytest.mark.parametrize(
    "words, command", list(_commands(main)), ids=lambda value: value if isinstance(value, str) else ""
)
@pytest.mark.usefixtures("empty_workspace")
def test_a_commands_help_names_no_project_of_the_workspace(
    words: str, command: click.Command, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--help`` is the page a pip user reads first: its examples name ``myproject``, never this workspace's.

    The services the workspace's projects bring are left out: their lines name the project that brings each, which
    in any workspace is that workspace's own (`tests/test_plugin_services.py::test_its_help_names_the_project_that_brings_it`).
    """
    monkeypatch.setattr(registry, "declared", lambda root, reserved=(): [])
    text = command.get_help(click.Context(command, info_name=words))
    hits = [line.strip() for line in text.splitlines() if _WORKSPACE_NAME.search(line)]
    assert hits == []


def test_the_wheel_zips_usage_names_no_project_of_the_workspace() -> None:
    """The zip's ``__main__.py`` travels to machines without the workspace; its usage text says ``myproject``."""
    hits = [line.strip() for line in (wheels_unzip.__doc__ or "").splitlines() if _WORKSPACE_NAME.search(line)]
    assert hits == []


def test_every_example_is_shown_on_a_page() -> None:
    """An example is a page's runnable twin: the page names it."""
    pages = "\n".join(page.read_text(encoding="utf-8") for page in _user_pages())
    unnamed = [example.name for example in sorted(EXAMPLES.glob("*.py")) if f"examples/{example.name}" not in pages]
    assert unnamed == []


def test_every_command_is_shown_on_a_page() -> None:
    """A user finds every command — each verb of each group — in the README or a page, not only in `--help`. The
    command groups a project brings are its own docs' (they are not aisland's `main.commands`)."""
    from aisland.cli import main

    text = README.read_text(encoding="utf-8") + "".join(page.read_text(encoding="utf-8") for page in _user_pages())
    missing = [
        words
        for words, command in _commands(main)
        if not command.hidden and not re.search(re.escape(words) + r"(?![\w-])", text)
    ]
    assert not missing, "commands no page shows:\n" + "\n".join(missing)


def test_every_variable_aisland_reads_is_in_the_readme() -> None:
    """`env.complete` lists the variables a person may set (`# NAME=…`); the README's table names each."""
    listed = re.findall(r"^# ([A-Z][A-Z0-9_]+)=", (PROJECT / "env.complete").read_text(encoding="utf-8"), re.M)
    readme = README.read_text(encoding="utf-8")
    assert listed and not [name for name in listed if f"`{name}`" not in readme]
