"""What aisland says and asks goes through `aisland.console`, and the console in charge answers.

In a terminal that is click, so a command prints and asks as it always did. The MCP server runs each tool inside
`console.collecting()`: the lines are kept, unstyled, apart by stream, and a question is refused — no one is there to
answer, and a tool must never act on a guessed answer.
"""

from __future__ import annotations

import ast
import threading
from typing import List

import click
import pytest

from aisland import console, failures
from tests import code_rules


def test_in_a_terminal_the_lines_and_questions_are_clicks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    console.echo("done")
    failures.warn("Warning: careful")
    out, err = capsys.readouterr()
    assert (out, err) == ("done\n", "Warning: careful\n")
    monkeypatch.setattr(click, "confirm", lambda question, default=False: question == "Go on?")
    assert console.confirm("Go on?") is True


def test_collected_lines_are_kept_unstyled_and_apart(capsys: pytest.CaptureFixture[str]) -> None:
    with console.collecting() as said:
        console.echo(click.style("==> Installing", bold=True))
        failures.warn("Warning: a\nsecond line")
    assert said.out == ["==> Installing"]
    assert said.err == ["Warning: a", "second line"]
    assert capsys.readouterr() == ("", "")
    console.echo("after")  # the terminal is back
    assert capsys.readouterr().out == "after\n"


def test_a_question_where_no_one_can_answer_is_refused() -> None:
    with console.collecting(), pytest.raises(console.NoOneToAsk, match="no one is here to answer: Delete it?"):
        console.confirm("Delete it?", default=True)
    with console.collecting(), pytest.raises(console.NoOneToAsk):
        console.prompt("Tunnel name")


def test_a_collector_belongs_to_its_own_thread(capsys: pytest.CaptureFixture[str]) -> None:
    """A tool call collecting in one thread changes nothing for another."""
    seen: List[str] = []

    def elsewhere() -> None:
        console.echo("from the other thread")
        seen.append("ran")

    with console.collecting() as said:
        thread = threading.Thread(target=elsewhere)
        thread.start()
        thread.join()
    assert seen == ["ran"]
    assert said.out == []
    assert capsys.readouterr().out == "from the other thread\n"


#: What only the CLI and the console call: click's own output and questions.
_CLICK_SPEECH = {"click.echo", "click.secho", "click.confirm", "click.prompt", "click.utils.echo"} | {
    f"click.termui.{name}" for name in ("echo", "secho", "confirm", "prompt")
}


def _speech(tree: ast.Module) -> List[int]:
    return [line for line, name in code_rules.references(tree, "click") if name in _CLICK_SPEECH]


def test_no_library_module_writes_or_asks_through_click_itself() -> None:
    """Only the CLI layer (`aisland/cli/`) and the console speak click; everything else goes through the console, or
    an MCP tool call would write onto the server's protocol and wait on a question no one can answer. It reads every
    use from the syntax tree, however click is imported (`click.echo(...)`, `c.confirm`, `from click import echo as
    say`)."""
    hits = [
        f"{path}:{line}"
        for path, tree in code_rules.library_modules()
        if path != "aisland/console.py"
        for line in _speech(tree)
    ]
    assert hits == []


@pytest.mark.parametrize(
    "source",
    [
        "import click\nclick.echo('x')\n",
        "import click as c\nc.confirm('Go on?')\n",
        "import click.termui\nclick.termui.prompt('Name')\n",
        "from click import secho as say\nsay('x')\n",
        "import click\nwrite = click.echo\n",
    ],
)
def test_a_use_of_clicks_speech_is_found_however_it_is_spelled(source: str) -> None:
    assert _speech(ast.parse(source)) != []


def test_clicks_style_and_a_patched_console_are_no_speech() -> None:
    """`click.style` stays anywhere; a name of the module's own called `echo` is no click."""
    source = "import click\nfrom aisland import console\nconsole.echo(click.style('x', bold=True))\n"
    assert _speech(ast.parse(source)) == []
