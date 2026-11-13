"""The user docs: a short README that indexes the pages, pages whose links resolve, and no project of this workspace.

The README is what PyPI shows, where a relative link resolves nowhere, so every link in it is absolute; the pages
under ``docs/`` link each other relatively, and every link must land on a file and, with an anchor, on a heading
of it. A user page names no project of the workspace aisland is developed in — those mean nothing to a reader of
the package — but the tools aisland itself drives (process-compose, Jenkins, liquifai's completion, loggair's log
folder) keep their names. ``docs/architecture.md`` and the decision records under ``docs/decisions/`` are for
contributors, not user pages, but their links must land and they name no project either. The same holds for
what the installed package shows: every command's ``--help`` and the usage text of the zip ``aisland wheels zip``
writes.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

import click
import pytest

from aisland.ci import ci_templates
from aisland.cli import main
from aisland.services import registry
from aisland.wheels import install as wheels_install
from aisland.wheels import unzip as wheels_unzip
from tests import command_reference
from tests.private_names import PRIVATE, words_named

PROJECT = Path(__file__).resolve().parents[1]
README = PROJECT / "README.md"
DOCS = PROJECT / "docs"
EXAMPLES = PROJECT / "examples"
COMMANDS_PAGE = DOCS / "commands.md"
VERSION = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
#: Where the README's links point: the release's own tag, so the page PyPI shows for a release links to that
#: release's docs (GitHub's default branch may be ahead of it, and `main` may lack a page it names).
REPOSITORY = f"https://github.com/Gearlux/aisland/blob/v{VERSION}/"
_REPOSITORY_LINK = re.compile(r"https://github\.com/Gearlux/aisland/(?:blob|tree)/([^/)\s]+)/")

#: The projects of the workspace aisland is developed in, which a published page never names; the private names and
#: the company's are checked too, as digests (:data:`tests.private_names.PRIVATE`). liquifai and loggair are not
#: here: aisland drives them by name (completion, the editor's log folder).
WORKSPACE_NAMES = (
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
)
README_MAX_LINES = 250

_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")


def _user_pages() -> List[Path]:
    return sorted(page for page in DOCS.glob("*.md") if page.name != "architecture.md")


def _decision_records() -> List[Path]:
    return sorted((DOCS / "decisions").glob("*.md"))


def _contributor_pages() -> List[Path]:
    """The architecture page and every decision record."""
    return [DOCS / "architecture.md", *_decision_records()]


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


def _links_in(text: str) -> Iterator[Tuple[int, str]]:
    """Each link's target with the number of the line it is on, a link whose text runs over a line break included
    (the text outside code read whole, a fenced block's lines left blank)."""
    lines = dict(_outside_code(text))
    whole = "\n".join(lines.get(number, "") for number in range(1, max(lines, default=0) + 1))
    for found in _LINK.finditer(whole):
        yield whole.count("\n", 0, found.start(1)) + 1, found.group(1)


def _links(page: Path) -> Iterator[Tuple[int, str]]:
    return _links_in(page.read_text(encoding="utf-8"))


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


def test_the_readme_links_name_the_tag_of_the_version() -> None:
    """PyPI shows the README of a release: its links to the repository name the release's tag, `v<pyproject's
    version>`, so a version bump moves them."""
    refs = _REPOSITORY_LINK.findall(README.read_text(encoding="utf-8"))
    assert refs and set(refs) == {f"v{VERSION}"}, sorted(set(refs))


_CHANGELOG_ENTRY = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


def _entries_linking_another_tag(changelog: str, version: str) -> List[str]:
    """Each repository link of a CHANGELOG entry that names another tag than its own release's: an entry's text runs
    to the next entry (the text above the first is the first's), and an `## [Unreleased]` entry is ``version``'s."""
    starts = list(_CHANGELOG_ENTRY.finditer(changelog))
    wrong = []
    for index, entry in enumerate(starts):
        begin = entry.start() if index else 0
        end = starts[index + 1].start() if index + 1 < len(starts) else len(changelog)
        tag = "v" + (version if entry.group(1).lower() == "unreleased" else entry.group(1))
        wrong += [
            f"[{entry.group(1)}] links {ref}" for ref in _REPOSITORY_LINK.findall(changelog[begin:end]) if ref != tag
        ]
    return wrong


def test_each_changelog_entry_links_the_tag_of_its_own_release() -> None:
    """PyPI links the CHANGELOG of a release: an entry's links to the repository name ITS release's tag, so opening
    the next version moves the newest entry's links alone, and a released entry keeps pointing at the docs it
    described."""
    text = (PROJECT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert _REPOSITORY_LINK.search(text), "CHANGELOG.md links no page of the repository"
    assert _entries_linking_another_tag(text, VERSION) == []


def test_a_released_entry_keeps_its_tag_when_the_next_one_opens() -> None:
    page = "https://github.com/Gearlux/aisland/blob/{}/docs/a.md"
    changelog = (
        f"# Changelog\n\n## [0.2.0] - Unreleased\n\n- [a]({page.format('v0.2.0')})\n\n"
        f"## [0.1.0] - 2026-11-13\n\n- [a]({page.format('v0.1.0')}), [b]({page.format('v0.2.0')})\n"
    )
    assert _entries_linking_another_tag(changelog, "0.2.0") == ["[0.1.0] links v0.2.0"]
    assert _entries_linking_another_tag(f"## [Unreleased]\n\n- [a]({page.format('v0.2.0')})\n", "0.2.0") == []


def test_a_link_whose_text_runs_over_a_line_break_is_read() -> None:
    """A decision record's anchor link wrapped over two lines once pointed at no heading, unseen."""
    page = "Some text [a link that\n  runs on](#a-heading) and [one](other.md).\n```\n[not](code.md)\n```\n"
    assert list(_links_in(page)) == [(2, "#a-heading"), (2, "other.md")]


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


@pytest.mark.parametrize("page", [*_user_pages(), *_contributor_pages()], ids=lambda page: page.name)
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


def _title(page: Path) -> str:
    """The text of ``page``'s ``#`` heading."""
    return next(
        line[2:].strip() for _, line in _outside_code(page.read_text(encoding="utf-8")) if line.startswith("# ")
    )


def _index_entries() -> List[Tuple[str, str]]:
    """The ``(text, target)`` of each list item of the architecture page that links a decision record: its index."""
    page = DOCS / "architecture.md"
    return [
        (text, target)
        for _, line in _outside_code(page.read_text(encoding="utf-8"))
        for text, target in re.findall(r"^- \[(.+)\]\((decisions/[^)\s#]+)\)$", line)
    ]


def test_the_architecture_page_indexes_every_decision_record_once() -> None:
    """Every file under `docs/decisions/` once, and no entry for a file not there."""
    indexed = [target for _, target in _index_entries()]
    assert sorted(indexed) == sorted(f"decisions/{record.name}" for record in _decision_records())


def test_an_index_entry_names_its_record_by_number_and_title() -> None:
    wrong = [
        f"{text} -> {target}"
        for text, target in _index_entries()
        if text != f"{(DOCS / target).name[:4]} — {_title(DOCS / target)}"
    ]
    assert wrong == []


def test_the_decision_records_are_numbered_without_a_gap() -> None:
    numbers = [int(record.name[:4]) for record in _decision_records()]
    assert numbers == list(range(1, len(numbers) + 1))


#: A name not preceded by a letter or digit: `_labels_completion`, `planner_bundle-1.0.whl` and `datasets's` are
#: caught too (``\b`` stops at none of `_`, `-` or a plural `s`).
_WORKSPACE_NAME = re.compile(r"(?<![a-z0-9])(" + "|".join(WORKSPACE_NAMES) + r")", re.IGNORECASE)


def _names_a_project(line: str) -> bool:
    return bool(_WORKSPACE_NAME.search(line) or words_named(line, PRIVATE))


def _commands(group: click.Group, path: Tuple[str, ...] = ("aisland",)) -> Iterator[Tuple[str, click.Command]]:
    """Every command under ``group``, the group included, with the words that call it."""
    yield " ".join(path), group
    for name, command in sorted(group.commands.items()):
        if isinstance(command, click.Group):
            yield from _commands(command, path + (name,))
        else:
            yield " ".join(path + (name,)), command


@pytest.mark.parametrize("page", [README, *_user_pages(), *_contributor_pages()], ids=lambda page: page.name)
def test_a_user_page_names_no_project_of_the_workspace(page: Path) -> None:
    """The decision records too: they are published with the docs, and name a project they measured on by its role."""
    hits = [
        f"{page.name}:{number}: {line.strip()}"
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1)
        if _names_a_project(line)
    ]
    assert hits == []


@pytest.mark.parametrize(
    "page",
    [PROJECT / "CHANGELOG.md", *sorted(EXAMPLES.rglob("*.py"))],
    ids=lambda page: page.name,
)
def test_the_changelog_and_the_examples_name_no_project_of_the_workspace(page: Path) -> None:
    """The CHANGELOG is linked from PyPI and the examples are a page's runnable twin: both are read by anyone, so
    they follow the user pages (aisland rules → "Docs Never Name Dependent Projects")."""
    hits = [
        f"{page.name}:{number}: {line.strip()}"
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), start=1)
        if _names_a_project(line)
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
    hits = [line.strip() for line in text.splitlines() if _names_a_project(line)]
    assert hits == []


