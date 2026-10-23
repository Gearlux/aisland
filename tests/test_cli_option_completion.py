"""TAB on an empty word after a command that takes nothing more offers that command's options (aisland/cli.py,
`_Command`): click by itself offers them only once the word starts with `-`.

Each case asks the way the generated bash completion asks (`_AISLAND_COMPLETE=bash_complete`), through
`test_wheels._complete`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List, Tuple

import click
import pytest

from aisland import cli
from aisland.cli import main

from .test_wheels import _complete, _project, _workspace


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = _workspace(tmp_path, "vendorcli")
    _project(root, "vendorcli")
    return root


def _leaves(group: click.Group, path: Tuple[str, ...] = ()) -> Iterator[Tuple[Tuple[str, ...], click.Command]]:
    for name, command in sorted(group.commands.items()):
        if isinstance(command, click.Group):
            yield from _leaves(command, (*path, name))
        else:
            yield (*path, name), command


def _groups(group: click.Group) -> Iterator[click.Group]:
    yield group
    for command in group.commands.values():
        if isinstance(command, click.Group):
            yield from _groups(command)


BUILD_OPTIONS = [
    "--include-published",
    "--all",
    "--include-project",
    "--bundle",
    "--zip",
    "--commit",
    "--force",
    "--no-check",
    "--json",
    "--help",
]


def test_after_the_project_an_empty_word_offers_builds_options(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _complete(monkeypatch, root, "aisland wheels build vendorcli ") == BUILD_OPTIONS
    assert _complete(monkeypatch, root, "aisland wheels vendorcli ") == BUILD_OPTIONS, "the short form too"


def test_an_option_already_typed_is_not_offered_again(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    offered = _complete(monkeypatch, root, "aisland wheels build vendorcli --zip --force ")
    assert offered == [option for option in BUILD_OPTIONS if option not in ("--zip", "--force")]


def test_an_argument_still_to_fill_is_offered_before_any_option(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _complete(monkeypatch, root, "aisland wheels build ") == ["vendorcli"]


def test_a_group_still_offers_its_commands_only(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    offered = _complete(monkeypatch, root, "aisland ")
    assert "wheels" in offered and "doctor" in offered
    assert [item for item in offered if item.startswith("-")] == []


def test_every_command_without_arguments_offers_its_options_on_an_empty_word(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every aisland command: what an empty word offers is what `-` offers."""
    checked: List[str] = []
    for path, command in _leaves(main):
        if any(isinstance(param, click.Argument) for param in command.params):
            continue
        line = " ".join(("aisland", *path))
        empty = _complete(monkeypatch, root, f"{line} ")
        assert empty == _complete(monkeypatch, root, f"{line} -"), line
        assert "--help" in empty, line
        checked.append(line)
    assert "aisland doctor" in checked and len(checked) > 20


def test_every_group_makes_its_commands_and_subgroups_with_the_completing_classes() -> None:
    """A command added later gets the behaviour without anyone remembering it."""
    for group in _groups(main):
        assert isinstance(group, cli._Group), group.name
        for command in group.commands.values():
            assert isinstance(command, (cli._Group, cli._Command)), command.name
