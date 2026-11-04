"""The Jenkins jobs aisland seeds: ``aisland jenkins init [--local]`` and ``aisland jenkins reset <project>``.

A multibranch job per project with a ``Jenkinsfile`` (or, with ``--local``, a pipeline job per project with a
``Jenkinsfile.local``, its script embedded), the orchestrator that builds them all in dependency order, the local
``<workspace>.nightly-check`` job, the dashboard view and the workspace's environment in Jenkins' global properties.
Every function writes into the Jenkins home it is handed (``JenkinsService().service_data_dir``); only
:func:`init_pipelines` starts and stops the service around them. The CI files those jobs run are
:mod:`aisland.ci_scaffold`'s; the service itself is :mod:`aisland.services.jenkins`.
"""

import logging
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, AbstractSet, Dict, List, Optional
from xml.sax.saxutils import escape

from aisland import console, failures, gitcmd
from aisland import pyproject as pyproject_toml
from aisland import supervisor
from aisland.ci_scaffold import registers_slow_marker
from aisland.errors import AislandError
from aisland.projects import ProjectMetadata, get_install_order, get_workspace_name, get_workspace_root

if TYPE_CHECKING:
    from aisland.services.jenkins import JenkinsService

logger = logging.getLogger(__name__)

#: When the LOCAL orchestrator (`<workspace>.local`) builds every project on its own: 01:00 every night (user
#: ruling 2026-10-06), so each project's slow run happens and the test-run statistics fill (`aisland tests report`).
#: Jenkins reads it in the server's time zone; a machine asleep at that minute misses the night (Jenkins does not catch
#: up a missed timer). The GitHub-backed orchestrator gets none: it builds what is pushed.
NIGHTLY_CRON = "0 1 * * *"
#: When the `<workspace>.nightly-check` job reports the nightly runs (`aisland tests nightly`): 09:30 on weekdays (user
#: choice 2026-10-06), so a Monday's report covers Saturday's, Sunday's and Monday's nights.
NIGHTLY_CHECK_CRON = "30 9 * * 1-5"


def init_pipelines(service: "JenkinsService", project_name: Optional[str] = None, local: bool = False) -> None:
    """Seed ``service``'s Jenkins: a job per project — a multibranch pipeline per ``Jenkinsfile``, or with ``local`` a
    pipeline job per ``Jenkinsfile.local``, its script embedded — the orchestrator over them, the nightly check
    (``local``), the dashboard and the workspace's properties; Jenkins is stopped while its files change."""
    home = service.service_data_dir
    service.setup()
    # setup_plugins returns True if it actually installed/updated something
    plugins_changed = service.setup_plugins()

    projects = _projects_to_seed(get_workspace_root(), project_name, local)
    if not projects:
        console.echo("No projects with the required Jenkinsfile found to initialize.")
        return

    jobs_dir = home / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    console.echo(f"Initializing Jenkins pipelines for {len(projects)} project(s)...")
    for project_path in projects:
        _seed_project_job(home, jobs_dir, project_path, local)

    # Create the orchestrator job
    _create_orchestrator_job(home, local, [p.name for p in projects if p.name])
    if local:
        _create_nightly_check_job(home)

    _stop_with_a_config_xml(service, home)
    setup_dashboard_view(home)
    setup_global_properties(home)

    if local and console.confirm(
        "Allow anonymous read access? (the nightly report, the SwiftBar menu and the scaffold's linter read Jenkins "
        "without logging in; the login and CSRF protection stay)"
    ):
        allow_anonymous_read(home)

    console.echo("Starting Jenkins to load the new plugins, pipelines, and globalProperties...")
    service.launch(background=True)
    if plugins_changed:
        console.echo("Jenkins plugins updated.")
    console.echo("Jenkins started in the background.")


