"""What a machine installs of each project: the selections (`aisland/selection.py`).

A project declares a selection in its own `pyproject.toml` — the project whose table has `question` DEFINES it, and
every project's `choices` are its menu, each an extra of its own (`aisland/discovery.py`)::

    [tool.aisland.selection.framework]
    question = "Which ML frameworks should this machine install?"
    choices = ["torch", "lightning"]

aisland adds one selection of its own, `extras`, whose menu is every extra a project defines. The chosen names are
committed in `workspace.toml` (`[workspace] framework = [...]`) and chosen per machine in `.git/config`
(`aisland.framework.selected`); a project installs its menu ∩ the effective choice. `aisland workspace configure
<selection>` lists and changes them; CI reads the committed layer only.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

import click
import pytest
from click.testing import CliRunner, Result

from aisland import cli, configure
from aisland import selection as s
from aisland import workspace_file
from aisland.discovery import DeclarationError
from aisland.manager import get_workspace_root
from tests.workspace_layout import needs_workspace

QUESTION = "Which ML frameworks should this machine install?"


def _project(
    root: Path,
    name: str,
    extras: List[str],
    selections: Optional[Dict[str, List[str]]] = None,
    questions: Optional[Dict[str, str]] = None,
) -> Path:
    """A project folder whose `pyproject.toml` defines ``extras`` and declares ``selections`` (name → choices)."""
    project = root / name
    project.mkdir(parents=True, exist_ok=True)
    lines = ["[project]", f'name = "{name}"', 'version = "0.1.0"', "", "[project.optional-dependencies]"]
    lines += [f'{extra} = ["some-package"]' for extra in extras]
    for selection, choices in (selections or {}).items():
        lines += ["", f"[tool.aisland.selection.{selection}]"]
        if questions and selection in questions:
            lines.append(f'question = "{questions[selection]}"')
        lines.append("choices = [" + ", ".join(f'"{choice}"' for choice in choices) + "]")
    (project / "pyproject.toml").write_text("\n".join(lines) + "\n")
    return project


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """trainer defines `framework` and `tracker`; models offers frameworks; plain declares nothing."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _project(
        tmp_path,
        "trainer",
        ["dev", "torch", "lightning", "mlflow", "aim"],
        {"framework": ["torch", "lightning"], "tracker": ["mlflow", "aim"]},
        {"framework": QUESTION, "tracker": "Which experiment-tracking backends should this machine install?"},
    )
    _project(tmp_path, "models", ["dev", "keras", "torch"], {"framework": ["keras", "torch"]})
    _project(tmp_path, "plain", ["dev"])
    monkeypatch.setattr(cli, "get_workspace_root", lambda: tmp_path)
    monkeypatch.setattr("aisland.manager.get_workspace_root", lambda: tmp_path)
    return tmp_path


def _axis(root: Path, name: str) -> s.Axis:
    return {axis.name: axis for axis in s.axes(root)}[name]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout.strip()


def _run(*args: str) -> Result:
    return CliRunner().invoke(cli.main, ["workspace", "configure", *args])


@pytest.fixture
def installs(monkeypatch: pytest.MonkeyPatch) -> List[List[str]]:
    """Every `uv pip install` a selection change runs, recorded instead of run."""
    calls: List[List[str]] = []
    monkeypatch.setattr("aisland.manager.ensure_uv_in_venv", lambda venv_bin: "uv")

    def record(cmd: List[str], **kw: object) -> int:
        calls.append(list(cmd))
        return 0

    monkeypatch.setattr(s.subprocess, "check_call", record)
    return calls


# --- what the projects declare ------------------------------------------------------------------------------------


def test_the_selections_are_extras_then_what_the_projects_declare(workspace: Path) -> None:
    assert [axis.name for axis in s.axes(workspace)] == ["extras", "framework", "tracker"]
    assert _axis(workspace, "framework").question == QUESTION


