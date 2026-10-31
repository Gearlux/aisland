"""The system tools a workspace needs: `aisland workspace provision [install]` (`aisland/provision.py`).

A project declares the tools it needs outside Python in its own `pyproject.toml`::

    [tool.aisland.sys-dependencies]
    brew = ["node"]          # macOS, through Homebrew
    userspace = ["node"]     # Linux, from aisland's own download list (installers.USERSPACE_TOOLS)

Discovery reads the declaration before anything is installed and refuses one it cannot use, naming the file and the
line. aisland's own tools (the service supervisor, Docker, …) live in the package, so a workspace whose aisland came
from a wheel gets them too. `aisland workspace provision` lists what this machine needs, who needs it and whether it
is there; `install` installs it, as `aisland setup` does.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Dict, List

import pytest
from click.testing import CliRunner, Result

from aisland import cli, discovery, installers, locations, manager, provision


def _workspace(tmp_path: Path, projects: Dict[str, str]) -> Path:
    """A workspace at ``tmp_path / "ws"`` holding one folder per project, each with the given ``pyproject.toml``."""
    root = tmp_path / "ws"
    (root / ".git").mkdir(parents=True)
    for name, text in projects.items():
        (root / name).mkdir(parents=True)
        (root / name / "pyproject.toml").write_text(textwrap.dedent(text))
    return root


EDITOR = """\
    [project]
    name = "editor"

    [tool.aisland.sys-dependencies]
    brew = ["node"]
    userspace = ["node"]
"""

SHELL = """\
    [project]
    name = "shell"

    [tool.aisland.sys-dependencies]
    brew = ["bash", "node"]