def _projects_to_seed(workspace_root: Path, project_name: Optional[str], local: bool) -> List[Path]:
    """The project folders to seed: ``project_name``'s, refused without the Jenkinsfile the mode needs, else every
    folder holding one."""
    if project_name:
        project_path = workspace_root / project_name
        if not project_path.exists() or not project_path.is_dir():
            raise AislandError(f"Project directory '{project_name}' not found at {project_path}.")
        if not local and not (project_path / "Jenkinsfile").exists():
            raise AislandError(f"Project '{project_name}' does not have a Jenkinsfile at its root.")
        if local and not (project_path / "Jenkinsfile.local").exists():
            raise AislandError(f"Project '{project_name}' does not have a Jenkinsfile.local at its root.")
        return [project_path]
    if local:
        return [p for p in workspace_root.iterdir() if p.is_dir() and (p / "Jenkinsfile.local").exists()]
    return [p for p in workspace_root.iterdir() if p.is_dir() and (p / "Jenkinsfile").exists()]


def _seed_project_job(home: Path, jobs_dir: Path, project_path: Path, local: bool) -> None:
    """One project's job: ``<project>.local`` with its ``Jenkinsfile.local`` embedded, or a multibranch job over its
    remote with the credentials Jenkins holds for it."""
    project = f"{project_path.name}.local" if local else project_path.name
    project_job_dir = jobs_dir / project
    project_job_dir.mkdir(exist_ok=True)

    if local:
        # For local jobs, embed the Jenkinsfile.local content directly to avoid needing commits
        jenkinsfile_path = project_path / "Jenkinsfile.local"
        if not jenkinsfile_path.exists():
            failures.warn(f"  - Warning: {jenkinsfile_path} not found for {project}. Skipping.")
            return
        config_xml = local_job_config(project_path.name, jenkinsfile_path.read_text(), project_path)
        (project_job_dir / "config.xml").write_text(config_xml)
        console.echo(f"  - Seeded local job (embedded script): {project}")
        return

    # Standard Multibranch Pipeline for remote jobs
    remote_url = _get_git_remote(project_path)
    if not remote_url:
        console.echo(f"  - Skipping {project}: Could not determine Git remote.")
        return
    cred_id = _find_credentials(home, remote_url)
    (project_job_dir / "config.xml").write_text(_multibranch_config(project, remote_url, cred_id))
    console.echo(f"  - Seeded job: {project} (Remote: {remote_url}, CredID: {cred_id})")


def _multibranch_config(project: str, remote_url: str, cred_id: str) -> str:
    """The config.xml of a multibranch job over ``remote_url`` (a simplified Multibranch Pipeline template)."""
    script_path = "Jenkinsfile"
    config_xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject plugin="workflow-multibranch">
  <properties/>
  <folderViews class="jenkins.branch.MultiBranchProjectViewHolder" plugin="branch-api">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </folderViews>
  <healthMetrics/>
  <icon class="jenkins.branch.MetadataActionFolderIcon" plugin="branch-api">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </icon>
  <orphanedItemStrategy
      class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy"
      plugin="cloudbees-folder">
    <pruneDeadBranches>true</pruneDeadBranches>
    <daysToKeep>-1</daysToKeep>
    <numToKeep>-1</numToKeep>
  </orphanedItemStrategy>
  <triggers>
    <com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger plugin="cloudbees-folder">
      <spec>* * * * *</spec>
      <interval>300000</interval>
    </com.cloudbees.hudson.plugins.folder.computed.PeriodicFolderTrigger>
  </triggers>
  <disabled>false</disabled>
  <sources class="jenkins.branch.MultiBranchProject$BranchSourceList" plugin="branch-api">
    <data>
      <jenkins.branch.BranchSource>
        <source class="jenkins.plugins.git.GitSCMSource" plugin="git">
          <id>{project}-remote</id>
          <remote>{remote_url}</remote>
          <credentialsId>{cred_id}</credentialsId>
          <traits>
            <jenkins.plugins.git.traits.BranchDiscoveryTrait/>
          </traits>
        </source>
        <strategy class="jenkins.branch.DefaultBranchPropertyStrategy">
          <properties class="empty-list"/>
        </strategy>
      </jenkins.branch.BranchSource>
    </data>
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
  </sources>
  <factory class="org.jenkinsci.plugins.workflow.multibranch.WorkflowBranchProjectFactory">
    <owner class="org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject" reference="../.."/>
    <scriptPath>{script_path}</scriptPath>
  </factory>
