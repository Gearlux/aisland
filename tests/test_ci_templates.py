"""`aisland jenkins scaffold` writes a project's CI files from a template set: aisland's own, a folder of the
workspace, or one an installed package registers — chosen in `workspace.toml`, with the set's own variables.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from aisland import cli, locations
from aisland.ci import ci_scaffold, ci_templates
from aisland.services.jenkins import JenkinsService
from aisland.workspace import check as workspace_check
from aisland.workspace import file as workspace_file

#: A company's Jenkinsfile: a shared library, a pod agent, a private index — and a Groovy list of maps, which Jinja's
#: own `{{ }}` would not have read past.
COMPANY_JENKINSFILE = """\
@Library(['<[ variables.library ]>']) _
pipeline {
    agent { kubernetes { inheritFrom '<[ variables.agent_label ]>' } }
    stages {
        stage('Setup') { steps { sh "pip install --index-url <[ variables.index_url ]> -e '.[<[ extras|join(',') ]>]'" } }
        stage('Unit Tests') {
            steps { sh "pytest<% if slow_tests %> -m 'not slow'<% endif %> --cov=<[ package ]>" }
            post { always { recordIssues tools: [[parser: 'COBERTURA', pattern: 'coverage.xml']] } }
        }
    }
}
"""

COMPANY_SET = """\
[files]
"Jenkinsfile" = "Jenkinsfile.jinja"

