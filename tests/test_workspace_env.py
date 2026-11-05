"""aisland reads the workspace `.env` the way the shell does: `envfile.read` is the one reader.

`project.bashrc` exports `.env` with `set -a; source .env; set +a`, so what a run sees is what bash makes of it. Two
readers disagreed with it and with each other: the services' kept the quotes of `KEY="a b"`, Jenkins' stripped
them, and neither expanded `$HOME`, read `export KEY=…` or dropped a trailing `# comment`. Each case here is checked
against bash itself.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict

import pytest

from aisland import envfile, locations

ENVIRONMENT = {"HOME": "/home/you", "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

CASES = {
    "plain": ("PLAIN=value", {"PLAIN": "value"}),
    "double quotes": ('SPACED="a b"', {"SPACED": "a b"}),
    "single quotes keep a dollar": ("LITERAL='$HOME/x'", {"LITERAL": "$HOME/x"}),
    "export": ("export EXPORTED=/data", {"EXPORTED": "/data"}),
    "a variable of the environment": ("DATA_ROOT=$HOME/data", {"DATA_ROOT": "/home/you/data"}),
    "a braced earlier line": ('A=x\nB="${A}-y"', {"A": "x", "B": "x-y"}),
    "an unset variable is empty": ("MISSING=$NO_SUCH_VARIABLE_X/y", {"MISSING": "/y"}),
    "a trailing comment": ("WITH_COMMENT=value # a note", {"WITH_COMMENT": "value"}),
    "a hash inside a word": ("COLOUR=a#b", {"COLOUR": "a#b"}),
    "equals in the value": ("TOKEN=abc=def", {"TOKEN": "abc=def"}),
    "an escaped quote": ('QUOTED="say \\"hi\\""', {"QUOTED": 'say "hi"'}),
    "empty": ("EMPTY=", {"EMPTY": ""}),
    "a later line wins": ("TWICE=1\nTWICE=2", {"TWICE": "2"}),
    "comments and blanks": ("# a comment\n\n   \n# HIDDEN=1\nSHOWN=1", {"SHOWN": "1"}),
}


def _bash(env_file: Path, names: Dict[str, str]) -> Dict[str, str]:
    """What `set -a; source .env` exports, for ``names``."""
    script = 'set -a; source "$1"; set +a; ' + "; ".join(
        f'printf "%s\\0" "{name}=${{{name}-__unset__}}"' for name in names
    )
    out = subprocess.run(
        ["bash", "-c", script, "bash", str(env_file)], env=ENVIRONMENT, capture_output=True, text=True, check=True
    ).stdout
    pairs = [item.split("=", 1) for item in out.split("\0") if item]
    return {name: value for name, value in pairs if value != "__unset__"}


@pytest.mark.parametrize("text, expected", CASES.values(), ids=list(CASES))
def test_the_reader_reads_what_bash_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, expected: Dict[str, str]
) -> None:
    (tmp_path / ".env").write_text(text + "\n")
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    assert _bash(tmp_path / ".env", expected) == expected  # the case says what bash does
    assert envfile.read(tmp_path) == expected


def test_a_line_bash_reads_as_no_assignment_sets_nothing(tmp_path: Path) -> None:
    """`KEY = value` runs a command named KEY in bash; `not-a-name=1` is no variable."""
    (tmp_path / ".env").write_text("SPACED_KEY = spaced value\nnot-a-name=1\nmalformed\nKEPT=1\n")
    assert envfile.read(tmp_path) == {"KEPT": "1"}


def test_a_missing_file_reads_as_nothing(tmp_path: Path) -> None:
    assert envfile.read(tmp_path) == {}


def test_the_reader_defaults_to_the_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "# comment\n"
        "\n"
        "LABELSTUDIO_TOKEN=abc123\n"
        "SPACED_KEY = spaced value\n"  # bash runs a command named SPACED_KEY: no assignment
        "malformed-line-without-equals\n"
        "DATA_ROOT=/data/gert\n"
    )
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    assert envfile.read() == {"LABELSTUDIO_TOKEN": "abc123", "DATA_ROOT": "/data/gert"}


def test_a_missing_file_at_the_workspace_root_reads_as_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    assert envfile.read() == {}


@pytest.mark.parametrize("value", ["plain", "a b", "$HOME/x", 'say "hi"', "it's", "", "a#b"])
def test_what_a_service_writes_reads_back_the_same_in_bash_and_in_aisland(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A service publishes what it decided (`envfile.write`); bash and the reader both get it back verbatim."""
    monkeypatch.setattr(locations, "workspace_root", lambda: tmp_path)
    (tmp_path / ".env").write_text("# mine\nOTHER=1\n")
    envfile.write({"PUBLISHED": value})
    assert _bash(tmp_path / ".env", {"PUBLISHED": value}) == {"PUBLISHED": value}
    assert envfile.read(tmp_path)["PUBLISHED"] == value
    assert (tmp_path / ".env").read_text().startswith("# mine\nOTHER=1\n")


def test_jenkins_reads_the_workspace_env_with_the_one_reader() -> None:
    from aisland.services import jenkins

    assert not hasattr(jenkins, "_parse_dotenv")