</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>
"""
    return config_xml


def _stop_with_a_config_xml(service: "JenkinsService", home: Path) -> None:
    """Jenkins stopped, with a ``config.xml`` in ``home`` for the writes that follow."""
    # Two failure modes to avoid when injecting globalProperties:
    #   1. Jenkins is running → our config.xml edits get clobbered by
    #      Jenkins's in-memory model on its next save.
    #   2. Jenkins has never been started (clean reset) → config.xml
    #      doesn't exist yet, and setup_global_properties early-returns
    #      silently, leaving env.WORKSPACE_ROOT null in every build
    #      (manifests as `null/.venv/bin/python3: No such file`).
    # Solution: warm-start Jenkins to materialize config.xml, then stop
    # it before mutating the file, then cold-start for real.
    if not service.is_running():
        console.echo("Warm-starting Jenkins to generate baseline config.xml...")
        service.launch(background=True)
        config_file = home / "config.xml"
        deadline = time.time() + 90
        while not config_file.exists() and time.time() < deadline:
            time.sleep(1)
        if not config_file.exists():
            failures.warn("  - Warning: config.xml did not appear within 90s.")
    service.stop()


def _get_git_remote(project_path: Path) -> Optional[str]:
    """Get the Git remote origin URL for a project (None: no origin, or no git)."""
    return gitcmd.remote_url(project_path)


def _find_credentials(home: Path, remote_url: str) -> str:
    """Find matching credentials in Jenkins configuration."""
    remote_lower = remote_url.lower()
    is_github = "github" in remote_lower
    expected_id = "aisland-github" if is_github else None

    config_file = home / "credentials.xml"
    if not config_file.exists():
        if expected_id:
            failures.warn(
                f"  - Warning: credentials.xml missing. You MUST add your Git credential with ID '{expected_id}'."
            )
        return expected_id or ""

    try:
        tree = ET.parse(config_file)
        root = tree.getroot()

        # Extract all credential IDs
        cred_ids = []
        for entry in root.findall(".//com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl"):
            cred_ids.append(entry.findtext("id") or "")
        for entry in root.findall(".//com.cloudbees.jenkins.plugins.sshcredentials.impl.BasicSSHUserPrivateKey"):
            cred_ids.append(entry.findtext("id") or "")

        if expected_id:
            if expected_id not in cred_ids:
                failures.warn(
                    f"  - Warning: Credential with ID '{expected_id}' not found in credentials.xml! "
                    f"You MUST name your Jenkins credential exactly '{expected_id}'."
                )
            return expected_id

        # Fallback for non-github: return the first available if any
        if cred_ids:
            return cred_ids[0]

    except (OSError, ET.ParseError) as e:
        failures.report(f"  - Error parsing credentials.xml: {e}")

    return expected_id or ""


def local_job_config(project_base_name: str, script_content: str, project_path: Path) -> str:
    """The config.xml of a local job (`<project>.local`): its `Jenkinsfile.local` embedded, no commit needed.

    A pipeline's `parameters` are defined by its FIRST run, and Jenkins drops a parameter a job does not define —
    so a pipeline declaring SLOW_TESTS gets it defined here already, or the orchestrator's first nightly run would
    ask a new job for its slow tests in vain.
    """
    from xml.sax.saxutils import escape

    slow_switch = (
        """
    <hudson.model.ParametersDefinitionProperty>
      <parameterDefinitions>
        <hudson.model.BooleanParameterDefinition>
          <name>SLOW_TESTS</name>
          <defaultValue>false</defaultValue>
        </hudson.model.BooleanParameterDefinition>
      </parameterDefinitions>
    </hudson.model.ParametersDefinitionProperty>"""
        if "name: 'SLOW_TESTS'" in script_content
        else ""
    )
    return f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>Local development pipeline for {project_base_name}</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>{slow_switch}
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps">
    <script>{escape(script_content)}</script>
    <sandbox>true</sandbox>
  </definition>
  <triggers/>
  <disabled>false</disabled>
  <customWorkspace>{project_path.absolute()}</customWorkspace>
</flow-definition>
"""


