"""The Jenkins jobs aisland seeds: ``aisland jenkins init [--local]`` and ``aisland jenkins reset <project>``.

A multibranch job per project with a ``Jenkinsfile`` (or, with ``--local``, a pipeline job per project with a
``Jenkinsfile.local``, its script embedded), the orchestrator that builds them all in dependency order and the local
``<workspace>.nightly-check`` job, each written into the Jenkins home it is handed (``JenkinsService().service_data_dir``);
only :func:`init_pipelines` starts and stops the service around them. What each job's ``config.xml`` holds is
:mod:`aisland.ci.jenkins_job_config`'s, the home's own settings (the dashboard, the global properties, anonymous
read) :mod:`aisland.services.jenkins_settings`'; the service itself is :mod:`aisland.services.jenkins`.
"""

import logging
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from aisland import atomic, console, failures, gitcmd
from aisland.ci import jenkins_job_config as job_config
from aisland.errors import AislandError
from aisland.services import jenkins_settings
from aisland.workspace.projects import get_install_order, get_workspace_name, get_workspace_root

if TYPE_CHECKING:
    from aisland.services.jenkins import JenkinsService

logger = logging.getLogger(__name__)


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
    jenkins_settings.setup_dashboard_view(home)
    jenkins_settings.setup_global_properties(home)

    if local and console.confirm(
        "Allow anonymous read access? (the nightly report, the SwiftBar menu and the scaffold's linter read Jenkins "
        "without logging in; the login and CSRF protection stay)"
    ):
        jenkins_settings.allow_anonymous_read(home)

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
        config_xml = job_config.local_job_config(project_path.name, jenkinsfile_path.read_text(), project_path)
        atomic.write_text((project_job_dir / "config.xml"), config_xml)
        console.echo(f"  - Seeded local job (embedded script): {project}")
        return

    # Standard Multibranch Pipeline for remote jobs
    remote_url = _get_git_remote(project_path)
    if not remote_url:
        console.echo(f"  - Skipping {project}: Could not determine Git remote.")
        return
    cred_id = _find_credentials(home, remote_url)
    atomic.write_text((project_job_dir / "config.xml"), job_config.multibranch_config(project, remote_url, cred_id))
    console.echo(f"  - Seeded job: {project} (Remote: {_without_userinfo(remote_url)}, CredID: {cred_id})")


def _without_userinfo(url: str) -> str:
    """``url`` with any ``user:token@`` dropped: what aisland prints reaches scrollback, CI logs and AI clients."""
    return re.sub(r"^([a-z][a-z0-9+.-]*://)[^/@]*@", r"\1", url, flags=re.IGNORECASE)


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


def _create_orchestrator_job(home: Path, local: bool, initialized_projects: List[str]) -> None:
    """Create an orchestrator job that builds all discovered projects in topological order."""
    workspace_root = get_workspace_root()
    install_order = get_install_order(workspace_root)
    base_name = get_workspace_name()
    pipeline_script = job_config.orchestrator_pipeline(install_order, initialized_projects, local, workspace_root)

    job_name = f"{base_name}.local" if local else base_name
    description = (
        f"Orchestrates the build of all local {base_name} workspace projects based on dependencies."
        if local
        else f"Orchestrates the build of all {base_name} workspace projects based on dependencies."
    )
    project_job_dir = home / "jobs" / job_name
    project_job_dir.mkdir(parents=True, exist_ok=True)
    atomic.write_text(
        (project_job_dir / "config.xml"), job_config.orchestrator_config(pipeline_script, description, local)
    )
    console.echo(f"  - Seeded orchestrator job: {job_name}")


def _create_nightly_check_job(home: Path) -> None:
    """Seed `<workspace>.nightly-check`: `aisland tests nightly` each weekday morning (`NIGHTLY_CHECK_CRON`).

    It reports the orchestrator's nightly runs to `aisland/data/nightly/<date>.md`, notifies on a failure, and
    exits 1 then, so the job is red on the mornings that need a look. The timer sits in
    the job's properties (armed at seeding) and in the pipeline's `triggers` (re-applied at each run).
    """
    job_name = f"{get_workspace_name()}.nightly-check"
    config_xml = job_config.nightly_check_config(get_workspace_name())
    job_dir = home / "jobs" / job_name
    job_dir.mkdir(parents=True, exist_ok=True)
    atomic.write_text((job_dir / "config.xml"), config_xml)
    console.echo(f"  - Seeded nightly check job: {job_name}")


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