def test_a_selections_menu_is_every_projects_choices(workspace: Path) -> None:
    framework = _axis(workspace, "framework")
    assert s.declarations(workspace, framework) == {"trainer": ["torch", "lightning"], "models": ["keras", "torch"]}
    assert s.available(workspace, framework) == ["keras", "lightning", "torch"]


def test_choices_for_a_selection_no_project_defines_offer_nothing(tmp_path: Path) -> None:
    """Without the project that asks the question there is no such selection; the workspace still sets up."""
    _project(tmp_path, "models", ["dev", "keras"], {"framework": ["keras"]})
    assert [axis.name for axis in s.axes(tmp_path)] == ["extras"]
    assert s.editable_target(tmp_path, tmp_path / "models", "models").endswith("models[dev]")


def test_a_choice_that_is_no_extra_is_refused_naming_its_file_and_line(tmp_path: Path) -> None:
    _project(tmp_path, "trainer", ["torch"], {"framework": ["torch"]}, {"framework": QUESTION})
    _project(tmp_path, "models", ["torch"], {"framework": ["torch", "mlx"]})
    with pytest.raises(DeclarationError) as caught:
        s.axes(tmp_path)
    assert str(caught.value).startswith(f"{tmp_path / 'models' / 'pyproject.toml'}:8: models offers 'mlx'")


# --- aisland's own selection: extras ------------------------------------------------------------------------------


def test_the_extras_menu_is_every_extra_a_project_defines(workspace: Path) -> None:
    assert s.declared(workspace / "models" / "pyproject.toml", s.EXTRAS) == ["dev", "keras", "torch"]
    assert s.available(workspace, s.EXTRAS) == ["aim", "dev", "keras", "lightning", "mlflow", "torch"]


def test_each_project_gets_only_the_extras_it_defines(workspace: Path) -> None:
    s.write_committed(workspace, "extras", ["dev", "keras", "aim"])
    assert s.editable_target(workspace, workspace / "models", "models").endswith("models[dev,keras]")
    assert s.editable_target(workspace, workspace / "plain", "plain").endswith("plain[dev]")


def test_a_resolved_extras_selection_owns_dev(workspace: Path) -> None:
    """Once extras is chosen, `dev` comes only from it: "everything but dev" must be expressible."""
    s.write_committed(workspace, "extras", ["keras"])
    assert s.editable_target(workspace, workspace / "plain", "plain") == str(workspace / "plain")


def test_an_unresolved_extras_selection_keeps_dev(workspace: Path) -> None:
    assert s.editable_target(workspace, workspace / "plain", "plain").endswith("plain[dev]")


def test_dev_is_never_listed_twice(workspace: Path) -> None:
    assert s.install_target(workspace / "plain", True, ["dev", "local"]).endswith("plain[dev,local]")


def test_a_self_referential_extra_is_not_offered(tmp_path: Path) -> None:
    project = _project(tmp_path, "tools", ["dev", "local"])
    text = (project / "pyproject.toml").read_text()
    (project / "pyproject.toml").write_text(
        text.replace(
            "[project.optional-dependencies]\n", '[project.optional-dependencies]\nall = ["tools[dev,local]"]\n'
        )
    )
    assert s.declared(project / "pyproject.toml", s.EXTRAS) == ["dev", "local"]


def test_the_self_reference_test_is_structural_not_a_name_match(tmp_path: Path) -> None:
    project = _project(tmp_path, "my_tools", ["dev"])
    text = (project / "pyproject.toml").read_text()
    extra = 'all = ["numpy"]\neverything = ["My-Tools[dev]"]\n'
    (project / "pyproject.toml").write_text(
        text.replace("[project.optional-dependencies]\n", "[project.optional-dependencies]\n" + extra)
    )
    assert s.declared(project / "pyproject.toml", s.EXTRAS) == ["all", "dev"]


# --- the two layers -----------------------------------------------------------------------------------------------