def _create_orchestrator_job(home: Path, local: bool, initialized_projects: List[str]) -> None:
    """Create an orchestrator job that builds all discovered projects in topological order."""
    workspace_root = get_workspace_root()
    install_order = get_install_order(workspace_root)
    base_name = get_workspace_name()
    phases = _build_phases(install_order, initialized_projects)
    pipeline_script = _orchestrator_script(_phase_stages(phases, local, workspace_root), local)

    job_name = f"{base_name}.local" if local else base_name
    description = (
        f"Orchestrates the build of all local {base_name} workspace projects based on dependencies."
        if local
        else f"Orchestrates the build of all {base_name} workspace projects based on dependencies."
    )
    project_job_dir = home / "jobs" / job_name
    project_job_dir.mkdir(parents=True, exist_ok=True)
    (project_job_dir / "config.xml").write_text(_orchestrator_config(pipeline_script, description, local))
    console.echo(f"  - Seeded orchestrator job: {job_name}")


def _build_phases(install_order: List[ProjectMetadata], initialized_projects: List[str]) -> Dict[int, List[str]]:
    """Each seeded project by its phase: one after the deepest of its workspace dependencies, so a phase builds in
    parallel what the next one needs.

    ``initialized_projects`` are Jenkins job folder names, which follow the workspace folders — NOT the distribution
    names, which may differ (a dashed name, a dashless folder): both are mapped to the folder form to match.
    """
    initialized_set = set(initialized_projects)
    depths: Dict[str, int] = {}
    for p in install_order:
        dir_name = pyproject_to_workspace_dir(p["name"], initialized_set)
        if dir_name not in initialized_set:
            continue
        deps = []
        for dep in p["dependencies"]:
            dep_name = dep.split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].strip()
            dep_dir = pyproject_to_workspace_dir(dep_name, initialized_set)
            if dep_dir in initialized_set and dep_dir != dir_name:
                deps.append(dep_dir)
        depths[dir_name] = max((depths.get(d, -1) for d in deps), default=-1) + 1

    phases: Dict[int, List[str]] = {}
    for proj_name, depth_val in depths.items():
        phases.setdefault(depth_val, []).append(proj_name)
    return phases


def _phase_stages(phases: Dict[int, List[str]], local: bool, workspace_root: Path) -> str:
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
            if _project_has_slow_tests(workspace_root / proj)
            else ""
        )
        stage += f"                            build job: '{job_name}', wait: true, propagate: true{slow_switch}\n"
        stage += "                        }\n"
        stage += "                    }\n"
    else:
        stage += f"                    steps {{ build job: '{job_name}', wait: true, propagate: true }}\n"
    stage += "                }\n"
    return stage


def _orchestrator_script(stages_str: str, local: bool) -> str:
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
    # every project with slow tests for them too (user ruling 2026-10-06), and that run gathers the statistics.
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


def _orchestrator_config(pipeline_script: str, description: str, local: bool) -> str:
    """The orchestrator's config.xml, its script escaped."""
    escaped_script = escape(pipeline_script)
    timer_property = (
        f"""
    <org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>
      <triggers>
        <hudson.triggers.TimerTrigger>
          <spec>{NIGHTLY_CRON}</spec>
        </hudson.triggers.TimerTrigger>
      </triggers>
    </org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>"""
        if local
        else ""
    )
    # SLOW_TESTS defined at seeding too, so Build with Parameters offers the tick before the job's first run.
    slow_switch_property = (
        """
    <hudson.model.ParametersDefinitionProperty>
      <parameterDefinitions>
        <hudson.model.BooleanParameterDefinition>
          <name>SLOW_TESTS</name>
          <defaultValue>false</defaultValue>
        </hudson.model.BooleanParameterDefinition>
      </parameterDefinitions>
    </hudson.model.ParametersDefinitionProperty>"""
        if local
        else ""
    )
    config_xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>{description}</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>{timer_property}{slow_switch_property}
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps">
    <script>{escaped_script}</script>
    <sandbox>true</sandbox>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
