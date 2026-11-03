import os
import platform
import subprocess
from pathlib import Path
from typing import Any, List, Optional

import click

from aisland import failures, provision
from aisland.github import github_repository
from aisland.paths import DATA_DIR
from aisland.projects import discover_projects, get_project_metadata, get_workspace_root
from aisland.services.base import BaseService
from aisland.services.jenkins import JenkinsService


def swiftbar_installed() -> bool:
    """Whether the SwiftBar app is in this machine's or this user's Applications folder (where Homebrew's cask puts it)."""
    return any((folder / "SwiftBar.app").exists() for folder in (Path("/Applications"), Path.home() / "Applications"))


class SwiftBarService(BaseService):
    """SwiftBar Plugin Management Service."""

    def __init__(self) -> None:
        super().__init__("swiftbar")
        self.plugins_dir = self.service_data_dir / "plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _require_macos() -> bool:
        """True on macOS; otherwise report and skip.

        SwiftBar is a macOS-only menu bar app and every action here shells out
        to macOS-only tools (``open``, ``defaults``, ``pkill``). Note that
        ``os.name`` is NOT a valid guard — Linux is also ``"posix"``.
        """
        if platform.system() != "Darwin":
            click.echo("SwiftBar is only supported on macOS — skipping.")
            return False
        return True

    def _get_github_repo(self, path: Path) -> Optional[str]:
        """``owner/name`` of the GitHub repository ``path``'s origin names; None for no repository or another host."""
        if not (path / ".git").exists():  # a submodule's .git is a file, the root's a folder
            return None
        try:
            output = subprocess.check_output(
                ["git", "remote", "get-url", "origin"], cwd=path, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (OSError, subprocess.CalledProcessError):  # no origin, or no git
            return None
        repository = github_repository(output)
        return f"{repository.owner}/{repository.name}" if repository else None

    def _dependency_ordered_projects(self, workspace_root: Path) -> List[Path]:
        """Return project directories ordered so dependencies precede dependents.

        Alphabetical tie-breaking is applied within each dependency level so
        the resulting order is deterministic. The workspace root itself is
        excluded (it has no ``pyproject.toml``); callers add it separately.
        """
        import graphlib

        projects = [get_project_metadata(p) for p in discover_projects(workspace_root)]
        project_names = {p["name"] for p in projects if p["name"]}
        name_to_path = {p["name"]: p["path"] for p in projects if p["name"]}

        graph: dict[str, set[str]] = {}
        for p in projects:
            if not p["name"]:
                continue
            deps: set[str] = set()
            for raw in p["dependencies"]:
                # Strip version specifiers, extras, and URL fragments so that
                # 'loggair>=0.1.0', 'pkg[extra]', and 'torchsig @ git+https://...'
                # all reduce to the bare distribution name.
                dep_name = (
                    raw.split("@")[0].split(">=")[0].split("==")[0].split("<=")[0].split("[")[0].split()[0].strip()
                )
                if dep_name in project_names and dep_name != p["name"]:
                    deps.add(dep_name)
            graph[p["name"]] = deps

        sorter = graphlib.TopologicalSorter(graph)
        sorter.prepare()
        ordered: List[str] = []
        while sorter.is_active():
            # sorted() here gives alphabetical tie-breaking within a level.
            for name in sorted(sorter.get_ready()):
                ordered.append(name)
                sorter.done(name)
        return [name_to_path[n] for n in ordered]

    def init_scripts(self) -> None:
        """Scan workspace and generate SwiftBar scripts for Jenkins and GitHub."""
        if not self._require_macos():
            return

        workspace_root = get_workspace_root()
        click.echo(f"Initializing SwiftBar scripts in {self.plugins_dir}...")

        # 1. Determine project traversal order (dependencies before dependents)
        ordered_dirs = self._dependency_ordered_projects(workspace_root)
        # Workspace root is not a project but is still monitored for Jenkins / GH remote.
        iteration_order = [*ordered_dirs, workspace_root]

        # 2. Monitored Jenkins jobs
        jenkins_home = DATA_DIR / "jenkins"
        jobs_dir = jenkins_home / "jobs"
        jenkins_projects = []

        # For GitHub script
        github_repos = []

        for p_dir in iteration_order:
            p_name = p_dir.name

            # Check Jenkins jobs
            if (jobs_dir / p_name).exists():
                jenkins_projects.append(p_name)
            if (jobs_dir / f"{p_name}.local").exists():
                jenkins_projects.append(f"{p_name}.local")

            # Check GitHub repo
            gh_repo = self._get_github_repo(p_dir)
            if gh_repo:
                github_repos.append(gh_repo)

        click.echo(f"  - Monitored Jenkins jobs: {', '.join(jenkins_projects)}")
        click.echo(f"  - Monitored GitHub repos: {', '.join(github_repos)}")

        # 3. Generate Jenkins Script
        self._generate_jenkins_script(jenkins_projects)

        # 4. Generate GitHub Script
        self._generate_github_script(github_repos)

        # 5. Finalize
        click.echo("SwiftBar scripts initialized.")

        if click.confirm(f"Do you want to point SwiftBar directly to {self.plugins_dir}?"):
            try:
                # 1. Stop SwiftBar to ensure settings are written cleanly
                self.stop()

                # 2. Set directory keys
                for key in ["PluginDirectory", "PluginsDirectory"]:
                    subprocess.run(
                        [
                            "defaults",
                            "write",
                            "com.ameba.SwiftBar",
                            key,
                            str(self.plugins_dir),
                        ],
                        check=True,
                    )

                # 3. Set helper keys
                subprocess.run(
                    [
                        "defaults",
                        "write",
                        "com.ameba.SwiftBar",
                        "MakePluginExecutable",
                        "-bool",
                        "YES",
                    ],
                    check=True,
                )

                # 4. Hide workspace documentation from the menu
                subprocess.run(
                    [
                        "defaults",
                        "write",
                        "com.ameba.SwiftBar",
                        "DisabledPlugins",
                        "-array",
                        "CLAUDE.md",
                        "AGENTS.md",
                        "GEMINI.md",
                    ],
                    check=True,
                )

                click.echo(f"  - SwiftBar configured to use: {self.plugins_dir}")

                # 5. Use URL scheme as well to trigger bookmark generation if app is running
                url = f"swiftbar://changePluginsDirectory?path={self.plugins_dir}"
                subprocess.run(["open", "-g", url], check=False)

                self.launch()
            except (OSError, subprocess.CalledProcessError) as e:
                failures.report(f"  - Error: failed to configure SwiftBar: {e}")
        else:
            click.echo(f"\nPlease manually point SwiftBar to: {self.plugins_dir}")

    def launch(self) -> None:
        """Start the SwiftBar application — installed first, once the user agrees, when it is missing."""
        if not self._require_macos():
            return
        if not swiftbar_installed():
            provision.ask_to_install("swiftbar")

        click.echo("Starting SwiftBar...")
        try:
            subprocess.run(["open", "-a", "SwiftBar"], check=True)
            click.echo("  - SwiftBar launched.")
        except subprocess.CalledProcessError as e:
            failures.report(f"  - Error launching SwiftBar: {e}")

    def stop(self) -> None:
        """Stop the SwiftBar application."""
        if not self._require_macos():
            return

        click.echo("Stopping SwiftBar...")
        try:
            subprocess.run(["pkill", "SwiftBar"], check=False)
            click.echo("  - SwiftBar stopped.")
        except OSError as e:
            failures.report(f"  - Error stopping SwiftBar: {e}")

    def restart(self, **kwargs: Any) -> None:  # Any: launch's options, each typed there
        """Restart SwiftBar."""
        if not self._require_macos():
            return
        self.stop()
        import time

        time.sleep(1)
        self.launch()

    def remove(self) -> None:
        """Clear SwiftBar configuration if it points to this workspace."""
        if not self._require_macos():
            return
        keys = ["PluginDirectory", "PluginsDirectory"]
        for key in keys:
            try:
                current = subprocess.check_output(["defaults", "read", "com.ameba.SwiftBar", key], text=True).strip()
                if current == str(self.plugins_dir):
                    click.echo(f"Clearing SwiftBar {key} setting (was {self.plugins_dir})...")
                    self.stop()
                    subprocess.run(["defaults", "delete", "com.ameba.SwiftBar", key], check=True)
                    click.echo(f"  - SwiftBar {key} configuration cleared.")
            except subprocess.CalledProcessError:
                # Key might not exist or app not installed
                pass
            except OSError as e:
                failures.report(f"  - Error clearing SwiftBar {key} configuration: {e}")

    def reset(self) -> None:
        """Alias for remove() to maintain consistency with other services."""
        self.remove()

    def status(self) -> None:
        """Display SwiftBar status."""
        if platform.system() != "Darwin":
            click.echo(f"  Swiftbar        : {click.style('Not supported (macOS only)', fg='yellow')}")
            return
        try:
            subprocess.check_output(["pgrep", "SwiftBar"])
            status_str = click.style("Running", fg="green")
        except subprocess.CalledProcessError:
            status_str = click.style("Stopped", fg="red")

        click.echo(f"  Swiftbar        : {status_str}")

    def info(self) -> None:
        """Show detailed SwiftBar configuration."""
        click.echo(click.style("Swiftbar:", bold=True))
        if platform.system() != "Darwin":
            click.echo(f"  Status        : {click.style('Not supported (macOS only)', fg='yellow')}")
            return
        try:
            subprocess.check_output(["pgrep", "SwiftBar"])
            click.echo(f"  Status        : {click.style('Running', fg='green')}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            click.echo(f"  Status        : {click.style('Stopped', fg='red')}")
        click.echo(f"  Plugins dir   : {self.plugins_dir}")
        if self.plugins_dir.exists():
            scripts = sorted(p.name for p in self.plugins_dir.iterdir() if p.is_file())
            if scripts:
                click.echo(f"  Plugin scripts: {', '.join(scripts)}")
            else:
                click.echo("  Plugin scripts: (none — run `aisland swiftbar init`)")
        click.echo(f"  App support   : {Path.home() / 'Library' / 'Application Support' / 'SwiftBar'}")

    def _generate_jenkins_script(self, projects: List[str]) -> None:
        """Generate the Jenkins status script."""
        script_path = self.plugins_dir / "jenkins_status.5m.sh"

        # Resolve JENKINS_HOME relative to aisland data
        # aisland/aisland/services/swiftbar.py -> aisland/data/jenkins
        jenkins_home = DATA_DIR / "jenkins"

        # Check for read access
        if not os.access(jenkins_home, os.R_OK):
            click.echo(
                click.style(
                    f"  [WARNING] No read access to Jenkins home: {jenkins_home}",
                    fg="yellow",
                )
            )
            click.echo("            The SwiftBar script might not display project status correctly.")
            click.echo("            Please check your terminal's permissions for the 'aisland' data directory.")

        projects_str = " ".join([f'"{p}"' for p in projects])
        # The menu links the Jenkins running now, on the port it runs on (else its default).
        jenkins_url = JenkinsService().url()

        content = f"""#!/bin/bash
# =========================================================================
# AUTO-GENERATED FILE — DO NOT EDIT BY HAND
# Generated by: aisland swiftbar init
# Source template: aisland/services/swiftbar.py (SwiftBarService._generate_jenkins_script)
# To regenerate:  aisland swiftbar init
# =========================================================================

# Jenkins Data Root
JENKINS_HOME="{jenkins_home}"
JOBS_DIR="${{JENKINS_HOME}}/jobs"

GLOBAL_FAILED=false
DROPDOWN_TEXT=""

# Define projects to check
PROJECTS=({projects_str})

# Function to process a job/branch directory
process_item() {{
    local ITEM_DIR="$1"
    local PROJECT_NAME="$2"
    local DISPLAY_NAME="$3"
    local IS_BRANCH="$4"

    CONFIG_XML="${{ITEM_DIR}}/config.xml"
    if [ -z "$DISPLAY_NAME" ] && [ -f "$CONFIG_XML" ]; then
        # Extract name from config.xml
        DISPLAY_NAME=$(sed -n 's/.*<displayName>\\([^<]*\\)<\\/displayName>.*/\\1/p' \\
            "$CONFIG_XML" | head -n 1)
        if [ -z "$DISPLAY_NAME" ]; then
            DISPLAY_NAME=$(sed -n 's/.*<sourceBranch>\\([^<]*\\)<\\/sourceBranch>.*/\\1/p' \\
                "$CONFIG_XML" | head -n 1)
        fi
        if [ -z "$DISPLAY_NAME" ]; then
            DISPLAY_NAME=$(sed -n 's/.*<name>\\([^<]*\\)<\\/name>.*/\\1/p' \\
                "$CONFIG_XML" | head -n 1)
        fi
    fi

    if [ -z "$DISPLAY_NAME" ]; then
        ITEM_NAME=$(basename "$ITEM_DIR")
        # Strip Jenkins hash (e.g., dev-main.k31dj -> dev-main)
        DISPLAY_NAME=$(echo "$ITEM_NAME" | cut -d. -f1)
        # Reconstruct common branch patterns (e.g., dev-main -> dev/main)
        if [[ "$DISPLAY_NAME" == "dev-main" ]]; then
            DISPLAY_NAME="dev/main"
        fi
    fi

    PERMALINKS="${{ITEM_DIR}}/builds/permalinks"
    STATUS_STR=""

    if [ -f "$PERMALINKS" ]; then
        LAST_COMPLETED=$(grep "lastCompletedBuild" "$PERMALINKS" | awk '{{print $2}}')

        if [ "$LAST_COMPLETED" == "-1" ] || [ -z "$LAST_COMPLETED" ]; then
            STATUS_STR="⚪️ (No builds)"
        else
            BUILD_XML="${{ITEM_DIR}}/builds/${{LAST_COMPLETED}}/build.xml"
            if [ -f "$BUILD_XML" ]; then
                RESULT=$(sed -n 's/.*<result>\\([^<]*\\)<\\/result>.*/\\1/p' "$BUILD_XML" | head -n 1)

                if [ "$RESULT" == "SUCCESS" ]; then
                    STATUS_STR="🟢"
                elif [ "$RESULT" == "FAILURE" ]; then
                    STATUS_STR="🔴"
                    GLOBAL_FAILED=true
                elif [ "$RESULT" == "ABORTED" ]; then
                    STATUS_STR="⚪️ (Aborted)"
                else
                    STATUS_STR="🟡 (${{RESULT}})"
                fi
            else
                STATUS_STR="❔ (xml missing)"
            fi
        fi
    else
        STATUS_STR="⚪️ (Not indexed)"
    fi

    if [ "$IS_BRANCH" == "true" ]; then
        ENCODED_BRANCH=$(echo "$DISPLAY_NAME" | sed 's/\\//%2F/g')
        URL="{jenkins_url}/job/${{PROJECT_NAME}}/job/${{ENCODED_BRANCH}}"
        DROPDOWN_TEXT+="${{DISPLAY_NAME}}: ${{STATUS_STR}} | href=${{URL}}\\n"
    else
        URL="{jenkins_url}/job/${{PROJECT_NAME}}"
        DROPDOWN_TEXT+="Status: ${{STATUS_STR}} | href=${{URL}}\\n"
    fi
}}

for PROJECT in "${{PROJECTS[@]}}"; do
    PROJECT_DIR="${{JOBS_DIR}}/${{PROJECT}}"

    if [ ! -d "$PROJECT_DIR" ]; then
        continue
    fi

    DROPDOWN_TEXT+="${{PROJECT}} | color=gray size=11\\n"

    BRANCHES_DIR="${{PROJECT_DIR}}/branches"
    if [ -d "$BRANCHES_DIR" ]; then
        # Handle multibranch pipeline
        HAS_BRANCHES=false
        for BRANCH_DIR_PATH in "${{BRANCHES_DIR}}"/*; do
            if [ ! -d "$BRANCH_DIR_PATH" ]; then continue; fi
            HAS_BRANCHES=true
            process_item "$BRANCH_DIR_PATH" "$PROJECT" "" "true"
        done
        if [ "$HAS_BRANCHES" = false ]; then
            DROPDOWN_TEXT+="No branches discovered | color=red\\n"
        fi
    elif [ -d "${{PROJECT_DIR}}/builds" ]; then
        # Handle direct job (like .local jobs)
        process_item "$PROJECT_DIR" "$PROJECT" "$PROJECT" "false"
    else
        DROPDOWN_TEXT+="No builds or branches found | color=red\\n"
    fi
    DROPDOWN_TEXT+="---\\n"
done

if [ "$GLOBAL_FAILED" = true ]; then
    echo "Jenkins: 🔴"
else
    echo "Jenkins: 🟢"
fi

echo "---"
echo -e "$DROPDOWN_TEXT"
echo "---"
echo "Refresh Now | refresh=true"
echo "Open Jenkins | href={jenkins_url}"
"""
        script_path.write_text(content)
        script_path.chmod(0o755)
        click.echo(f"  - Generated {script_path.name}")

    def _generate_github_script(self, github_repos: List[str]) -> None:
        """Generate the GitHub status script."""
        script_path = self.plugins_dir / "gh_status.10m.sh"

        repos_str = "\\n".join(github_repos)

        content = f"""#!/bin/bash
# =========================================================================
# AUTO-GENERATED FILE — DO NOT EDIT BY HAND
# Generated by: aisland swiftbar init
# Source template: aisland/services/swiftbar.py (SwiftBarService._generate_github_script)
# To regenerate:  aisland swiftbar init
# =========================================================================

# Configuration
if ! command -v gh &> /dev/null; then
    echo "GH: ⚠️"
    echo "---"
    echo "GitHub CLI (gh) not found."
    exit 0
fi

# Monitored repositories
REPOS=$(echo -e "{repos_str}")

if [ -z "$REPOS" ]; then
    echo "GH: ⚪️"
    echo "---"
    echo "No GitHub repositories found in workspace."
    exit 0
fi

GLOBAL_FAILED=false
GLOBAL_RUNNING=false
GLOBAL_SUCCESS=false
DROPDOWN_TEXT=""

for REPO in $REPOS; do
    # Get last run status
    LAST_RUN=$(gh run list --repo "$REPO" --limit 1 --json status,conclusion,workflowName --jq '.[0]' 2>/dev/null)

    # If LAST_RUN is empty or "null", there are no runs
    if [ -z "$LAST_RUN" ] || [ "$LAST_RUN" == "null" ]; then
        DROPDOWN_TEXT+="${{REPO}}: ⚪️ (No runs) | href=https://github.com/${{REPO}}/actions\\n"
        continue
    fi

    STATUS=$(echo "$LAST_RUN" | jq -r '.status')
    CONCLUSION=$(echo "$LAST_RUN" | jq -r '.conclusion')
    WORKFLOW=$(echo "$LAST_RUN" | jq -r '.workflowName')

    if [ "$STATUS" == "in_progress" ] || [ "$STATUS" == "queued" ]; then
        GLOBAL_RUNNING=true
        DROPDOWN_TEXT+="${{REPO}}: 🟡 (Running) | href=https://github.com/${{REPO}}/actions\\n"
    elif [ "$CONCLUSION" == "failure" ]; then
        GLOBAL_FAILED=true
        DROPDOWN_TEXT+="${{REPO}}: 🔴 (Failed: ${{WORKFLOW}}) | href=https://github.com/${{REPO}}/actions\\n"
    elif [ "$CONCLUSION" == "success" ]; then
        GLOBAL_SUCCESS=true
        DROPDOWN_TEXT+="${{REPO}}: 🟢 | href=https://github.com/${{REPO}}/actions\\n"
    elif [ "$CONCLUSION" == "null" ] || [ -z "$CONCLUSION" ]; then
        # This can happen if it is currently running or just completed without conclusion yet
        if [ "$STATUS" == "completed" ]; then
             DROPDOWN_TEXT+="${{REPO}}: ⚪️ (Completed, no status) | href=https://github.com/${{REPO}}/actions\\n"
        else
             GLOBAL_RUNNING=true
             DROPDOWN_TEXT+="${{REPO}}: 🟡 (Running) | href=https://github.com/${{REPO}}/actions\\n"
        fi
    else
        DROPDOWN_TEXT+="${{REPO}}: 🟡 (${{CONCLUSION}}) | href=https://github.com/${{REPO}}/actions\\n"
    fi
done

if [ "$GLOBAL_FAILED" = true ]; then
    echo "Actions: 🔴"
elif [ "$GLOBAL_RUNNING" = true ]; then
    echo "Actions: 🟡"
elif [ "$GLOBAL_SUCCESS" = true ]; then
    echo "Actions: 🟢"
else
    echo "Actions: ⚪️"
fi

echo "---"
echo -e "$DROPDOWN_TEXT"
echo "---"
echo "Refresh Now | refresh=true"
"""
        script_path.write_text(content)
        script_path.chmod(0o755)
        click.echo(f"  - Generated {script_path.name}")
