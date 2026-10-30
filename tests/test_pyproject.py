"""``aisland.pyproject``: the one reader of a project's ``pyproject.toml`` — the tolerant ``read``, the refusing
``parse``, ``load`` with tomllib's own errors, and ``name``; and the refusals its callers print word for word."""

import tomllib
from pathlib import Path

import pytest

from aisland import discovery
from aisland import pyproject as pyproject_toml
from aisland import wheels_extra
from aisland.errors import AislandError

BROKEN = '[project]\nname = "app"\nversion = \n'


def _write(folder: Path, text: str) -> Path:
    path = folder / "pyproject.toml"
    path.write_text(text)
    return path


def test_load_reads_the_file_and_lets_tomllibs_errors_through(tmp_path: Path) -> None:
    good = _write(tmp_path, '[project]\nname = "app"\n')
    assert pyproject_toml.load(good) == {"project": {"name": "app"}}
    with pytest.raises(FileNotFoundError):
        pyproject_toml.load(tmp_path / "missing" / "pyproject.toml")
    with pytest.raises(tomllib.TOMLDecodeError):
        pyproject_toml.load(_write(tmp_path, BROKEN))


def test_read_answers_nothing_for_a_missing_unreadable_or_broken_file(tmp_path: Path) -> None:
    assert pyproject_toml.read(_write(tmp_path, "[tool.x]\ny = 1\n")) == {"tool": {"x": {"y": 1}}}
    assert pyproject_toml.read(tmp_path / "missing.toml") == {}
    assert pyproject_toml.read(tmp_path) == {}  # a folder: open() raises an OSError
    assert pyproject_toml.read(_write(tmp_path, BROKEN)) == {}


def test_parse_refuses_naming_the_file_and_the_line(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    assert pyproject_toml.parse('[project]\nname = "app"\n', path) == {"project": {"name": "app"}}
    with pytest.raises(pyproject_toml.PyprojectError) as raised:
        pyproject_toml.parse(BROKEN, path)
    assert str(raised.value) == f"{path}: Invalid value (at line 3, column 11)"
    assert isinstance(raised.value, AislandError) and isinstance(raised.value, ValueError)
    assert isinstance(raised.value.__cause__, tomllib.TOMLDecodeError)


@pytest.mark.parametrize(
    "text, expected",
    [
        ('[project]\nname = "app"\n', "app"),
        ('[project]\nname = ""\n', None),
        ("[project]\nname = 3\n", None),
        ('[project]\nversion = "1"\n', None),
        ('[tool.x]\ny = "z"\n', None),
        (BROKEN, None),
    ],
)
def test_name_is_the_project_name_or_none(tmp_path: Path, text: str, expected: object) -> None:
    assert pyproject_toml.name(_write(tmp_path, text)) == expected


def test_name_of_a_missing_file_is_none(tmp_path: Path) -> None:
    assert pyproject_toml.name(tmp_path / "pyproject.toml") is None


def test_discovery_still_refuses_a_broken_file_with_its_own_error_and_words(tmp_path: Path) -> None:
    path = _write(tmp_path, BROKEN)
    with pytest.raises(discovery.DeclarationError) as raised:
        discovery._read(path)
    assert str(raised.value) == f"{path}: Invalid value (at line 3, column 11)"
    assert isinstance(raised.value.__cause__, tomllib.TOMLDecodeError)


def test_wheels_extra_still_refuses_a_broken_file_naming_it(tmp_path: Path) -> None:
    path = _write(tmp_path, BROKEN)
    with pytest.raises(AislandError) as raised:
        wheels_extra.check_extra(path, "wheels", ["lib"])
    assert str(raised.value) == f"{path}: Invalid value (at line 3, column 11)"
