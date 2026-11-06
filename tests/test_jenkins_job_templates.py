"""Every Jenkins job's `config.xml` comes from a template in `aisland/templates/jenkins/`, each value escaped once.

A url, a name or a path read from a project is another person's text, and a pipeline script is full of `<`, `&` and
quotes: whatever they hold, Jenkins must read back exactly what was given — never markup, never escaped twice. So
each job is rendered here with hostile values and parsed as XML.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import jinja2
import pytest

from aisland.ci import jenkins_job_config as job_config
from aisland.services import jenkins_jobs

#: Text that breaks XML written unescaped, would be escaped twice by a careless template, or looks like Jinja.
HOSTILE = "a&b <c> \"d\" 'e' ${x} {{ y }} {% z %} {# w #} &amp; ]]> <![CDATA[ v ]]>"
#: A pipeline script with the same, over several lines.
SCRIPT = (
    "pipeline {\n"
    "    parameters { booleanParam(name: 'SLOW_TESTS', defaultValue: false) }\n"
    "    stages { stage('x') { steps { sh \"test 1 < 2 && echo ${env.X} {{y}}\" } } }\n"
    "}\n" + HOSTILE
)


def _text(xml: str, path: str) -> str:
    element = ET.fromstring(xml).find(path)
    assert element is not None, path
    return element.text or ""


def test_a_multibranch_job_holds_its_project_remote_and_credential_as_given() -> None:
    xml = job_config.multibranch_config(HOSTILE, "https://u:t@example.com/a.git?a=1&b=<2>&" + HOSTILE, HOSTILE)
    assert _text(xml, ".//source/id") == HOSTILE + "-remote"
    assert _text(xml, ".//source/remote") == "https://u:t@example.com/a.git?a=1&b=<2>&" + HOSTILE
    assert _text(xml, ".//source/credentialsId") == HOSTILE


def test_a_local_job_holds_its_name_script_and_folder_as_given(tmp_path: Path) -> None:
    folder = tmp_path / HOSTILE.replace("/", "_")
    xml = job_config.local_job_config(HOSTILE, SCRIPT, folder)
    assert _text(xml, "description") == f"Local development pipeline for {HOSTILE}"
    assert _text(xml, "definition/script") == SCRIPT
    assert _text(xml, "customWorkspace") == str(folder.absolute())
    assert ET.fromstring(xml).find(".//hudson.model.BooleanParameterDefinition") is not None


@pytest.mark.parametrize("local", [True, False])
def test_an_orchestrator_holds_its_description_and_script_as_given(local: bool) -> None:
    xml = job_config.orchestrator_config(SCRIPT, HOSTILE, local)
    assert _text(xml, "description") == HOSTILE
    assert _text(xml, "definition/script") == SCRIPT
    timer = ET.fromstring(xml).find(".//hudson.triggers.TimerTrigger/spec")
    assert (timer.text if timer is not None else None) == (job_config.NIGHTLY_CRON if local else None)


def test_the_nightly_check_holds_the_workspace_name_as_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    name = "a&b<c>"
    monkeypatch.setattr(jenkins_jobs, "get_workspace_name", lambda: name)
    jenkins_jobs._create_nightly_check_job(tmp_path)
    xml = (tmp_path / "jobs" / f"{name}.nightly-check" / "config.xml").read_text()
    assert _text(xml, "description") == f"Reports the nightly runs of {name}.local each weekday morning."
    assert _text(xml, ".//hudson.triggers.TimerTrigger/spec") == job_config.NIGHTLY_CHECK_CRON
    assert "${env.WORKSPACE_ROOT}/.venv/bin/aisland tests nightly" in _text(xml, "definition/script")


def test_a_script_keeps_its_quotes_as_written_in_the_file() -> None:
    """XML element text needs only `&`, `<` and `>` escaped; a script's quotes stay readable in the job's file."""
    xml = job_config.orchestrator_config("cron('0 1 * * *'); sh \"a && b\"", "d", local=False)
    assert "<script>cron('0 1 * * *'); sh \"a &amp;&amp; b\"</script>" in xml


def test_every_template_refuses_a_value_it_was_not_given() -> None:
    templates = sorted(path.name for path in job_config.TEMPLATES_DIR.glob("*.xml.jinja"))
    assert templates == [
        "local_job.xml.jinja",
        "multibranch.xml.jinja",
        "nightly_check.xml.jinja",
        "orchestrator.xml.jinja",
    ]
    for template in templates:
        with pytest.raises(jinja2.UndefinedError):
            job_config.render_job(template)
