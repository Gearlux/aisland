"""A ``[tool.aisland]`` axis name must exist as a real extra — and setup refuses when it doesn't.

``extras_for`` is set intersection: *what the project declares* ∩ *what the operator
selected*. It trusts the declaration, so a name that is declared but never defined as a
``[project.optional-dependencies]`` key used to travel intact to uv, which shrugged:

    warning: The package `distilaide @ file:///…/distilaide` does not have an extra named `mlflow`

Two lines in the middle of a twenty-project install is not a signal anyone acts on, and the
failure is silent in the way that matters: ``aisland tracker set mlflow`` reports success,
reinstalls the declaring projects, and changes nothing about their dependency closure. The
venv still HAS mlflow — pulled in by a different project that defines the extra properly —
so nothing looks broken until the project is installed on its own and the tracker is missing.
That is how distilaide, deltaid and raidar each carried
``trackers = ["mlflow", "tensorboard"]`` with neither extra defined for three weeks.

User ruling 2026-08-25, option B: **stop and name the project and the tracker.** The check
runs ONCE, up front, over the whole workspace — before anything is installed — so one run
lists every project to fix rather than failing at the twelfth and making the operator go
again. It guards both entry points: ``aisland setup`` and ``aisland tracker/framework set``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

import click
import pytest

from aisland.manager import get_workspace_root
from aisland.selection import (
    TRACKERS,
    UndefinedDeclaration,
    apply_selection,
    require_defined_declarations,
    undefined_declarations,
)

_TEMPLATE = """\
[project]
name = "{name}"
version = "0.1.0"

[project.optional-dependencies]
{extras}

[tool.aisland]
{declarations}
"""


def _project(tmp_path: Path, name: str, extras: Dict[str, str], **declarations: List[str]) -> Path:
    """Write <tmp>/<name>/pyproject.toml with the given extras and axis declarations."""
    directory = tmp_path / name
    directory.mkdir()
    directory.joinpath("pyproject.toml").write_text(
        _TEMPLATE.format(
            name=name,
            extras="\n".join(f'{key} = ["{value}"]' for key, value in extras.items()) or 'dev = ["pytest"]',
            declarations="\n".join(f"{k} = {v!r}".replace("'", '"') for k, v in declarations.items()),
        )
    )
    return tmp_path


# --------------------------------------------------------------------------
# finding them
# --------------------------------------------------------------------------


def test_a_declared_extra_that_exists_is_fine(tmp_path: Path) -> None:
    """PRO case: declare the tracker AND define the extra it names."""
    root = _project(
        tmp_path,
        "matrainer",
        {"mlflow": "mlflow>=2.0", "tensorboard": "tensorboard"},
        trackers=["mlflow", "tensorboard"],
    )
    assert undefined_declarations(root) == []


def test_a_declared_extra_that_does_not_exist_is_found(tmp_path: Path) -> None:
    """CON case: the drift — the tracker is declared, the extra was never written."""
    root = _project(tmp_path, "distilaide", {"torch": "torch"}, trackers=["mlflow", "tensorboard"])
    assert undefined_declarations(root) == [
        UndefinedDeclaration("distilaide", "trackers", "mlflow", ["torch"]),
        UndefinedDeclaration("distilaide", "trackers", "tensorboard", ["torch"]),
    ]


def test_declaring_nothing_is_fine(tmp_path: Path) -> None:
    """PRO case: no declaration at all, so no selection reaches the project."""
    assert undefined_declarations(_project(tmp_path, "sonair", {"torch": "torch"})) == []


def test_both_axes_are_checked(tmp_path: Path) -> None:
    """A frameworks declaration drifts exactly the way a trackers one does."""
    root = _project(tmp_path, "raidar", {"torch": "torch"}, frameworks=["torch", "keras"])
    assert [u.name for u in undefined_declarations(root)] == ["keras"]


# --------------------------------------------------------------------------
# refusing on them (option B)
# --------------------------------------------------------------------------


def test_a_clean_workspace_does_not_raise(tmp_path: Path) -> None:
    root = _project(tmp_path, "matrainer", {"mlflow": "mlflow>=2.0"}, trackers=["mlflow"])
    require_defined_declarations(root)  # must not raise


def test_the_refusal_names_the_project_and_the_tracker(tmp_path: Path) -> None:
    """The operator has to learn WHICH project and WHICH name, or the error is useless."""
    root = _project(tmp_path, "distilaide", {"torch": "torch"}, trackers=["mlflow"])
    with pytest.raises(click.ClickException) as caught:
        require_defined_declarations(root)
    message = caught.value.message
    assert "distilaide" in message
    assert "mlflow" in message
    assert "trackers" in message
    # and it must say what to DO about it, not just what is wrong
    assert "pyproject.toml" in message


def test_every_offender_is_listed_in_one_go(tmp_path: Path) -> None:
    """Fail-fast over the WHOLE workspace: one run names every project to fix, not the first."""
    _project(tmp_path, "distilaide", {"torch": "torch"}, trackers=["mlflow"])
    _project(tmp_path, "deltaid", {"torch": "torch"}, trackers=["tensorboard"])
    _project(tmp_path, "matrainer", {"mlflow": "mlflow>=2.0"}, trackers=["mlflow"])
    with pytest.raises(click.ClickException) as caught:
        require_defined_declarations(tmp_path)
    message = caught.value.message
    assert "distilaide" in message and "deltaid" in message
    assert "matrainer" not in message  # the clean project is not accused


def test_a_single_axis_can_be_checked_alone(tmp_path: Path) -> None:
    """`aisland tracker set` checks the tracker axis; a framework drift is not its business."""
    root = _project(tmp_path, "raidar", {"torch": "torch"}, frameworks=["keras"], trackers=["mlflow"])
    assert [u.name for u in undefined_declarations(root, (TRACKERS,))] == ["mlflow"]


def test_tracker_set_refuses_before_installing_anything(tmp_path: Path) -> None:
    """`aisland tracker set mlflow` must STOP -- not install a subset and warn about the rest.

    ``uv_cmd`` points at a path that does not exist: if the guard failed to fire, the call
    would get that far and die with a different error, so this pins WHICH check stopped it.
    """
    _project(tmp_path, "distilaide", {"torch": "torch"}, trackers=["mlflow"])
    with pytest.raises(click.ClickException) as caught:
        apply_selection(tmp_path, ["mlflow"], TRACKERS, uv_cmd="/nonexistent/uv")
    assert "distilaide" in caught.value.message
    assert "mlflow" in caught.value.message


# --------------------------------------------------------------------------
# the real workspace
# --------------------------------------------------------------------------


def test_the_real_workspace_defines_every_extra_it_declares() -> None:
    """The integration pin: `aisland tracker set X` reaches every project claiming X."""
    undefined = undefined_declarations(get_workspace_root())
    assert undefined == [], "\n".join(
        ["axis declarations naming extras that do not exist:"] + [str(u) for u in undefined]
    )