def test_the_committed_choice_is_in_workspace_toml_under_the_selections_name(workspace: Path) -> None:
    s.write_committed(workspace, "framework", ["torch", "keras"])
    s.write_committed(workspace, "tracker", ["mlflow"])
    assert workspace_file.load(workspace).settings == {"framework": ["torch", "keras"], "tracker": ["mlflow"]}
    assert (
        '[workspace]\nframework = ["torch", "keras"]\ntracker = ["mlflow"]\n'
        in (workspace / "workspace.toml").read_text()
    )
    assert not (workspace / "config.toml").exists()


def test_writing_a_selection_keeps_the_projects_and_the_other_selections(workspace: Path) -> None:
    (workspace / "workspace.toml").write_text(
        '[workspace]\ntracker = ["aim"]\n\n[project."models"]\nurl = "https://example.com/models.git"\nbranch = "main"\n'
    )
    s.write_committed(workspace, "framework", ["torch"])
    listed = workspace_file.load(workspace)
    assert listed.settings == {"tracker": ["aim"], "framework": ["torch"]}
    assert listed.projects["models"].url == "https://example.com/models.git"


def test_unset_is_not_empty(workspace: Path) -> None:
    assert s.read_committed(workspace, "framework") is None
    s.write_committed(workspace, "framework", [])
    assert s.read_committed(workspace, "framework") == []
    s.write_committed(workspace, "framework", None)
    assert s.read_committed(workspace, "framework") is None


def test_this_machines_choice_is_aislands_key_and_wins(workspace: Path) -> None:
    s.write_committed(workspace, "framework", ["torch", "lightning"])
    before = (workspace / "workspace.toml").read_text()
    s.set_machine_choice(workspace, "framework", ["keras"])
    assert _git(workspace, "config", "--local", "--get", "aisland.framework.selected") == "keras"
    assert s.effective(workspace, "framework") == ["keras"]
    assert (workspace / "workspace.toml").read_text() == before


def test_an_empty_machine_choice_is_honoured(workspace: Path) -> None:
    s.write_committed(workspace, "framework", ["torch"])
    s.set_machine_choice(workspace, "framework", [])
    assert s.effective(workspace, "framework") == []


def test_forgetting_the_machine_choice_falls_back_to_the_committed_one(workspace: Path) -> None:
    s.write_committed(workspace, "framework", ["torch"])
    s.set_machine_choice(workspace, "framework", ["keras"])
    s.set_machine_choice(workspace, "framework", None)
    assert s.effective(workspace, "framework") == ["torch"]


# --- what a project installs --------------------------------------------------------------------------------------


def test_a_project_installs_dev_and_every_selections_extras_in_its_declarations_order(workspace: Path) -> None:
    s.write_committed(workspace, "framework", ["torch", "lightning", "keras"])
    s.write_committed(workspace, "tracker", ["aim"])
    assert s.editable_target(workspace, workspace / "trainer", "trainer").endswith("trainer[dev,torch,lightning,aim]")
    assert s.editable_target(workspace, workspace / "models", "models").endswith("models[dev,keras,torch]")


def test_a_project_declaring_nothing_installs_dev_only(workspace: Path) -> None:
    s.write_committed(workspace, "framework", ["torch"])
    assert s.editable_target(workspace, workspace / "plain", "plain").endswith("plain[dev]")


def test_without_a_pyproject_the_target_is_the_bare_folder(workspace: Path) -> None:
    (workspace / "empty").mkdir()
    assert s.editable_target(workspace, workspace / "empty", "empty") == str(workspace / "empty")


# --- setup asks ---------------------------------------------------------------------------------------------------


def test_setup_asks_the_selections_own_question(workspace: Path) -> None:
    framework = _axis(workspace, "framework")
    runner = CliRunner()

    @click.command()
    def ask() -> None:
        click.echo(repr(s.resolve_for_setup(workspace, False, framework)))

    result = runner.invoke(ask, input="torch\n")
    assert result.output.splitlines()[:5] == [
        "",
        f"==> {QUESTION}",
        "      [1] keras        models",
        "      [2] lightning    trainer",
        "      [3] torch        models, trainer",
    ]
    assert "(['torch'], True)" in result.output
    assert s.read_committed(workspace, "framework") == ["torch"]