"""


def _refusal(root: Path) -> str:
    with pytest.raises(discovery.DeclarationError) as refused:
        discovery.discover(root)
    return refused.value.message


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """macOS with Homebrew holding ``installed`` (append to it); every install is recorded instead of run."""
    installed: List[str] = ["act", "bash", "process-compose"]
    monkeypatch.setattr(provision.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(provision, "_brew_installed", lambda: set(installed))
    return installed


@pytest.fixture
def installs(monkeypatch: pytest.MonkeyPatch) -> Dict[str, List[object]]:
    calls: Dict[str, List[object]] = {"brew": [], "userspace": [], "java": [], "shell": []}
    monkeypatch.setattr(installers, "install_brew_packages", lambda packages: calls["brew"].append(sorted(packages)))
    monkeypatch.setattr(installers, "install_userspace_tool", lambda name: calls["userspace"].append(name))
    monkeypatch.setattr(installers, "offer_shell_switch", lambda packages: calls["shell"].append(sorted(packages)))
    monkeypatch.setattr(
        "aisland.services.jenkins.JenkinsService.provision_java", lambda self: calls["java"].append("java")
    )
    return calls


def _run(root: Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> Result:
    monkeypatch.setattr(cli, "get_workspace_root", lambda: root)
    return CliRunner().invoke(cli.main, ["workspace", "provision", *args])


# --- the declaration, read before anything is installed -----------------------------------------------------------


def test_a_project_declares_the_tools_it_needs_on_each_platform(tmp_path: Path) -> None:
    found = discovery.discover(_workspace(tmp_path, {"editor": EDITOR, "shell": SHELL}))
    assert [(tool.project, tool.kind, tool.name) for tool in found.system_tools] == [
        ("editor", "brew", "node"),
        ("editor", "userspace", "node"),
        ("shell", "brew", "bash"),
        ("shell", "brew", "node"),
    ]


def test_an_unknown_setting_is_refused_naming_its_line(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path, {"editor": '[project]\nname = "editor"\n\n[tool.aisland.sys-dependencies]\napt = ["node"]\n'}
    )
    assert _refusal(root) == (
        f"{root / 'editor' / 'pyproject.toml'}:4: sys-dependencies takes brew and userspace, not 'apt'"
    )


def test_a_value_that_is_no_list_of_names_is_refused(tmp_path: Path) -> None:
    root = _workspace(
        tmp_path, {"editor": '[project]\nname = "editor"\n\n[tool.aisland.sys-dependencies]\nbrew = "node"\n'}
    )
    assert _refusal(root) == (
        f"{root / 'editor' / 'pyproject.toml'}:4: sys-dependencies: brew must be a list of names, as brew = [\"node\"]"
    )


def test_a_userspace_tool_aisland_cannot_install_is_refused_naming_those_it_can(tmp_path: Path) -> None:
    text = '[project]\nname = "editor"\n\n[tool.aisland.sys-dependencies]\nuserspace = ["nodejs"]\n'
    root = _workspace(tmp_path, {"editor": text})
    assert _refusal(root) == (
        f"{root / 'editor' / 'pyproject.toml'}:4: userspace tool 'nodejs' is none aisland can install "
        f"(it can: {', '.join(sorted(installers.USERSPACE_TOOLS))})"
    )


def test_a_bad_declaration_stops_the_install_before_anything_is_installed(
    tmp_path: Path, macos: List[str], installs: Dict[str, List[object]]
) -> None:
    root = _workspace(
        tmp_path, {"editor": '[project]\nname = "editor"\n\n[tool.aisland.sys-dependencies]\napt = ["x"]\n'}
    )
    with pytest.raises(discovery.DeclarationError):
        provision.install(root)
    assert installs == {"brew": [], "userspace": [], "java": [], "shell": []}


def test_workspace_show_lists_the_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _workspace(tmp_path, {"editor": EDITOR})
    monkeypatch.setattr(cli, "get_workspace_root", lambda: root)
    result = CliRunner().invoke(cli.main, ["workspace", "show", "--json"])
    assert result.exit_code == 0, result.output
    assert {"project": "editor", "kind": "brew", "name": "node"} in [
        {key: tool[key] for key in ("project", "kind", "name")} for tool in json.loads(result.output)["system_tools"]
    ]


# --- aisland's own tools ------------------------------------------------------------------------------------------


def test_aislands_own_tools_come_from_the_package_not_a_pyproject(tmp_path: Path, macos: List[str]) -> None:
    """A workspace whose aisland came from a wheel has no aisland/pyproject.toml, and still gets them."""
    tools = {tool.name: tool.projects for tool in provision.tools(_workspace(tmp_path, {}))}
    assert tools == {name: ("aisland",) for name in provision.AISLAND_TOOLS["brew"]}


def test_aislands_pyproject_declares_no_tools_of_its_own() -> None:
    """One place for them: the checkout's pyproject.toml would list them a second time."""
    import tomllib

    pyproject = Path(provision.__file__).resolve().parents[1] / "pyproject.toml"
    assert "sys-dependencies" not in tomllib.loads(pyproject.read_text()).get("tool", {}).get("aisland", {})


def test_aisland_never_needs_the_floating_openjdk_formula() -> None:
    """`openjdk` follows the newest Java (26 on 2026-10-06), which Jenkins refuses; `openjdk@N` is chosen by the WAR."""
    assert not [name for name in provision.AISLAND_TOOLS["brew"] if name == "openjdk" or name.startswith("openjdk@")]


# --- `aisland workspace provision`: what this machine needs -------------------------------------------------------


def test_the_listing_shows_this_platforms_tools_who_needs_them_and_whether_they_are_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, macos: List[str]
) -> None:
    monkeypatch.setattr(provision, "AISLAND_TOOLS", {"brew": ("act", "f1bonacc1/tap/process-compose"), "userspace": ()})
    result = _run(_workspace(tmp_path, {"editor": EDITOR, "shell": SHELL}), monkeypatch)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "  TOOL                           HOW   PROJECTS       INSTALLED",
        "  act                            brew  aisland        yes",
        "  bash                           brew  shell          yes",
        "  f1bonacc1/tap/process-compose  brew  aisland        yes",
        "  node                           brew  editor, shell  no",
    ]