[variables]
library = { default = "Company Pipeline Library", help = "the shared library the pipeline loads" }
agent_label = { default = "linux-pod", help = "the pod template a build runs in" }
index_url = { help = "the package index CI installs from" }
"""


def _project(root: Path, name: str, slow: bool = False) -> Path:
    folder = root / name
    (folder / name).mkdir(parents=True)
    (folder / name / "__init__.py").touch()
    markers = '\n[tool.pytest.ini_options]\nmarkers = ["slow: the slow run"]\n' if slow else ""
    (folder / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n{markers}')
    return folder


def _set(folder: Path, set_file: str = COMPANY_SET, jenkinsfile: str = COMPANY_JENKINSFILE) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "template.toml").write_text(set_file)
    (folder / "Jenkinsfile.jinja").write_text(jenkinsfile)
    return folder


def _workspace_file(root: Path, text: str) -> Path:
    path = root / "workspace.toml"
    path.write_text(textwrap.dedent(text))
    return path


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "acme"
    root.mkdir()
    _project(root, "models", slow=True)
    monkeypatch.setattr(locations, "workspace_root", lambda: root)
    return root


def _scaffold(project: str = "models") -> None:
    ci_scaffold.scaffold(project, force=True, jenkins_url=JenkinsService().url())


# --- aisland's own set -------------------------------------------------------------------------------------------


def test_without_a_choice_aisland_writes_its_three_files_each_naming_its_template(ws: Path) -> None:
    _scaffold()
    for relative, template in (
        ("Jenkinsfile.local", "Jenkinsfile.local.jinja"),
        ("Jenkinsfile", "Jenkinsfile.jinja"),
        (".github/workflows/ci.yml", "ci.yml.jinja"),
    ):
        lines = (ws / "models" / relative).read_text().splitlines()
        assert lines[3].endswith(f"Source template: {template} of the 'aisland' template set"), lines[3]
        assert lines[4].endswith("To regenerate:  aisland jenkins scaffold models --force")


def test_the_groovy_list_of_maps_in_aislands_jenkinsfile_passes_through_the_template(ws: Path) -> None:
    _scaffold()
    assert "tools: [[parser: 'COBERTURA', pattern: 'coverage.xml']]" in (ws / "models" / "Jenkinsfile").read_text()


# --- a set of the workspace's own ---------------------------------------------------------------------------------


def test_a_set_in_a_folder_of_the_workspace_writes_only_its_files_with_the_projects_facts(ws: Path) -> None:
    _set(ws / "ci-templates")
    _workspace_file(
        ws,
        """\
        [ci]
        templates = "ci-templates"

        [ci.variables]
        index_url = "https://pypi.example.com/simple"
        """,
    )
    _scaffold()
    text = (ws / "models" / "Jenkinsfile").read_text()
    assert text.splitlines()[3] == "// Source template: Jenkinsfile.jinja of the 'ci-templates' template set"
    body = "\n".join(text.splitlines()[6:])
    assert "@Library(['Company Pipeline Library']) _" in body
    assert "inheritFrom 'linux-pod'" in body
    assert "pip install --index-url https://pypi.example.com/simple -e '.[dev]'" in body
    assert "pytest -m 'not slow' --cov=models" in body
    assert "tools: [[parser: 'COBERTURA', pattern: 'coverage.xml']]" in body
    assert not (ws / "models" / "Jenkinsfile.local").exists()
    assert not (ws / "models" / ".github").exists()


def test_a_projects_own_ci_table_picks_its_set_and_its_values(ws: Path) -> None:
    _set(ws / "ci-templates")
    _project(ws, "tools")
    _workspace_file(
        ws,
        """\
        [project."tools"]
        url = "https://example.com/acme/tools.git"
        ci = { templates = "ci-templates", agent_label = "gpu-pod", index_url = "https://gpu.example.com/simple" }
        """,
    )
    _scaffold("tools")
    _scaffold("models")
    assert "inheritFrom 'gpu-pod'" in (ws / "tools" / "Jenkinsfile").read_text()
    assert "--index-url https://gpu.example.com/simple" in (ws / "tools" / "Jenkinsfile").read_text()
    assert "of the 'aisland' template set" in (ws / "models" / "Jenkinsfile").read_text()


def test_a_set_an_installed_package_registers_is_chosen_by_its_name(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    _set(site / "acme_ci_templates")
    (site / "acme_ci_templates" / "__init__.py").touch()
    (site / "acme_ci-1.0.dist-info").mkdir(parents=True)
    (site / "acme_ci-1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: acme-ci\nVersion: 1.0\n")
    (site / "acme_ci-1.0.dist-info" / "entry_points.txt").write_text(
        "[aisland.ci_templates]\nacme-ci = acme_ci_templates\n"
    )
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.delitem(sys.modules, "acme_ci_templates", raising=False)
    _workspace_file(
        ws, '[ci]\ntemplates = "acme-ci"\n\n[ci.variables]\nindex_url = "https://pypi.example.com/simple"\n'
    )
    _scaffold()
    assert "of the 'acme-ci' template set" in (ws / "models" / "Jenkinsfile").read_text()


# --- what is refused ----------------------------------------------------------------------------------------------


def test_a_set_nobody_has_is_refused_naming_the_line_and_the_sets_there_are(ws: Path) -> None:
    path = _workspace_file(ws, '[ci]\ntemplates = "nowhere"\n')
    with pytest.raises(ci_templates.TemplateError) as refused:
        _scaffold()
    assert refused.value.message.startswith(f"{path}:2: templates = 'nowhere' is no folder of the workspace and no ")
    assert refused.value.message.endswith("(the sets: aisland)")
    assert not (ws / "models" / "Jenkinsfile").exists()


def test_a_required_variable_nobody_set_is_refused_with_its_help(ws: Path) -> None:
    _set(ws / "ci-templates")
    path = _workspace_file(ws, '[ci]\ntemplates = "ci-templates"\n')
    with pytest.raises(ci_templates.TemplateError) as refused:
        _scaffold()
    assert refused.value.message == (
        f"{path}: the 'ci-templates' template set needs index_url — set it in [ci.variables] "
        "(index_url: the package index CI installs from)"
    )


def test_a_variable_the_set_does_not_declare_is_refused(ws: Path) -> None:
    _set(ws / "ci-templates")
    path = _workspace_file(ws, '[ci]\ntemplates = "ci-templates"\n\n[ci.variables]\nindex_ulr = "x"\n')
    with pytest.raises(ci_templates.TemplateError) as refused:
        _scaffold()
    assert refused.value.message == (
        f"{path}: [ci.variables]: index_ulr is no variable of the 'ci-templates' template set "
        "(its variables: agent_label, index_url, library)"
    )


def test_a_fact_no_project_has_is_refused_naming_the_template_and_its_line(ws: Path) -> None:
    _set(
        ws / "ci-templates", set_file='[files]\n"Jenkinsfile" = "Jenkinsfile.jinja"\n', jenkinsfile="a\n<[ pakage ]>\n"
    )
    _workspace_file(ws, '[ci]\ntemplates = "ci-templates"\n')
    with pytest.raises(ci_templates.TemplateError) as refused:
        _scaffold()
    assert refused.value.message == f"{ws / 'ci-templates' / 'Jenkinsfile.jinja'}:2: 'pakage' is undefined"


def test_a_template_that_does_not_parse_is_refused_naming_its_line(ws: Path) -> None:
    _set(ws / "ci-templates", set_file='[files]\n"Jenkinsfile" = "Jenkinsfile.jinja"\n', jenkinsfile="a\n<% if %>\n")
    _workspace_file(ws, '[ci]\ntemplates = "ci-templates"\n')
    with pytest.raises(ci_templates.TemplateError, match=r"Jenkinsfile.jinja:2: "):
        _scaffold()


@pytest.mark.parametrize(
    "set_file, message",
    [
        ("[variables]\n", "[files] must map each file the set writes to its template"),
        ('[files]\n"Jenkinsfile" = "missing.jinja"\n', "Jenkinsfile names no template of"),
        ('[files]\n"notes.txt" = "Jenkinsfile.jinja"\n', "aisland cannot tell how notes.txt writes a comment"),
        ('[files]\n"Jenkinsfile" = "Jenkinsfile.jinja"\n[variables]\nx = "y"\n', "variable 'x' must be"),
        (
            'format = 2\n[files]\n"Jenkinsfile" = "Jenkinsfile.jinja"\n',
            "template.toml:1: template.toml is format 2, written for a newer aisland; this one reads format 1. "
            "Upgrade it: pip install --upgrade aisland",
        ),
        (
            'format = "1"\n[files]\n"Jenkinsfile" = "Jenkinsfile.jinja"\n',
            "format must be a whole number, as format = 1",
        ),
    ],
)
def test_a_set_file_aisland_cannot_use_is_refused(tmp_path: Path, set_file: str, message: str) -> None:
    folder = _set(tmp_path / "set", set_file=set_file)
    with pytest.raises(ci_templates.TemplateError, match=re.escape(message)):
        ci_templates.load_set(folder, "set")


@pytest.mark.parametrize("first_line", ["", "format = 1\n"], ids=["no format", "format 1"])
def test_a_set_of_format_1_or_none_is_read(tmp_path: Path, first_line: str) -> None:
    """A set written before `format` existed is format 1; aisland's own set says so."""
    folder = _set(tmp_path / "set", set_file=first_line + '[files]\n"Jenkinsfile" = "Jenkinsfile.jinja"\n')
    assert [output.path for output in ci_templates.load_set(folder, "set").files] == ["Jenkinsfile"]
    assert "format = 1" in (ci_templates.BUILTIN_DIR / ci_templates.SET_FILE).read_text()


