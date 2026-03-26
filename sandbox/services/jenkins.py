import sys
import urllib.request
from pathlib import Path
from typing import Optional

import click

from sandbox.manager import BIN_DIR, BaseService


class JenkinsService(BaseService):
    """Jenkins CI/CD Server."""

    URL = "https://get.jenkins.io/war-stable/latest/jenkins.war"

    def __init__(self) -> None:
        super().__init__("jenkins")
        self.war_file = BIN_DIR / "jenkins.war"

    def setup(self) -> None:
        """Download Jenkins binary."""
        if not self.war_file.exists():
            click.echo(f"Downloading Jenkins from {self.URL}...")
            try:
                BIN_DIR.mkdir(exist_ok=True)
                urllib.request.urlretrieve(self.URL, self.war_file)
                click.echo("Download complete.")
            except Exception as e:
                click.echo(f"Error downloading Jenkins: {e}", err=True)
                sys.exit(1)

    def launch(self, port: int = 8080, background: bool = False) -> None:
        """Start the Jenkins server."""
        self.setup()

        cmd = ["java", "-jar", str(self.war_file), f"--httpPort={port}"]
        env = {"JENKINS_HOME": str(self.service_data_dir)}

        self.start(cmd, env=env, background=background)

    def status(self) -> None:
        """Show extended Jenkins status."""
        super().status()
        admin_pass_file = self.service_data_dir / "secrets/initialAdminPassword"
        if self.is_running() and admin_pass_file.exists():
            password = admin_pass_file.read_text().strip()
            click.echo(f"    Initial Admin Password: {password}")

    def clear_jobs(self) -> None:
        """Clear all Jenkins projects/jobs without resetting the rest of the environment."""
        jobs_dir = self.service_data_dir / "jobs"
        if jobs_dir.exists():
            import shutil

            click.echo(f"Clearing Jenkins jobs at {jobs_dir}...")
            shutil.rmtree(jobs_dir)
            jobs_dir.mkdir(parents=True, exist_ok=True)
            click.echo("Jenkins projects cleared.")
        else:
            click.echo("No Jenkins jobs directory found.")

    def init_pipelines(self) -> None:
        """Seed Jenkins with Multi-branch pipelines for all projects in the workspace."""
        from sandbox.manager import BASE_DIR

        workspace_root = BASE_DIR.parent

        # Dynamically find all projects in the workspace root
        projects = []
        for item in workspace_root.iterdir():
            if item.is_dir() and (item / "pyproject.toml").exists():
                projects.append(item.name)

        jobs_dir = self.service_data_dir / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)

        click.echo("Initializing Jenkins pipelines...")
        for project in sorted(projects):
            project_dir = workspace_root / project
            project_job_dir = jobs_dir / project
            project_job_dir.mkdir(exist_ok=True)

            # 1. Generate Jenkinsfile if it doesn't exist (using LogFlow as template)
            jenkinsfile_path = project_dir / "Jenkinsfile"
            if not jenkinsfile_path.exists():
                click.echo(f"  - Generating Jenkinsfile for {project}...")

                # Check for standard directories to include in quality gates
                pkg_dir = project.replace("-", "_")
                check_dirs = [pkg_dir]
                if (project_dir / pkg_dir).exists():
                    pass
                elif (project_dir / project).exists():
                    pkg_dir = project
                    check_dirs = [pkg_dir]
                else:
                    # Fallback to current directory as package dir if we can't find it
                    pkg_dir = project
                    check_dirs = [pkg_dir]

                if (project_dir / "tests").exists():
                    check_dirs.append("tests")
                if (project_dir / "examples").exists():
                    check_dirs.append("examples")

                dirs_str = " ".join(check_dirs)

                # Dependencies for this project (from pyproject.toml if possible)
                deps_str = ""
                try:
                    import tomllib

                    with open(project_dir / "pyproject.toml", "rb") as f:
                        pyproject = tomllib.load(f)
                        deps = pyproject.get("project", {}).get("dependencies", [])
                        for dep in deps:
                            # If it's one of our projects, install it from GitHub
                            if dep in projects:
                                deps_str += f'                sh "${{VENV_BIN}}/pip install --no-cache-dir git+https://github.com/Gearlux/{dep}.git@main"\n'
                except Exception:
                    pass

                # Using a single-quoted triple string for the template to avoid confusion with Groovy's triple quotes
                jenkinsfile_content = f'''pipeline {{
    agent any

    environment {{
        VENV_PATH = "${{WORKSPACE}}/.venv"
        VENV_BIN = "${{VENV_PATH}}/bin"
        FORCE_JAVASCRIPT_ACTIONS_TO_NODE24 = 'true'
    }}

    stages {{
        stage('Initialize') {{
            steps {{
                echo 'Creating Isolated Virtual Environment...'
                sh "python3 -m venv ${{VENV_PATH}}"
                echo 'Installing Dependencies...'
                sh "${{VENV_BIN}}/pip install --upgrade pip"
{deps_str}                sh "${{VENV_BIN}}/pip install -e .[dev] || ${{VENV_BIN}}/pip install -e ."
            }}
        }}

        stage('Quality Gates') {{
            parallel {{
                stage('Black') {{
                    steps {{
                        script {{
                            def rc = sh(script: "${{VENV_BIN}}/black --check --diff {dirs_str} > black-diff.txt 2>&1", returnStatus: true)
                            sh """${{VENV_BIN}}/python3 -c "
import sys, os
lines = open('black-diff.txt').readlines()
with open('black-checkstyle.xml', 'w') as f:
    f.write('<?xml version=\\"1.0\\" encoding=\\"UTF-8\\"?>\\\\n<checkstyle version=\\"5.0\\">\\\\n')
    for line in lines:
        if line.startswith('would reformat '):
            path = line.replace('would reformat ', '').strip()
            f.write('  <file name=\\"' + path + '\\">\\\\n')
            f.write('    <error line=\\"1\\" severity=\\"warning\\" message=\\"Black would reformat this file\\" source=\\"black\\"/>\\\\n')
            f.write('  </file>\\\\n')
    f.write('</checkstyle>\\\\n')
" """
                        }}
                    }}
                    post {{
                        always {{
                            recordIssues(
                                id: 'black-{project}',
                                name: 'Black Formatting ({project.capitalize()})',
                                tools: [checkStyle(pattern: 'black-checkstyle.xml')]
                            )
                        }}
                    }}
                }}
                stage('Isort') {{
                    steps {{
                        script {{
                            def rc = sh(script: "${{VENV_BIN}}/isort --check-only --diff {dirs_str} > isort-diff.txt 2>&1", returnStatus: true)
                            sh """${{VENV_BIN}}/python3 -c "
import sys, os
lines = open('isort-diff.txt').readlines()
with open('isort-checkstyle.xml', 'w') as f:
    f.write('<?xml version=\\"1.0\\" encoding=\\"UTF-8\\"?>\\\\n<checkstyle version=\\"5.0\\">\\\\n')
    for line in lines:
        if line.startswith('ERROR: '):
            path = line.split(' ')[1].strip()
            f.write('  <file name=\\"' + path + '\\">\\\\n')
            f.write('    <error line=\\"1\\" severity=\\"warning\\" message=\\"Isort import order issues\\" source=\\"isort\\"/>\\\\n')
            f.write('  </file>\\\\n')
    f.write('</checkstyle>\\\\n')
" """
                        }}
                    }}
                    post {{
                        always {{
                            recordIssues(
                                id: 'isort-{project}',
                                name: 'Isort Import Order ({project.capitalize()})',
                                tools: [checkStyle(pattern: 'isort-checkstyle.xml')]
                            )
                        }}
                    }}
                }}
                stage('Flake8') {{
                    steps {{
                        sh "${{VENV_BIN}}/flake8 {dirs_str} --tee --output-file=flake8.txt || true"
                    }}
                    post {{
                        always {{
                            recordIssues(
                                id: 'flake8-{project}',
                                name: 'Flake8 ({project.capitalize()})',
                                tools: [flake8(pattern: 'flake8.txt')]
                            )
                        }}
                    }}
                }}
                stage('Mypy') {{
                    steps {{
                        sh "${{VENV_BIN}}/mypy {dirs_str} > mypy.txt || true"
                    }}
                    post {{
                        always {{
                            recordIssues(
                                id: 'mypy-{project}',
                                name: 'Mypy ({project.capitalize()})',
                                tools: [myPy(pattern: 'mypy.txt')]
                            )
                        }}
                    }}
                }}
            }}
        }}

        stage('Unit Tests') {{
            steps {{
                sh "${{VENV_BIN}}/pytest tests --junitxml=test-report.xml --cov={pkg_dir} --cov-report=xml:coverage.xml --cov-report=term || true"
            }}
            post {{
                always {{
                    junit allowEmptyResults: true, testResults: 'test-report.xml'
                    recordCoverage(
                        id: 'coverage-{project}',
                        name: '{project.capitalize()} Coverage',
                        tools: [[parser: 'COBERTURA', pattern: 'coverage.xml']]
                    )
                }}
            }}
        }}
    }}

    post {{
        always {{
            echo '{project.capitalize()} Pipeline Complete.'
        }}
    }}
}}
'''
                jenkinsfile_path.write_text(jenkinsfile_content)

            # 2. Generate config.xml (Multibranch Pipeline) using GitHub remote
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
  <orphanedItemStrategy class="com.cloudbees.hudson.plugins.folder.computed.DefaultOrphanedItemStrategy" plugin="cloudbees-folder">
    <pruneDeadBranches>true</pruneDeadBranches>
    <daysToKeep>-1</daysToKeep>
    <numToKeep>-1</numToKeep>
  </orphanedItemStrategy>
  <triggers/>
  <disabled>false</disabled>
  <sources class="jenkins.branch.MultiBranchProject$BranchSourceList" plugin="branch-api">
    <data>
      <jenkins.branch.BranchSource>
        <source class="jenkins.plugins.git.GitSCMSource" plugin="git">
          <id>{project}-remote</id>
          <remote>git@github.com:Gearlux/{project}.git</remote>
          <credentialsId>jenkins-sandbox</credentialsId>
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
    <scriptPath>Jenkinsfile</scriptPath>
  </factory>
</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>
"""
            config_file = project_job_dir / "config.xml"
            config_file.write_text(config_xml)
            click.echo(f"  - Seeded job: {project}")

        click.echo(
            "Jenkins pipelines initialized. Please restart Jenkins if it was already running."
        )