@pytest.mark.parametrize(
    "words, command", list(_commands(main)), ids=lambda value: value if isinstance(value, str) else ""
)
@pytest.mark.usefixtures("empty_workspace")
def test_a_commands_help_names_only_what_a_wheel_install_has(
    words: str, command: click.Command, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pip user has no `setup.sh`, and aisland's state is in the workspace's `.aisland/`: a help that names a
    folder of aisland's names the wheel's one too."""
    monkeypatch.setattr(registry, "declared", lambda root, reserved=(): [])
    text = " ".join(command.get_help(click.Context(command, info_name=words)).split())
    assert "setup.sh" not in text
    checkout_only = [hit for hit in re.findall(r"\S*aisland/data/\S*", text) if not hit.lstrip("`(").startswith(".")]
    assert not checkout_only or ".aisland/data/" in text, checkout_only


@pytest.mark.parametrize("page", ["ci.md", "stability.md"])
def test_every_fact_a_ci_template_reads_is_on_the_pages(page: str) -> None:
    """A template set's author reads the facts on `ci.md` and their promise on `stability.md`: a fact added to
    `ci_templates.Facts` is on both."""
    text = (DOCS / page).read_text(encoding="utf-8")
    missing = [fact.name for fact in dataclasses.fields(ci_templates.Facts) if f"`{fact.name}`" not in text]
    assert missing == []


def test_the_wheel_zips_usage_names_no_project_of_the_workspace() -> None:
    """The zip's ``__main__.py`` travels to machines without the workspace; its usage text says ``myproject``."""
    hits = [line.strip() for line in (wheels_unzip.__doc__ or "").splitlines() if _names_a_project(line)]
    assert hits == []


def test_the_install_scripts_usage_names_no_project_and_carries_no_dated_measurement() -> None:
    """``wheels/install.py`` is copied into every project's wheels folder: its usage said `--only` with two of this
    workspace's projects and a measurement dated with the day it was made."""
    text = wheels_install.__doc__ or ""
    hits = [line.strip() for line in text.splitlines() if _names_a_project(line)]
    assert hits == []
    assert re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text) == []