def test_setup_uses_a_recorded_choice_without_asking(workspace: Path) -> None:
    s.write_committed(workspace, "framework", [])
    assert s.resolve_for_setup(workspace, False, _axis(workspace, "framework")) == ([], False)


def test_setup_y_with_nothing_recorded_takes_every_choice_and_records_it(workspace: Path) -> None:
    assert s.resolve_for_setup(workspace, True, _axis(workspace, "framework")) == (
        ["keras", "lightning", "torch"],
        True,
    )
    assert s.read_committed(workspace, "framework") == ["keras", "lightning", "torch"]


def test_setup_all_takes_every_extra_over_a_recorded_choice(workspace: Path) -> None:
    s.write_committed(workspace, "extras", ["dev"])
    chosen, _ = s.resolve_for_setup(workspace, True, s.EXTRAS, select_all=True)
    assert chosen == ["aim", "dev", "keras", "lightning", "mlflow", "torch"]
    assert s.read_committed(workspace, "extras") == chosen


def _manual(project: Path, names: List[str]) -> None:
    """Mark ``names`` as extras ``project`` installs only when they are named (`[tool.aisland] manual-extras`)."""
    with (project / "pyproject.toml").open("a") as f:
        f.write("\n[tool.aisland]\nmanual-extras = [" + ", ".join(f'"{name}"' for name in names) + "]\n")


def test_a_blank_answer_leaves_out_an_extra_installed_only_when_named(workspace: Path) -> None:
    _manual(workspace / "trainer", ["aim"])

    @click.command()
    def ask() -> None:
        click.echo(repr(s.resolve_for_setup(workspace, False, s.EXTRAS)))

    result = CliRunner().invoke(ask, input="\n")
    lines = result.output.splitlines()
    assert "      [1] aim          trainer (only when named)" in lines
    assert "      [2] dev          models, plain, trainer" in lines
    assert "    Select (names or numbers, comma/space separated; blank = all but those only when named): " in lines
    assert "(['dev', 'keras', 'lightning', 'mlflow', 'torch'], True)" in result.output
    assert s.read_committed(workspace, "extras") == ["dev", "keras", "lightning", "mlflow", "torch"]


def test_naming_an_extra_installed_only_when_named_takes_it(workspace: Path) -> None:
    _manual(workspace / "trainer", ["aim"])

    @click.command()
    def ask() -> None:
        click.echo(repr(s.resolve_for_setup(workspace, False, s.EXTRAS)))

    assert "(['aim', 'torch'], True)" in CliRunner().invoke(ask, input="aim torch\n").output


def test_setup_y_leaves_out_an_extra_installed_only_when_named_and_says_so(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _manual(workspace / "trainer", ["aim"])
    assert s.resolve_for_setup(workspace, True, s.EXTRAS) == (["dev", "keras", "lightning", "mlflow", "torch"], True)
    assert (
        "==> Nothing chosen for extras; installing all (dev, keras, lightning, mlflow, torch); "
        "only when named: aim." in capsys.readouterr().out
    )


def test_setup_all_leaves_out_an_extra_installed_only_when_named(workspace: Path) -> None:
    _manual(workspace / "trainer", ["aim"])
    chosen, _ = s.resolve_for_setup(workspace, True, s.EXTRAS, select_all=True)
    assert chosen == ["dev", "keras", "lightning", "mlflow", "torch"]


def test_an_extra_one_project_installs_only_when_named_is_left_out_for_every_project(workspace: Path) -> None:
    _manual(workspace / "models", ["torch"])  # trainer offers torch too, and does not mark it
    chosen, _ = s.resolve_for_setup(workspace, True, s.EXTRAS, select_all=True)
    assert "torch" not in chosen


def test_without_extras_installed_only_when_named_the_blank_answer_is_everything(workspace: Path) -> None:
    @click.command()
    def ask() -> None:
        click.echo(repr(s.resolve_for_setup(workspace, False, s.EXTRAS)))

    result = CliRunner().invoke(ask, input="\n")
    assert "    Select (names or numbers, comma/space separated; blank = all): " in result.output.splitlines()
    assert "(['aim', 'dev', 'keras', 'lightning', 'mlflow', 'torch'], True)" in result.output


@pytest.mark.parametrize(
    "value, said",
    [
        ('["nope"]', "trainer marks 'nope' as installed only when named, but defines no extra of that name"),
        ('"aim"', 'manual-extras must be a list of extras, as ["a", "b"]'),
    ],
)
def test_manual_extras_that_are_no_extras_are_refused_naming_the_file_and_line(
    workspace: Path, value: str, said: str
) -> None:
    pyproject = workspace / "trainer" / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + f"\n[tool.aisland]\nmanual-extras = {value}\n")
    line = pyproject.read_text().splitlines().index("[tool.aisland]") + 2
    with pytest.raises(DeclarationError) as raised:
        s.axes(workspace)
    assert str(raised.value.message) == f"{pyproject}:{line}: {said}"


