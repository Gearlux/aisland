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
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import click

from aisland import failures
from aisland import pyproject as pyproject_toml
from aisland import supervisor
from aisland.ci_scaffold import registers_slow_marker
from aisland.errors import AislandError
from aisland.projects import get_workspace_root

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
    """Seed ``service``'s Jenkins with Multi-branch pipelines for projects discovered in the workspace."""
    home = service.service_data_dir
    service.setup()
    # setup_plugins returns True if it actually installed/updated something
    plugins_changed = service.setup_plugins()

    workspace_root = get_workspace_root()

    if project_name:
        project_path = workspace_root / project_name
        if not project_path.exists() or not project_path.is_dir():
            raise AislandError(f"Project directory '{project_name}' not found at {project_path}.")
        if not local and not (project_path / "Jenkinsfile").exists():
            raise AislandError(f"Project '{project_name}' does not have a Jenkinsfile at its root.")
        if local and not (project_path / "Jenkinsfile.local").exists():
            raise AislandError(f"Project '{project_name}' does not have a Jenkinsfile.local at its root.")
        projects = [project_path]
    else:
        if local:
            projects = [p for p in workspace_root.iterdir() if p.is_dir() and (p / "Jenkinsfile.local").exists()]
        else:
            projects = [p for p in workspace_root.iterdir() if p.is_dir() and (p / "Jenkinsfile").exists()]

    if not projects:
        click.echo("No projects with the required Jenkinsfile found to initialize.")
        return

    jobs_dir = home / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"Initializing Jenkins pipelines for {len(projects)} project(s)...")

    for project_path in projects:
        project_base_name: Optional[str] = project_path.name
        if project_base_name is None:
            continue

        project: str = f"{project_base_name}.local" if local else project_base_name
        project_job_dir = jobs_dir / project
        project_job_dir.mkdir(exist_ok=True)

        if local:
            # For local jobs, embed the Jenkinsfile.local content directly to avoid needing commits
            jenkinsfile_path = project_path / "Jenkinsfile.local"
            if jenkinsfile_path.exists():
                config_xml = local_job_config(project_base_name, jenkinsfile_path.read_text(), project_path)
                config_file = project_job_dir / "config.xml"
                config_file.write_text(config_xml)
                click.echo(f"  - Seeded local job (embedded script): {project}")
                continue
            else:
                failures.warn(f"  - Warning: {jenkinsfile_path} not found for {project}. Skipping.")
                continue

        # Standard Multibranch Pipeline for remote jobs
        remote_url = _get_git_remote(project_path)
        if not remote_url:
            click.echo(f"  - Skipping {project}: Could not determine Git remote.")
            continue
        cred_id = _find_credentials(home, remote_url)
        script_path = "Jenkinsfile"

        # Simplified Multibranch Pipeline config.xml template
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
        config_file = project_job_dir / "config.xml"
        config_file.write_text(config_xml)
        click.echo(f"  - Seeded job: {project} (Remote: {remote_url}, CredID: {cred_id})")

    # Create the orchestrator job
    initialized_names = [p.name for p in projects if p.name]
    _create_orchestrator_job(home, local, initialized_names)
    if local:
        _create_nightly_check_job(home)

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
        click.echo("Warm-starting Jenkins to generate baseline config.xml...")
        service.launch(background=True)
        config_file = home / "config.xml"
        deadline = time.time() + 90
        while not config_file.exists() and time.time() < deadline:
            time.sleep(1)
        if not config_file.exists():
            failures.warn("  - Warning: config.xml did not appear within 90s.")
    service.stop()

    setup_dashboard_view(home)
    setup_global_properties(home)

    if local:
        if click.confirm("Reduce Jenkins security for faster local access? (Disables CSRF, allows anonymous read)"):
            reduce_security(home)

    click.echo("Starting Jenkins to load the new plugins, pipelines, and globalProperties...")
    service.launch(background=True)
    if plugins_changed:
        click.echo("Jenkins plugins updated.")
    click.echo("Jenkins started in the background.")


