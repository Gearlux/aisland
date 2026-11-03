"""``machine_config``: this machine's settings in a repository's own config, read and written one way."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aisland import machine_config
from aisland.errors import AislandError


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def test_a_value_is_read_back_as_written(repo: Path) -> None:
    machine_config.put(repo, "aisland.demo.lib.choice", "dev/main")
    assert machine_config.get(repo, "aisland.demo.lib.choice") == "dev/main"


def test_a_key_that_is_not_set_reads_nothing_and_an_empty_value_is_a_value(repo: Path) -> None:
    assert machine_config.get(repo, "aisland.demo.lib.choice") is None
    machine_config.put(repo, "aisland.demo.lib.choice", "")
    assert machine_config.get(repo, "aisland.demo.lib.choice") == ""


def test_a_bool_is_written_as_git_writes_one_and_read_through_git(repo: Path) -> None:
    machine_config.put(repo, "aisland.demo.lib.on", True)
    machine_config.put(repo, "aisland.demo.app.on", False)
    assert machine_config.get(repo, "aisland.demo.lib.on") == "true"
    assert machine_config.get_bool(repo, "aisland.demo.lib.on") is True
    assert machine_config.get_bool(repo, "aisland.demo.app.on") is False
    assert machine_config.get_bool(repo, "aisland.demo.nobody.on") is None


def test_git_spellings_of_a_boolean_are_read_as_one(repo: Path) -> None:
    subprocess.run(["git", "config", "--local", "aisland.demo.lib.on", "yes"], cwd=repo, check=True)
    assert machine_config.get_bool(repo, "aisland.demo.lib.on") is True


def test_none_forgets_a_key_and_forgetting_one_that_is_not_set_is_no_error(repo: Path) -> None:
    machine_config.put(repo, "aisland.demo.lib.choice", "x")
    machine_config.put(repo, "aisland.demo.lib.choice", None)
    machine_config.put(repo, "aisland.demo.lib.choice", None)
    assert machine_config.get(repo, "aisland.demo.lib.choice") is None


def test_every_setting_matching_a_pattern_is_listed_with_its_value(repo: Path) -> None:
    machine_config.put(repo, "aisland.demo.lib.choice", "one two")
    machine_config.put(repo, "aisland.demo.app.choice", "three")
    machine_config.put(repo, "aisland.other.lib.choice", "elsewhere")
    assert machine_config.get_all(repo, r"^aisland\.demo\.") == {
        "aisland.demo.lib.choice": "one two",
        "aisland.demo.app.choice": "three",
    }
    assert machine_config.get_all(repo, r"^aisland\.nothing\.") == {}


def test_a_folder_that_is_no_repository_has_no_settings_and_a_write_there_is_silent(tmp_path: Path) -> None:
    assert machine_config.get(tmp_path, "aisland.demo.lib.choice") is None
    assert machine_config.get_bool(tmp_path, "aisland.demo.lib.choice") is None
    assert machine_config.get_all(tmp_path, r"^aisland\.") == {}
    machine_config.put(tmp_path, "aisland.demo.lib.choice", "x")
    assert machine_config.get(tmp_path, "aisland.demo.lib.choice") is None


def test_a_write_git_refuses_raises_when_asked_to_check(tmp_path: Path) -> None:
    with pytest.raises(AislandError, match="git config --local aisland.demo.lib.choice x failed"):
        machine_config.put(tmp_path, "aisland.demo.lib.choice", "x", check=True)