def test_every_example_is_shown_on_a_page() -> None:
    """An example is a page's runnable twin: the page names it."""
    pages = "\n".join(page.read_text(encoding="utf-8") for page in _user_pages())
    unnamed = [example.name for example in sorted(EXAMPLES.glob("*.py")) if f"examples/{example.name}" not in pages]
    assert unnamed == []


def test_every_command_is_shown_on_a_page() -> None:
    """A user finds every command — each verb of each group — in the README or a page, not only in `--help`. The
    command groups a project brings are its own docs' (they are not aisland's `main.commands`)."""
    from aisland.cli import main

    pages = [page for page in _user_pages() if page != COMMANDS_PAGE]  # the reference lists them all; a page explains
    text = README.read_text(encoding="utf-8") + "".join(page.read_text(encoding="utf-8") for page in pages)
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


@pytest.mark.usefixtures("empty_workspace")
def test_the_command_reference_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """`docs/commands.md` is what the click tree renders, in a workspace without projects (no service or selection a
    project brings); `AISLAND_WRITE_COMMANDS=1` writes it instead (`tests/command_reference.py`)."""
    monkeypatch.setattr(registry, "declared", lambda root, reserved=(): [])
    rendered = command_reference.render(main)
    if os.environ.get("AISLAND_WRITE_COMMANDS") == "1":
        COMMANDS_PAGE.write_text(rendered, encoding="utf-8")
    assert COMMANDS_PAGE.read_text(encoding="utf-8") == rendered, (
        "docs/commands.md is not what the commands' --help says: "
        "AISLAND_WRITE_COMMANDS=1 .venv/bin/pytest -n 0 tests/test_docs.py -k command_reference"
    )


@pytest.mark.usefixtures("empty_workspace")
def test_the_command_reference_lists_every_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "declared", lambda root, reserved=(): [])
    page = command_reference.render(main)
    missing = [words for words, command in _commands(main) if not command.hidden and f"`{words}`" not in page]
    assert missing == []