def test_a_selection_nobody_offers_anything_for_is_not_asked(tmp_path: Path) -> None:
    _project(tmp_path, "trainer", [], {"framework": []}, {"framework": QUESTION})
    assert s.resolve_for_setup(tmp_path, False, _axis(tmp_path, "framework")) == ([], False)


@pytest.mark.parametrize("answer", ["torch keras", "torch,keras", "3, 1"])
def test_an_answer_is_names_or_numbers(answer: str) -> None:
    assert sorted(s.parse_selection(answer, ["keras", "lightning", "torch"], s.EXTRAS)) == ["keras", "torch"]


def test_a_blank_answer_is_everything_and_an_unknown_name_is_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    assert s.parse_selection("", ["a", "b"], s.EXTRAS) == ["a", "b"]
    assert s.parse_selection("a zz a", ["a", "b"], s.EXTRAS) == ["a"]
    assert "unknown extra 'zz'" in capsys.readouterr().out


# --- aisland workspace configure <selection> ----------------------------------------------------------------------


def test_configure_lists_each_selection_with_its_question(workspace: Path) -> None:
    result = _run()
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    framework = lines.index(f"  framework  {QUESTION}")
    assert lines[framework + 1].strip() == "set NAME… [--machine-only]"
    assert any(line.startswith("  extras ") for line in lines)


def test_a_selection_lists_its_choices_and_both_layers(workspace: Path) -> None:
    s.write_committed(workspace, "framework", ["torch", "keras"])
    s.set_machine_choice(workspace, "framework", ["torch"])
    result = _run("framework")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "  FRAMEWORK  COMMITTED  THIS MACHINE  EFFECTIVE  PROJECTS",
        "  keras      yes        no            no         models",
        "  lightning  no         no            no         trainer",
        "  torch      yes        yes           yes        models, trainer",
    ]


def test_a_layer_without_a_choice_shows_a_dash(workspace: Path) -> None:
    rows = json.loads(_run("tracker", "--json").output)["rows"]
    assert rows[0] == {"tracker": "aim", "committed": "-", "this machine": "-", "effective": "-", "projects": "trainer"}


def test_set_commits_the_choice_and_reinstalls_the_projects_that_offer_it(
    workspace: Path, installs: List[List[str]]
) -> None:
    result = _run("framework", "set", "torch", "keras")
    assert result.exit_code == 0, result.output
    assert s.read_committed(workspace, "framework") == ["torch", "keras"]
    assert "framework = torch, keras (in workspace.toml)" in result.output
    targets = [call[-1] for call in installs]
    assert sorted(Path(t.split("[")[0]).name for t in targets) == ["models", "trainer"]
    assert f"{workspace / 'models'}[dev,keras,torch]" in targets


def test_set_machine_only_changes_this_machine_only(workspace: Path, installs: List[List[str]]) -> None:
    result = _run("framework", "set", "keras", "--machine-only")
    assert result.exit_code == 0, result.output
    assert s.read_committed(workspace, "framework") is None
    assert s.machine_choice(workspace, "framework") == ["keras"]
    assert "framework = keras on this machine (nothing committed)" in result.output