"""
    return config_xml


def _create_nightly_check_job(home: Path) -> None:
    """Seed `<workspace>.nightly-check`: `aisland tests nightly` each weekday morning (`NIGHTLY_CHECK_CRON`).

    It reports the orchestrator's nightly runs to `aisland/data/nightly/<date>.md`, notifies on a failure, and
    exits 1 then, so the job is red on the mornings that need a look (user choice 2026-10-06). The timer sits in
    the job's properties (armed at seeding) and in the pipeline's `triggers` (re-applied at each run).
    """
    from xml.sax.saxutils import escape

    job_name = f"{get_workspace_name()}.nightly-check"
    pipeline_script = f"""// The morning report of the nightly runs (aisland/nightly.py): written to aisland/data/nightly/<date>.md,
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
                sh "${{env.WORKSPACE_ROOT}}/.venv/bin/aisland tests nightly"
            }}
        }}
    }}
}}"""
    config_xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>Reports the nightly runs of {get_workspace_name()}.local each weekday morning.</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <org.jenkinsci.plugins.workflow.job.properties.DisableConcurrentBuildsJobProperty/>
    <org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>
      <triggers>
        <hudson.triggers.TimerTrigger>
          <spec>{NIGHTLY_CHECK_CRON}</spec>
        </hudson.triggers.TimerTrigger>
      </triggers>
    </org.jenkinsci.plugins.workflow.job.properties.PipelineTriggersJobProperty>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition" plugin="workflow-cps">
    <script>{escape(pipeline_script)}</script>
    <sandbox>true</sandbox>
  </definition>
  <triggers/>
  <disabled>false</disabled>
</flow-definition>
"""
    job_dir = home / "jobs" / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "config.xml").write_text(config_xml)
    console.echo(f"  - Seeded nightly check job: {job_name}")


#: The jobs the dashboard shows: the branches the ecosystem is built from, and every job of the local pipeline.
_DASHBOARD_JOBS = "^(main|dev/main|.*\\.local)$"
_BUILD_MONITOR_VIEW = "com.smartcodeltd.jenkinsci.plugins.buildmonitor.BuildMonitorView"


def setup_dashboard_view(home: Path, name: Optional[str] = None) -> None:
    """Create a Build Monitor View for the Ecosystem branching workflow."""
    if name is None:
        name = f"{get_workspace_name().capitalize()} Ecosystem Dashboard"

    config_file = home / "config.xml"
    if not config_file.exists():
        console.echo("  - Jenkins config.xml not found. Skipping view setup until first run.")
        return
    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
    except (OSError, ET.ParseError) as e:
        failures.report(f"  - Error parsing config.xml: {e}")
        return

    dashboard = _dashboard_view(_views(root), name)
    _filter_dashboard_jobs(dashboard)
    # Revert primary view to 'all' to ensure standard overview is default
    primary_view = root.find("primaryView")
    if primary_view is not None:
        primary_view.text = "all"
    tree.write(config_file, encoding="UTF-8", xml_declaration=True)


def _view_basics(view: ET.Element, name: str) -> None:
    """The elements every Jenkins view starts with: its owner, name, filters and properties."""
    ET.SubElement(view, "owner").set("class", "hudson")
    ET.SubElement(view, "owner").set("reference", "../../..")
    ET.SubElement(view, "name").text = name
    ET.SubElement(view, "filterExecutors").text = "false"
    ET.SubElement(view, "filterQueue").text = "false"
    ET.SubElement(view, "properties").set("class", "hudson.model.View$PropertyList")


def _views(root: ET.Element) -> ET.Element:
    """Jenkins' ``views`` container — made with the standard ``all`` view when the config has none."""
    views = root.find("views")
    if views is None:
        views = ET.SubElement(root, "views")
        _view_basics(ET.SubElement(views, "hudson.model.AllView"), "all")
    return views


