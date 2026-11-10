"""What each Jenkins job aisland seeds holds: its ``config.xml``, rendered from the facts of the workspace.

A multibranch job per project with a ``Jenkinsfile``, a pipeline job per ``Jenkinsfile.local`` (its script embedded),
the orchestrator that builds them phase by phase in dependency order, and the local ``<workspace>.nightly-check``.
Text only: writing a job into a Jenkins home, and that home's own settings, are :mod:`aisland.services.jenkins_jobs`
and :mod:`aisland.services.jenkins_settings`; the CI files the jobs run are :mod:`aisland.ci.ci_scaffold`'s.

Each ``config.xml`` is rendered from its template in ``aisland/templates/jenkins/`` (:func:`render_job`), in an
environment that escapes every ``{{ value }}`` — a url, a name, a path, a whole pipeline script — so no caller escapes
one by hand and none reaches the XML as markup.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Dict, List
from xml.sax.saxutils import escape

import jinja2
from markupsafe import Markup
from packaging.utils import canonicalize_name

from aisland import locations
from aisland import pyproject as pyproject_toml
from aisland.ci.ci_scaffold import registers_slow_marker
from aisland.workspace.projects import ProjectMetadata, requirement_name

#: When the LOCAL orchestrator (`<workspace>.local`) builds every project on its own: 01:00 every night, so
#: each project's slow run happens and the test-run statistics fill (`aisland tests report`).
#: Jenkins reads it in the server's time zone; a machine asleep at that minute misses the night (Jenkins does not catch
#: up a missed timer). The GitHub-backed orchestrator gets none: it builds what is pushed.
NIGHTLY_CRON = "0 1 * * *"
#: When the `<workspace>.nightly-check` job reports the nightly runs (`aisland tests nightly`): 09:30 on weekdays, so
#: a Monday's report covers Saturday's, Sunday's and Monday's nights.
NIGHTLY_CHECK_CRON = "30 9 * * 1-5"
#: The folder of the jobs' ``config.xml`` templates.
TEMPLATES_DIR = locations.PACKAGE_DIR / "templates" / "jenkins"


def _xml_text(value: object) -> object:
    """A ``{{ value }}`` as a config.xml holds it: ``&``, ``<`` and ``>`` escaped — XML element text — and quotes left as
    written, so a pipeline script reads in the file as it does in the Jenkinsfile. Autoescape's own escape would also
    turn every ``'`` and ``"`` of a script into ``&#39;`` and ``&#34;``; a value already escaped (``Markup``: a
    template's literal, already escaped by autoescape, or a ``|safe``) passes as it is. Every value goes into element
    text, never an attribute (where a ``"`` would need escaping too)."""
    if isinstance(value, Markup):
        return value
    return Markup(escape(str(value)))


@functools.lru_cache(maxsize=1)
def _job_templates() -> jinja2.Environment:
    """The environment the job templates render in: autoescaped through :func:`_xml_text`, a value the template uses
    but the caller did not give refused (``StrictUndefined``), and each file's last newline kept."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=True,
        finalize=_xml_text,
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def render_job(template: str, **values: object) -> str:
    """The ``config.xml`` of ``templates/jenkins/<template>`` with ``values``, each escaped as XML text."""
    return _job_templates().get_template(template).render(**values)


def multibranch_config(project: str, remote_url: str, cred_id: str) -> str:
    """The config.xml of a multibranch job over ``remote_url`` (a simplified Multibranch Pipeline template)."""
    return render_job("multibranch.xml.jinja", project=project, remote_url=remote_url, credentials_id=cred_id)


def local_job_config(project_base_name: str, script_content: str, project_path: Path) -> str:
    """The config.xml of a local job (`<project>.local`): its `Jenkinsfile.local` embedded, no commit needed.

    A pipeline's `parameters` are defined by its FIRST run, and Jenkins drops a parameter a job does not define —
    so a pipeline declaring SLOW_TESTS gets it defined here already, or the orchestrator's first nightly run would
    ask a new job for its slow tests in vain.
    """
    return render_job(
        "local_job.xml.jinja",
        project=project_base_name,
        script=script_content,
        slow_tests="name: 'SLOW_TESTS'" in script_content,
        workspace=project_path.absolute(),
    )


def build_phases(install_order: List[ProjectMetadata], initialized_projects: List[str]) -> Dict[int, List[str]]:
    """Each seeded project by its phase: one after the deepest of its workspace dependencies, so a phase builds in
    parallel what the next one needs.

    ``initialized_projects`` are the Jenkins jobs, named by the projects' workspace folders; a requirement reaches its
    project by its PEP 503 name (:func:`aisland.workspace.projects.requirement_name`), whatever its specifier
    (``lib~=1.0``) or spelling (``Lib_Core``). ``install_order`` is dependency-first, so a dependency's phase is known
    before its dependents'.
    """
    initialized = set(initialized_projects)
    folders = {canonicalize_name(project["name"]): project["path"].name for project in install_order}
    depths: Dict[str, int] = {}
    for project in install_order:
        folder = project["path"].name
        if folder not in initialized:
            continue
        needed = {folders.get(name) for name in map(requirement_name, project["dependencies"]) if name is not None}
        deps = [dep for dep in needed if dep is not None and dep in initialized and dep != folder]
        depths[folder] = max((depths.get(dep, -1) for dep in deps), default=-1) + 1

    phases: Dict[int, List[str]] = {}
    for folder, depth in depths.items():
        phases.setdefault(depth, []).append(folder)
    return phases


def phase_stages(phases: Dict[int, List[str]], local: bool, workspace_root: Path) -> str:
    """The orchestrator's stages: one per phase, its projects' builds in parallel."""
    stages_str = ""
    for depth in sorted(phases.keys()):
        phase_projects = phases[depth]
        if not phase_projects:
            continue
        stages_str += f"        stage('Phase {depth}') {{\n            parallel {{\n"
        stages_str += "".join(_build_stage(proj, local, workspace_root) for proj in phase_projects)
        stages_str += "            }\n        }\n"
    return stages_str


def _build_stage(proj: str, local: bool, workspace_root: Path) -> str:
    """The stage that builds one project's job."""
    stage = ""
    job_name = f"{proj}.local" if local else f"{proj}/main"
    stage += f"                stage('Build {proj}') {{\n"
    if local:
        # In local-orchestration mode we want the run to walk
        # through every project even when individual builds fail
        # — `catchError` marks the offending stage red but keeps
        # the overall orchestrator result at SUCCESS so the user
        # can see all failures in one shot instead of fixing
        # them one-by-one. CI keeps the strict propagate: true
        # so PRs/branches stay gated.
        stage += "                    steps {\n"
        stage += "                        catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {\n"
        # A project with slow tests gets the nightly run's switch (`slowTests()` below); one without has
        # no Slow Tests stage, and Jenkins would only drop a parameter it does not define.
        slow_switch = (
            ", parameters: [booleanParam(name: 'SLOW_TESTS', value: slowTests())]"
            if project_has_slow_tests(workspace_root / proj)
            else ""
        )
        stage += f"                            build job: '{job_name}', wait: true, propagate: true{slow_switch}\n"
        stage += "                        }\n"
        stage += "                    }\n"
    else:
        stage += f"                    steps {{ build job: '{job_name}', wait: true, propagate: true }}\n"
    stage += "                }\n"
    return stage


def orchestrator_pipeline(
    install_order: List[ProjectMetadata], initialized_projects: List[str], local: bool, workspace_root: Path
) -> str:
    """The orchestrator's pipeline script: the seeded projects built phase by phase, dependencies first."""
    phases = build_phases(install_order, initialized_projects)
    return orchestrator_script(phase_stages(phases, local, workspace_root), local)


def orchestrator_script(stages_str: str, local: bool) -> str:
    """The orchestrator's pipeline script around ``stages_str``."""
    # `agent none`: the orchestrator only TRIGGERS jobs (`build job:` needs no node). Holding a
    # built-in executor for the whole run left one of the node's two executors for the projects,
    # so every "parallel" phase ran one project at a time (2026-09-13: one project waited 875 s for
    # another; two more sat at "Waiting for next available executor").
    # The LOCAL orchestrator runs every night (`NIGHTLY_CRON`). The `triggers` directive re-applies the timer at
    # every run — a pipeline without one clears the job's timer on its first run — and the job's properties below
    # arm it as soon as the job is seeded, before any run.
    triggers_block = f"    triggers {{\n        cron('{NIGHTLY_CRON}')\n    }}\n" if local else ""
    # A manual Build Now is the FAST run of every project; the nightly timer — or a tick on SLOW_TESTS — asks
    # every project with slow tests for them too, and that run gathers the statistics.
    slow_tests_head = (
        "// The nightly timer, or a tick on SLOW_TESTS, runs every project's slow tests too; Build Now the fast ones.\n"
        "def slowTests() {\n"
        "    return params.SLOW_TESTS || "
        "!currentBuild.getBuildCauses('hudson.triggers.TimerTrigger$TimerTriggerCause').isEmpty()\n"
        "}\n\n"
        if local
        else ""
    )
    slow_tests_param = (
        """    parameters {
        booleanParam(
            name: 'SLOW_TESTS',
            defaultValue: false,
            description: 'If true, run the slow tests of every project too. The nightly run (01:00) does; a manual build runs the fast tests only.'
        )
    }
"""
        if local
        else ""
    )
    pipeline_script = f"""{slow_tests_head}pipeline {{
    agent none
    options {{
        disableConcurrentBuilds()
    }}
{slow_tests_param}{triggers_block}    stages {{
{stages_str}    }}
}}"""
    return pipeline_script


def orchestrator_config(pipeline_script: str, description: str, local: bool) -> str:
    """The orchestrator's config.xml: the nightly timer and the SLOW_TESTS switch on the local one.

    SLOW_TESTS is defined at seeding too, so Build with Parameters offers the tick before the job's first run.
    """
    return render_job(
        "orchestrator.xml.jinja", script=pipeline_script, description=description, local=local, cron=NIGHTLY_CRON
    )


def nightly_check_config(workspace: str) -> str:
    """The config.xml of ``<workspace>.nightly-check``: ``aisland tests nightly`` each weekday morning
    (:data:`NIGHTLY_CHECK_CRON`), its timer in the job's properties (armed at seeding) and in the pipeline's
    ``triggers`` (re-applied at each run)."""
    pipeline_script = f"""// The morning report of the nightly runs (aisland/services/nightly.py): written to aisland/data/nightly/<date>.md,
// a desktop notification and a red build when a night failed, had no run, or is still building.
pipeline {{
    agent {{
        node {{
            label 'built-in'
        }}
    }}
    options {{
        disableConcurrentBuilds()
    }}
    triggers {{
        cron('{NIGHTLY_CHECK_CRON}')
    }}
    stages {{
        stage('Report the nightly runs') {{
            steps {{
                sh "'${{env.WORKSPACE_ROOT}}/.venv/bin/aisland' tests nightly"
            }}
        }}
    }}
}}"""
    return render_job("nightly_check.xml.jinja", workspace=workspace, script=pipeline_script, cron=NIGHTLY_CHECK_CRON)


def project_has_slow_tests(project_dir: Path) -> bool:
    """`registers_slow_marker` of the project's `pyproject.toml`; False for a folder without one (a detached project)."""
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return False
    return registers_slow_marker(pyproject_toml.load(pyproject))