def test_set_names_the_committed_choice_a_machine_choice_still_overrides(
    workspace: Path, installs: List[List[str]]
) -> None:
    s.set_machine_choice(workspace, "framework", ["keras"])
    result = _run("framework", "set", "torch")
    assert "this machine's choice (keras) still wins here" in result.output
    assert "aisland workspace configure framework unset --machine-only" in result.output


def test_set_refuses_a_name_no_project_offers_and_installs_nothing(workspace: Path, installs: List[List[str]]) -> None:
    result = _run("framework", "set", "torch", "jax")
    assert result.exit_code == 1
    assert "Error: no project offers framework 'jax' (the choices: keras, lightning, torch)" in result.output
    assert s.read_committed(workspace, "framework") is None
    assert installs == []


def test_unset_forgets_the_committed_choice(workspace: Path, installs: List[List[str]]) -> None:
    s.write_committed(workspace, "framework", ["torch"])
    result = _run("framework", "unset")
    assert result.exit_code == 0, result.output
    assert s.read_committed(workspace, "framework") is None
    assert "setup asks again" in result.output
    assert installs == []  # nothing is chosen, so nothing is reinstalled


def test_unset_machine_only_falls_back_to_the_committed_choice(workspace: Path, installs: List[List[str]]) -> None:
    s.write_committed(workspace, "framework", ["torch"])
    s.set_machine_choice(workspace, "framework", ["keras"])
    result = _run("framework", "unset", "--machine-only")
    assert result.exit_code == 0, result.output
    assert s.effective(workspace, "framework") == ["torch"]
    assert f"{workspace / 'models'}[dev,torch]" in [call[-1] for call in installs]