def _dashboard_view(views: ET.Element, name: str) -> ET.Element:
    """The view called ``name``; else any ``<Anything> Ecosystem Dashboard``, renamed in place; else a new one.

    The rename handles a workspace rename (the repository renamed on GitHub, so :func:`get_workspace_name` answers a new
    string) without stranding the old view or creating a duplicate.
    """
    for view in views:
        if view.findtext("name") == name:
            return view
    for view in views:
        existing = view.findtext("name") or ""
        if existing.endswith(" Ecosystem Dashboard"):
            console.echo(f"  - Renaming stale dashboard view '{existing}' -> '{name}' (workspace was renamed).")
            for field in ("name", "title"):
                element = view.find(field)
                if element is not None:
                    element.text = name
            return view
    console.echo(f"  - Creating '{name}' view (branches: main, dev/main, *.local) with Build Monitor View plugin...")
    view = ET.SubElement(views, _BUILD_MONITOR_VIEW)
    view.set("plugin", "build-monitor-plugin")
    _view_basics(view, name)
    ET.SubElement(view, "jobNames").set("class", "hudson.util.CaseInsensitiveComparator")
    ET.SubElement(view, "jobFilters")
    ET.SubElement(view, "columns").set("class", "empty-list")
    ET.SubElement(view, "recurse").text = "true"
    ET.SubElement(view, "title").text = name
    config = ET.SubElement(view, "config")
    ET.SubElement(config, "displayCommitters").text = "true"
    ET.SubElement(config, "displayLowPriorityJobs").text = "false"
    ET.SubElement(config, "displayLastBuildConsoleOutput").text = "true"
    ET.SubElement(config, "order").set("class", "com.smartcodeltd.jenkinsci.plugins.buildmonitor.order.ByName")
    return view


def _filter_dashboard_jobs(view: ET.Element) -> None:
    """Make the view's regular-expression job filter show :data:`_DASHBOARD_JOBS`, adding the filter if it lacks one."""
    job_filters = view.find("jobFilters")
    if job_filters is None:
        job_filters = ET.SubElement(view, "jobFilters")
    regex_filter = job_filters.find("hudson.views.RegExJobFilter")
    if regex_filter is None:
        regex_filter = ET.SubElement(job_filters, "hudson.views.RegExJobFilter")
        regex_filter.set("plugin", "view-job-filters")
        ET.SubElement(regex_filter, "includeExcludeTypeString").text = "includeMatched"
        ET.SubElement(regex_filter, "valueTypeString").text = "NAME"
        ET.SubElement(regex_filter, "regex").text = _DASHBOARD_JOBS
        ET.SubElement(regex_filter, "matchCanary").text = "false"
        return
    regex = regex_filter.find("regex")
    if regex is None:
        regex = ET.SubElement(regex_filter, "regex")
    regex.text = _DASHBOARD_JOBS


def setup_global_properties(home: Path) -> None:
    """Add WORKSPACE_ROOT and other workspace-wide settings to Jenkins global properties."""
    config_file = home / "config.xml"
    if not config_file.exists():
        return

    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
    except (OSError, ET.ParseError) as e:
        failures.report(f"  - Error parsing config.xml for global properties: {e}")
        return

    # Find or create globalNodeProperties
    node_props = root.find("globalNodeProperties")
    if node_props is None:
        node_props = ET.SubElement(root, "globalNodeProperties")

    # Find or create EnvironmentVariablesNodeProperty
    env_vars_prop = node_props.find("hudson.slaves.EnvironmentVariablesNodeProperty")
    if env_vars_prop is None:
        env_vars_prop = ET.SubElement(node_props, "hudson.slaves.EnvironmentVariablesNodeProperty")

    # Clear existing envVars
    env_vars = env_vars_prop.find("envVars")
    if env_vars is not None:
        env_vars_prop.remove(env_vars)

    # Create envVars with custom serialization (tree-map)
    env_vars = ET.SubElement(env_vars_prop, "envVars")
    env_vars.set("serialization", "custom")
    ET.SubElement(env_vars, "unserializable-parents")
    tree_map = ET.SubElement(env_vars, "tree-map")

    # Add default/comparator
    default_elem = ET.SubElement(tree_map, "default")
    ET.SubElement(default_elem, "comparator").set("class", "hudson.util.CaseInsensitiveComparator")

    workspace_root = get_workspace_root()
    dotenv_path = workspace_root / ".env"
    dotenv_vars = supervisor.read_workspace_env(workspace_root)  # the one reader: what the shell reads
    if dotenv_vars:
        logger.debug("read %d key(s) from %s", len(dotenv_vars), dotenv_path)
    else:
        console.echo("  - No workspace .env found; skipping injection.")

    # Computed keys come first so a typo'd .env can't override the workspace
    # path or the forced-Node24 toggle. ** dotenv_vars adds the remainder.
    env_data = {
        "WORKSPACE_ROOT": str(workspace_root),
        "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24": "true",
        **{k: v for k, v in dotenv_vars.items() if k not in ("WORKSPACE_ROOT", "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24")},
    }

    # int size
    ET.SubElement(tree_map, "int").text = str(len(env_data))

    computed_keys = {"WORKSPACE_ROOT", "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24"}
    for key, value in env_data.items():
        ET.SubElement(tree_map, "string").text = key
        ET.SubElement(tree_map, "string").text = value
        if key in computed_keys:
            console.echo(f"  - Added global Jenkins property: {key}={value}")
        else:
            console.echo(f"  - Added global Jenkins property: {key}=<from .env>")

    tree.write(config_file, encoding="UTF-8", xml_declaration=True)