def _get_git_remote(project_path: Path) -> Optional[str]:
    """Get the Git remote origin URL for a project."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_path,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):  # no origin, or no git
        return None


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
    from aisland.projects import get_install_order, get_workspace_name, get_workspace_root

    workspace_root = get_workspace_root()
    install_order = get_install_order(workspace_root)
    base_name = get_workspace_name()

    # `initialized_projects` is a list of Jenkins job dir names, which follow the
    # workspace directory names (e.g. "loggair", "recordstream") — NOT the pyproject
    # distribution names (which may differ: "loggair", "recordstream"). Build a
    # lookup that maps both forms to the canonical workspace-dir form so we can
    # match either a pyproject name or a dir name against initialized_projects.
    initialized_set = set(initialized_projects)

    def _to_dir_name(pyproject_name: str) -> str:
        return pyproject_to_workspace_dir(pyproject_name, initialized_set)

    depths: dict[str, int] = {}
    for p in install_order:
        dir_name = _to_dir_name(p["name"])
        if dir_name not in initialized_set:
            continue

        deps = []
        for dep in p["dependencies"]:
            dep_name = dep.split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].strip()
            dep_dir = _to_dir_name(dep_name)
            if dep_dir in initialized_set and dep_dir != dir_name:
                deps.append(dep_dir)

        max_dep_depth = -1
        for d in deps:
            max_dep_depth = max(max_dep_depth, depths.get(d, -1))

        depths[dir_name] = max_dep_depth + 1

    phases: dict[int, list[str]] = {}
    for proj_name, depth_val in depths.items():
        phases.setdefault(depth_val, []).append(proj_name)

    stages_str = ""
    for depth in sorted(phases.keys()):
        phase_projects = phases[depth]
        if not phase_projects:
            continue

        stages_str += f"        stage('Phase {depth}') {{\n            parallel {{\n"
        for proj in phase_projects:
            job_name = f"{proj}.local" if local else f"{proj}/main"
            stages_str += f"                stage('Build {proj}') {{\n"
            if local:
                # In local-orchestration mode we want the run to walk
                # through every project even when individual builds fail
                # — `catchError` marks the offending stage red but keeps
                # the overall orchestrator result at SUCCESS so the user
                # can see all failures in one shot instead of fixing
                # them one-by-one. CI keeps the strict propagate: true
                # so PRs/branches stay gated.
                stages_str += "                    steps {\n"
                stages_str += "                        catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {\n"
                # A project with slow tests gets the nightly run's switch (`slowTests()` below); one without has
                # no Slow Tests stage, and Jenkins would only drop a parameter it does not define.
                slow_switch = (
                    ", parameters: [booleanParam(name: 'SLOW_TESTS', value: slowTests())]"
                    if _project_has_slow_tests(workspace_root / proj)
                    else ""
                )
                stages_str += (
                    f"                            build job: '{job_name}', wait: true, propagate: true{slow_switch}\n"
                )
                stages_str += "                        }\n"
                stages_str += "                    }\n"
            else:
                stages_str += f"                    steps {{ build job: '{job_name}', wait: true, propagate: true }}\n"
            stages_str += "                }\n"
        stages_str += "            }\n        }\n"

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

    from xml.sax.saxutils import escape

    escaped_script = escape(pipeline_script)

    job_name = f"{base_name}.local" if local else base_name
    description = (
        f"Orchestrates the build of all local {base_name} workspace projects based on dependencies."
        if local
        else f"Orchestrates the build of all {base_name} workspace projects based on dependencies."
    )

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
    jobs_dir = home / "jobs"
    project_job_dir = jobs_dir / job_name
    project_job_dir.mkdir(parents=True, exist_ok=True)
    config_file = project_job_dir / "config.xml"
    config_file.write_text(config_xml)
    click.echo(f"  - Seeded orchestrator job: {job_name}")


def _create_nightly_check_job(home: Path) -> None:
    """Seed `<workspace>.nightly-check`: `aisland tests nightly` each weekday morning (`NIGHTLY_CHECK_CRON`).

    It reports the orchestrator's nightly runs to `aisland/data/nightly/<date>.md`, notifies on a failure, and
    exits 1 then, so the job is red on the mornings that need a look (user choice 2026-10-06). The timer sits in
    the job's properties (armed at seeding) and in the pipeline's `triggers` (re-applied at each run).
    """
    from xml.sax.saxutils import escape

    from aisland.projects import get_workspace_name

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
    click.echo(f"  - Seeded nightly check job: {job_name}")


def setup_dashboard_view(home: Path, name: Optional[str] = None) -> None:
    """Create a Build Monitor View for the Ecosystem branching workflow."""
    from aisland.projects import get_workspace_name

    base_name = get_workspace_name()
    if name is None:
        name = f"{base_name.capitalize()} Ecosystem Dashboard"

    config_file = home / "config.xml"
    if not config_file.exists():
        click.echo("  - Jenkins config.xml not found. Skipping view setup until first run.")
        return

    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
    except (OSError, ET.ParseError) as e:
        failures.report(f"  - Error parsing config.xml: {e}")
        return

    # Find or create the 'views' container
    views = root.find("views")
    if views is None:
        views = ET.SubElement(root, "views")
        # If views was missing, also add the 'all' view to keep standard behavior
        all_view = ET.SubElement(views, "hudson.model.AllView")
        ET.SubElement(all_view, "owner").set("class", "hudson")
        ET.SubElement(all_view, "owner").set("reference", "../../..")
        ET.SubElement(all_view, "name").text = "all"
        ET.SubElement(all_view, "filterExecutors").text = "false"
        ET.SubElement(all_view, "filterQueue").text = "false"
        ET.SubElement(all_view, "properties").set("class", "hudson.model.View$PropertyList")

    # Find or create the view.
    # First pass: look for an exact name match.
    # Second pass: if not found, look for ANY existing "<Anything> Ecosystem Dashboard"
    # view and RENAME it in place. This handles workspace rename (e.g. the repo is
    # renamed on GitHub, so get_workspace_name() returns a new string) without
    # stranding the old view or creating a duplicate.
    dashboard_view = None
    for v in views:
        if v.findtext("name") == name:
            dashboard_view = v
            break
    if dashboard_view is None:
        for v in views:
            existing = v.findtext("name") or ""
            if existing.endswith(" Ecosystem Dashboard"):
                click.echo(f"  - Renaming stale dashboard view '{existing}' -> '{name}' " f"(workspace was renamed).")
                name_elem = v.find("name")
                if name_elem is not None:
                    name_elem.text = name
                title_elem = v.find("title")
                if title_elem is not None:
                    title_elem.text = name
                dashboard_view = v
                break

    if dashboard_view is None:
        click.echo(
            f"  - Creating '{name}' view (branches: main, dev/main, *.local) " f"with Build Monitor View plugin..."
        )
        dashboard_view = ET.SubElement(
            views,
            "com.smartcodeltd.jenkinsci.plugins.buildmonitor.BuildMonitorView",
        )
        dashboard_view.set("plugin", "build-monitor-plugin")
        ET.SubElement(dashboard_view, "owner").set("class", "hudson")
        ET.SubElement(dashboard_view, "owner").set("reference", "../../..")
        ET.SubElement(dashboard_view, "name").text = name
        ET.SubElement(dashboard_view, "filterExecutors").text = "false"
        ET.SubElement(dashboard_view, "filterQueue").text = "false"
        ET.SubElement(dashboard_view, "properties").set("class", "hudson.model.View$PropertyList")
        ET.SubElement(dashboard_view, "jobNames").set("class", "hudson.util.CaseInsensitiveComparator")
        ET.SubElement(dashboard_view, "jobFilters")
        ET.SubElement(dashboard_view, "columns").set("class", "empty-list")
        ET.SubElement(dashboard_view, "recurse").text = "true"
        ET.SubElement(dashboard_view, "title").text = name
        config = ET.SubElement(dashboard_view, "config")
        ET.SubElement(config, "displayCommitters").text = "true"
        ET.SubElement(config, "displayLowPriorityJobs").text = "false"
        ET.SubElement(config, "displayLastBuildConsoleOutput").text = "true"
        ET.SubElement(config, "order").set("class", "com.smartcodeltd.jenkinsci.plugins.buildmonitor.order.ByName")

    # Always ensure the regex filter is present and correct
    job_filters = dashboard_view.find("jobFilters")
    if job_filters is None:
        job_filters = ET.SubElement(dashboard_view, "jobFilters")

    regex_filter = job_filters.find("hudson.views.RegExJobFilter")
    if regex_filter is None:
        regex_filter = ET.SubElement(job_filters, "hudson.views.RegExJobFilter")
        regex_filter.set("plugin", "view-job-filters")
        ET.SubElement(regex_filter, "includeExcludeTypeString").text = "includeMatched"
        ET.SubElement(regex_filter, "valueTypeString").text = "NAME"
        ET.SubElement(regex_filter, "regex").text = "^(main|dev/main|.*\\.local)$"
        ET.SubElement(regex_filter, "matchCanary").text = "false"
    else:
        regex_elem = regex_filter.find("regex")
        if regex_elem is None:
            regex_elem = ET.SubElement(regex_filter, "regex")
        regex_elem.text = "^(main|dev/main|.*\\.local)$"

    # Revert primary view to 'all' to ensure standard overview is default
    primary_view = root.find("primaryView")
    if primary_view is not None:
        primary_view.text = "all"

    # Robust write
    tree.write(config_file, encoding="UTF-8", xml_declaration=True)


def setup_global_properties(home: Path) -> None:
    """Add WORKSPACE_ROOT and other workspace-wide settings to Jenkins global properties."""
    config_file = home / "config.xml"
    if not config_file.exists():
        return

    from aisland.projects import get_workspace_root

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
        click.echo("  - No workspace .env found; skipping injection.")

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
            click.echo(f"  - Added global Jenkins property: {key}={value}")
        else:
            click.echo(f"  - Added global Jenkins property: {key}=<from .env>")

    tree.write(config_file, encoding="UTF-8", xml_declaration=True)


def reduce_security(home: Path) -> None:
    """Modify config.xml to reduce security for local development."""
    config_file = home / "config.xml"
    if not config_file.exists():
        return

    try:
        tree = ET.parse(config_file)
        root = tree.getroot()
    except (OSError, ET.ParseError) as e:
        failures.report(f"  - Error parsing config.xml for security reduction: {e}")
        return

    if click.confirm("Disable Jenkins security entirely? (No login required, most fast/unsecure)"):
        use_security = root.find("useSecurity")
        if use_security is not None:
            use_security.text = "false"
            click.echo("  - Jenkins security disabled.")
    else:
        # 1. Allow Anonymous Read Access
        auth_strategy = root.find("authorizationStrategy")
        if auth_strategy is not None:
            deny_anon = auth_strategy.find("denyAnonymousReadAccess")
            if deny_anon is not None:
                deny_anon.text = "false"
                click.echo("  - Enabled anonymous read access.")

        # 2. Disable CSRF Protection (Crumb Issuer)
        crumb = root.find("crumbIssuer")
        if crumb is not None:
            root.remove(crumb)
            click.echo("  - Disabled CSRF protection (Crumb Issuer removed).")

    tree.write(config_file, encoding="UTF-8", xml_declaration=True)


def remove_pipelines(home: Path, project_name: Optional[str] = None) -> None:
    """Remove one or all project pipelines from Jenkins."""
    jobs_dir = home / "jobs"
    if not jobs_dir.exists():
        click.echo("No Jenkins jobs directory found.")
        return

    if project_name:
        project_job_dir = jobs_dir / project_name
        if project_job_dir.exists():
            click.echo(f"Removing Jenkins pipeline for project '{project_name}'...")
            shutil.rmtree(project_job_dir)
            click.echo(f"  - Removed: {project_name}")
        else:
            failures.report(
                f"  - Error: Jenkins job for project '{project_name}' not found.",
            )
    else:
        click.echo("Removing ALL project pipelines from Jenkins...")
        for project_job_dir in jobs_dir.iterdir():
            if project_job_dir.is_dir() and (project_job_dir / "config.xml").exists():
                shutil.rmtree(project_job_dir)
                click.echo(f"  - Removed: {project_job_dir.name}")
        click.echo("All project pipelines removed. Please restart Jenkins if it was already running.")


def _project_has_slow_tests(project_dir: Path) -> bool:
    """`registers_slow_marker` of the project's `pyproject.toml`; False for a folder without one (a detached project)."""
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return False
    return registers_slow_marker(pyproject_toml.load(pyproject))


def pyproject_to_workspace_dir(pyproject_name: str, initialized_set: set) -> str:
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