def test_on_linux_the_listing_is_the_userspace_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision.platform, "system", lambda: "Linux")
    monkeypatch.setattr(provision, "AISLAND_TOOLS", {"brew": ("act",), "userspace": ("process-compose",)})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for binary in installers.USERSPACE_TOOLS["process-compose"]["binaries"]:
        (bin_dir / binary).write_text("")
    monkeypatch.setattr(provision, "BIN_DIR", bin_dir)
    rows = json.loads(_run(_workspace(tmp_path, {"editor": EDITOR}), monkeypatch, "--json").output)["rows"]
    assert rows == [
        {"tool": "node", "how": "userspace", "projects": "editor", "installed": "no"},
        {"tool": "process-compose", "how": "userspace", "projects": "aisland", "installed": "yes"},
    ]


def test_a_platform_aisland_cannot_provision_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision.platform, "system", lambda: "Windows")
    result = _run(_workspace(tmp_path, {"editor": EDITOR}), monkeypatch)
    assert result.exit_code == 0
    assert result.output == "aisland installs no system tools on Windows.\n"


def test_a_bad_declaration_is_one_error_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, macos: List[str]) -> None:
    root = _workspace(
        tmp_path, {"editor": '[project]\nname = "editor"\n\n[tool.aisland.sys-dependencies]\napt = ["x"]\n'}
    )
    result = _run(root, monkeypatch)
    assert result.exit_code == 1
    assert (
        result.output
        == f"Error: {root / 'editor' / 'pyproject.toml'}:4: sys-dependencies takes brew and userspace, not 'apt'\n"
    )


# --- `aisland workspace provision install` ------------------------------------------------------------------------


def test_install_on_macos_installs_every_brew_tool_once_then_the_java_jenkins_runs_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, macos: List[str], installs: Dict[str, List[object]]
) -> None:
    monkeypatch.setattr(provision, "AISLAND_TOOLS", {"brew": ("act",), "userspace": ("process-compose",)})
    result = _run(_workspace(tmp_path, {"editor": EDITOR, "shell": SHELL}), monkeypatch, "install")
    assert result.exit_code == 0, result.output
    assert installs == {
        "brew": [["act", "bash", "node"]],
        "userspace": [],
        "java": ["java"],
        "shell": [["act", "bash", "node"]],
    }


def test_install_on_linux_installs_the_userspace_tools_and_no_java(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installs: Dict[str, List[object]]
) -> None:
    monkeypatch.setattr(provision.platform, "system", lambda: "Linux")
    monkeypatch.setattr(provision, "AISLAND_TOOLS", {"brew": ("act",), "userspace": ("process-compose",)})
    provision.install(_workspace(tmp_path, {"editor": EDITOR}))
    assert installs == {"brew": [], "userspace": ["node", "process-compose"], "java": [], "shell": []}


def test_install_elsewhere_installs_nothing_and_says_so(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installs: Dict[str, List[object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(provision.platform, "system", lambda: "Windows")
    provision.install(_workspace(tmp_path, {"editor": EDITOR}))
    assert installs == {"brew": [], "userspace": [], "java": [], "shell": []}
    assert "aisland installs no system tools on Windows." in capsys.readouterr().out


def test_setup_provisions_through_the_same_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`aisland setup` runs `provision.install` for the workspace, the same as the command."""

    class _Stop(Exception):
        pass

    root = _workspace(tmp_path, {})
    (root / ".venv" / "bin").mkdir(parents=True)
    seen: List[Path] = []

    def stop(workspace: Path, aislands_own: bool = True) -> None:
        seen.append(workspace)
        raise _Stop()

    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    monkeypatch.setattr(manager, "ensure_uv_in_venv", lambda venv_bin: "uv")
    monkeypatch.setattr(manager, "link_project_bashrc", lambda workspace: None)
    monkeypatch.setattr(manager, "install_harness", lambda workspace: [])
    monkeypatch.setattr(provision, "install", stop)
    with pytest.raises(_Stop):
        manager.setup_workspace(non_interactive=True)
    assert seen == [root]


@pytest.mark.usefixtures("empty_workspace")
def test_the_old_command_is_gone() -> None:
    result = CliRunner().invoke(cli.main, ["provision"])
    assert result.exit_code == 2
    assert "No such command 'provision'" in result.output