def allow_anonymous_read(home: Path) -> None:
    """Let a visitor who has not logged in read Jenkins (`denyAnonymousReadAccess` false). Nothing else is loosened:
    the login stays, and so does CSRF protection — Jenkins listens on this machine only, but a page open in your
    browser can still post to it, so neither is ever switched off."""
    config_file = home / "config.xml"
    if not config_file.exists():
        return

    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
    except (OSError, ET.ParseError) as e:
        failures.report(f"  - Error parsing config.xml for anonymous read access: {e}")
        return

    auth_strategy = root.find("authorizationStrategy")
    deny_anon = auth_strategy.find("denyAnonymousReadAccess") if auth_strategy is not None else None
    if deny_anon is None:
        console.echo("  - Anonymous read access: config.xml has no setting for it; left as it is.")
        return
    deny_anon.text = "false"
    console.echo("  - Enabled anonymous read access.")
    tree.write(config_file, encoding="UTF-8", xml_declaration=True)


def remove_pipelines(home: Path, project_name: Optional[str] = None) -> None:
    """Remove one or all project pipelines from Jenkins."""
    jobs_dir = home / "jobs"
    if not jobs_dir.exists():
        console.echo("No Jenkins jobs directory found.")
        return

    if project_name:
        project_job_dir = jobs_dir / project_name
        if project_job_dir.exists():
            console.echo(f"Removing Jenkins pipeline for project '{project_name}'...")
            shutil.rmtree(project_job_dir)
            console.echo(f"  - Removed: {project_name}")
        else:
            failures.report(
                f"  - Error: Jenkins job for project '{project_name}' not found.",
            )
    else:
        console.echo("Removing ALL project pipelines from Jenkins...")
        for project_job_dir in jobs_dir.iterdir():
            if project_job_dir.is_dir() and (project_job_dir / "config.xml").exists():
                shutil.rmtree(project_job_dir)
                console.echo(f"  - Removed: {project_job_dir.name}")
        console.echo("All project pipelines removed. Please restart Jenkins if it was already running.")


def _project_has_slow_tests(project_dir: Path) -> bool:
    """`registers_slow_marker` of the project's `pyproject.toml`; False for a folder without one (a detached project)."""
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return False
    return registers_slow_marker(pyproject_toml.load(pyproject))


def pyproject_to_workspace_dir(pyproject_name: str, initialized_set: AbstractSet[str]) -> str:
    """Map a pyproject distribution name to its workspace directory name.

    Handles two conventions:
      1. Identity (most projects): ``confluid`` -> ``confluid``.
      2. Dashed PyPI name -> dashless dir: ``acme-tools`` -> ``acmetools``.

    Returns the input unchanged if no rule matches (caller decides whether
    that unknown name is a problem).
    """
    if pyproject_name in initialized_set:
        return pyproject_name
    dashless = pyproject_name.replace("-", "")
    if dashless in initialized_set:
        return dashless
    return pyproject_name