def test_a_file_with_its_own_comment_gets_a_header_in_it(tmp_path: Path) -> None:
    folder = _set(
        tmp_path / "set", set_file='[files]\n"notes.txt" = { template = "Jenkinsfile.jinja", comment = ";" }\n'
    )
    (folder / "Jenkinsfile.jinja").write_text("<[ project ]>\n")
    loaded = ci_templates.load_set(folder, "set")
    facts = ci_templates.Facts(
        "models", "Models", "models", "models", ["dev"], None, False, [], "", None, None, {}, {}, {}, "", "acme", ""
    )
    rendered = ci_templates.render(ci_templates.Choice(loaded, {}), facts)
    assert rendered["notes.txt"].splitlines()[1] == "; AUTO-GENERATED FILE — DO NOT EDIT BY HAND"
    assert rendered["notes.txt"].endswith("\nmodels\n")


# --- export, and the file -----------------------------------------------------------------------------------------


def test_export_copies_aislands_set_and_the_copy_writes_the_same_files(ws: Path) -> None:
    result = CliRunner().invoke(cli.main, ["jenkins", "templates", "export", str(ws / "ci-templates")])
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in (ws / "ci-templates").iterdir()) == sorted(
        p.name for p in ci_templates.BUILTIN_DIR.iterdir()
    )
    _scaffold()
    ours = (ws / "models" / "Jenkinsfile").read_text().splitlines()
    _workspace_file(ws, '[ci]\ntemplates = "ci-templates"\n')
    _scaffold()
    theirs = (ws / "models" / "Jenkinsfile").read_text().splitlines()
    assert ours[6:] == theirs[6:] and theirs[3].endswith("of the 'ci-templates' template set")


def test_export_refuses_a_folder_that_holds_files(ws: Path) -> None:
    (ws / "ci-templates").mkdir()
    (ws / "ci-templates" / "mine.txt").write_text("")
    result = CliRunner().invoke(cli.main, ["jenkins", "templates", "export", str(ws / "ci-templates")])
    assert result.exit_code == 1
    assert "is not empty: export aisland's template set into a new folder" in result.output


def test_the_ci_section_is_kept_when_aisland_rewrites_the_file(ws: Path) -> None:
    text = '[ci]\ntemplates = "ci-templates"\n\n[ci.variables]\nindex_url = "https://pypi.example.com/simple"\n'
    path = _workspace_file(ws, text)
    current = workspace_file.load(ws)
    workspace_file.write(ws, current)
    assert workspace_file.read(path).ci == {
        "templates": "ci-templates",
        "variables": {"index_url": "https://pypi.example.com/simple"},
    }
    assert '[ci]\ntemplates = "ci-templates"\n\n[ci.variables]\nindex_url = ' in path.read_text()


def test_the_ci_settings_raise_no_warning_and_a_typo_in_them_does(ws: Path) -> None:
    path = _workspace_file(ws, '[ci]\ntemplates = "x"\ntempaltes = "y"\n\n[project."models"]\nurl = "u"\nci = {}\n')
    assert workspace_check.unknown_keys(ws) == [
        f"Warning: {path}:3: [ci] tempaltes is no setting aisland reads (did you mean templates?)"
    ]