def test_a_failed_install_is_the_exit_code(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(cmd: List[str], **kw: object) -> int:
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr("aisland.manager.ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(s.subprocess, "check_call", fail)
    assert _run("framework", "set", "torch").exit_code == 2  # two projects offer torch, both failed


def test_a_bad_declaration_is_one_error_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path, "trainer", ["torch"], {"framework": ["torch", "mlx"]}, {"framework": QUESTION})
    monkeypatch.setattr(cli, "get_workspace_root", lambda: tmp_path)
    result = _run("framework")
    assert result.exit_code == 1
    assert result.output.startswith(f"Error: {tmp_path / 'trainer' / 'pyproject.toml'}:")


@pytest.mark.parametrize(
    "argv, message",
    [
        (["framework", "list"], "No such command 'framework'"),
        (["tracker", "set", "mlflow"], "No such command 'tracker'"),
        (["config", "show"], "No such command 'config'"),
        (["setup", "--framework", "torch"], "No such option '--framework'"),
        (["setup", "--extra", "dev"], "No such option '--extra'"),
        (["workspace", "configure", "framework", "set", "torch", "--no-apply"], "No such option '--no-apply'"),
    ],
)
def test_the_old_commands_and_options_are_gone(workspace: Path, argv: List[str], message: str) -> None:
    result = CliRunner().invoke(cli.main, argv)
    assert result.exit_code == 2
    assert message in result.output


# --- the service message ------------------------------------------------------------------------------------------


def test_the_command_that_adds_a_tracker_keeps_the_ones_chosen(workspace: Path) -> None:
    s.write_committed(workspace, "tracker", ["aim"])
    assert s.add_command(workspace, "tracker", "mlflow") == "aisland workspace configure tracker set aim mlflow"
    s.set_machine_choice(workspace, "tracker", [])
    assert s.add_command(workspace, "tracker", "mlflow") == "aisland workspace configure tracker set mlflow"
    s.set_machine_choice(workspace, "tracker", ["mlflow"])
    assert s.add_command(workspace, "tracker", "mlflow") == "aisland workspace configure tracker set mlflow"


@pytest.mark.parametrize(
    "service, tracker",
    [
        ("aisland.services.mlflow.MLflowService", "mlflow"),
    ],
)
def test_a_missing_tracker_names_the_command_that_adds_it_to_the_chosen_ones(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, service: str, tracker: str
) -> None:
    """The service never installs its backend; its message keeps the trackers already chosen."""
    import importlib

    module, _, name = service.rpartition(".")
    cls = getattr(importlib.import_module(module), name)
    s.write_committed(workspace, "tracker", ["tensorboard"])
    monkeypatch.setattr("importlib.util.find_spec", lambda spec, *a: None)
    with pytest.raises(click.ClickException) as caught:
        cls().setup()
    command = f"aisland workspace configure tracker set tensorboard {tracker}"
    assert f"      {command}\n" in caught.value.message
    assert caught.value.message.endswith(f"      {command} --machine-only")


def test_adding_an_extra_to_an_unset_selection_keeps_the_dev_extras_it_installs(workspace: Path) -> None:
    """An `extras` never chosen installs each project's `dev`; the command that adds one more must keep it, or every
    project's tools go at the next setup."""
    assert s.effective(workspace, "extras") is None
    assert s.add_command(workspace, "extras", "labelstudio") == "aisland workspace configure extras set dev labelstudio"
    s.write_committed(workspace, "extras", ["vision"])
    assert (
        s.add_command(workspace, "extras", "labelstudio") == "aisland workspace configure extras set vision labelstudio"
    )
    s.write_committed(workspace, "extras", None)
    assert s.add_command(workspace, "tracker", "aim") == "aisland workspace configure tracker set aim"


# --- CI reads the committed layer only ----------------------------------------------------------------------------


def _ci_install_line(workspace: Path, monkeypatch: pytest.MonkeyPatch, project: str) -> str:
    from aisland.services.jenkins import JenkinsService

    monkeypatch.setattr("aisland.services.jenkins.is_private_repo", lambda repository: False)
    JenkinsService().scaffold_pipelines(project_name=project, force=True)
    text = (workspace / project / ".github" / "workflows" / "ci.yml").read_text()
    return next(line.strip() for line in text.splitlines() if '-e ".[' in line)


def test_ci_installs_the_committed_choice(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s.write_committed(workspace, "framework", ["torch", "keras"])
    s.write_committed(workspace, "tracker", ["aim"])
    assert ".[dev,torch,aim]" in _ci_install_line(workspace, monkeypatch, "trainer")


def test_ci_never_installs_this_machines_choice(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s.write_committed(workspace, "framework", ["torch"])
    s.set_machine_choice(workspace, "framework", ["lightning"])
    assert ".[dev,torch]" in _ci_install_line(workspace, monkeypatch, "trainer")


def test_ci_installs_dev_alone_while_nothing_is_committed(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s.set_machine_choice(workspace, "framework", ["torch"])
    assert ".[dev]" in _ci_install_line(workspace, monkeypatch, "trainer")


# --- the real workspace -------------------------------------------------------------------------------------------


@needs_workspace
def test_the_real_workspace_declares_its_selections_without_a_fault() -> None:
    """Every choice of every project is an extra of its own, and the training project asks both questions."""
    names = [axis.name for axis in s.axes(get_workspace_root())]
    assert names[0] == "extras"
    assert {"framework", "tracker"} <= set(names)
    assert not (get_workspace_root() / "config.toml").exists()


def test_the_overview_and_the_plugins_agree(workspace: Path) -> None:
    assert {"extras", "framework", "tracker", "active", "mirror", "source"} == set(configure.plugins(workspace))


def test_a_selection_called_name_is_refused_since_the_workspaces_name_has_that_key(tmp_path: Path) -> None:
    _project(tmp_path, "trainer", ["torch"], {"name": ["torch"]}, {"name": "Which name?"})
    pyproject = tmp_path / "trainer" / "pyproject.toml"
    line = pyproject.read_text().splitlines().index("[tool.aisland.selection.name]") + 1
    with pytest.raises(DeclarationError) as raised:
        s.axes(tmp_path)
    assert str(raised.value.message) == (
        f"{pyproject}:{line}: a selection cannot be called 'name': workspace.toml's [workspace] name is the "
        "workspace's name"
    )
