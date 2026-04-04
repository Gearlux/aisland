import os
import subprocess
from typing import Any, List

import click

from aisland.manager import DATA_DIR, BaseService, get_workspace_root


class SwiftBarService(BaseService):
    """SwiftBar Plugin Management Service."""

    def __init__(self) -> None:
        super().__init__("swiftbar")
        self.plugins_dir = self.service_data_dir / "plugins"
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

    def init_scripts(self) -> None:
        """Scan workspace and generate SwiftBar scripts for Jenkins and GitHub."""
        workspace_root = get_workspace_root()
        click.echo(f"Initializing SwiftBar scripts in {self.plugins_dir}...")

        # 1. Discover Projects
        from aisland.manager import discover_projects

        base_projects = [p.parent.name for p in discover_projects(workspace_root)]

        # Include both standard and .local projects if they exist in Jenkins
        jenkins_home = DATA_DIR / "jenkins"
        jobs_dir = jenkins_home / "jobs"
        projects = []
        for p in base_projects:
            if (jobs_dir / p).exists():
                projects.append(p)
            if (jobs_dir / f"{p}.local").exists():
                projects.append(f"{p}.local")

        click.echo(f"  - Monitored Jenkins jobs: {', '.join(projects)}")

        # 2. Generate Jenkins Script
        self._generate_jenkins_script(projects)

        # 3. Generate GitHub Script
        self._generate_github_script()

        # 4. Finalize
        click.echo("SwiftBar scripts initialized.")

        if click.confirm(f"Do you want to point SwiftBar directly to {self.plugins_dir}?"):
            try:
                # 1. Stop SwiftBar to ensure settings are written cleanly
                self.stop()

                # 2. Set directory keys
                for key in ["PluginDirectory", "PluginsDirectory"]:
                    subprocess.run(["defaults", "write", "com.ameba.SwiftBar", key, str(self.plugins_dir)], check=True)

                # 3. Set helper keys
                subprocess.run(
                    ["defaults", "write", "com.ameba.SwiftBar", "MakePluginExecutable", "-bool", "YES"], check=True
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
            except Exception as e:
                click.echo(click.style(f"  [ERROR] Failed to configure SwiftBar: {e}", fg="red"))
        else:
            click.echo(f"\nPlease manually point SwiftBar to: {self.plugins_dir}")

    def launch(self) -> None:
        """Start the SwiftBar application."""
        if os.name != "posix":
            click.echo("SwiftBar is only supported on macOS.")
            return

        click.echo("Starting SwiftBar...")
        try:
            subprocess.run(["open", "-a", "SwiftBar"], check=True)
            click.echo("  - SwiftBar launched.")
        except subprocess.CalledProcessError as e:
            click.echo(f"  - Error launching SwiftBar: {e}")

    def stop(self) -> None:
        """Stop the SwiftBar application."""
        if os.name != "posix":
            click.echo("SwiftBar is only supported on macOS.")
            return

        click.echo("Stopping SwiftBar...")
        try:
            subprocess.run(["pkill", "SwiftBar"], check=False)
            click.echo("  - SwiftBar stopped.")
        except Exception as e:
            click.echo(f"  - Error stopping SwiftBar: {e}")

    def restart(self, **kwargs: Any) -> None:
        """Restart SwiftBar."""
        self.stop()
        import time

        time.sleep(1)
        self.launch()

    def remove(self) -> None:
        """Clear SwiftBar configuration if it points to this workspace."""
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
            except Exception as e:
                click.echo(f"  - Error clearing SwiftBar {key} configuration: {e}")

    def reset(self) -> None:
        """Alias for remove() to maintain consistency with other services."""
        self.remove()

    def status(self) -> None:
        """Display SwiftBar status."""
        try:
            subprocess.check_output(["pgrep", "SwiftBar"])
            status_str = click.style("Running", fg="green")
        except subprocess.CalledProcessError:
            status_str = click.style("Stopped", fg="red")

        click.echo(f"  Swiftbar        : {status_str}")

    def _generate_jenkins_script(self, projects: List[str]) -> None:
        """Generate the Jenkins status script."""
        script_path = self.plugins_dir / "jenkins_status.5m.sh"

        # Resolve JENKINS_HOME relative to aisland data
        # aisland/aisland/services/swiftbar.py -> aisland/data/jenkins
        jenkins_home = DATA_DIR / "jenkins"

        # Check for read access
        if not os.access(jenkins_home, os.R_OK):
            click.echo(click.style(f"  [WARNING] No read access to Jenkins home: {jenkins_home}", fg="yellow"))
            click.echo("            The SwiftBar script might not display project status correctly.")
            click.echo("            Please check your terminal's permissions for the 'aisland' data directory.")

        projects_str = " ".join([f'"{p}"' for p in projects])

        content = f"""#!/bin/bash
# Generated by aisland SwiftBarService

# Jenkins Data Root
JENKINS_HOME="{jenkins_home}"
JOBS_DIR="${{JENKINS_HOME}}/jobs"

GLOBAL_FAILED=false
DROPDOWN_TEXT=""

# Define projects to check
PROJECTS=({projects_str})

for PROJECT in "${{PROJECTS[@]}}"; do
    PROJECT_DIR="${{JOBS_DIR}}/${{PROJECT}}"

    if [ ! -d "$PROJECT_DIR" ]; then
        continue
    fi

    DROPDOWN_TEXT+="${{PROJECT}} | color=gray size=11\\n"

    BRANCHES_DIR="${{PROJECT_DIR}}/branches"
    if [ -d "$BRANCHES_DIR" ]; then
        for BRANCH_DIR_PATH in "${{BRANCHES_DIR}}"/*; do
            if [ ! -d "$BRANCH_DIR_PATH" ]; then continue; fi

            BRANCH_DIR_NAME=$(basename "$BRANCH_DIR_PATH")

            CONFIG_XML="${{BRANCH_DIR_PATH}}/config.xml"
            DISPLAY_NAME=""
            if [ -f "$CONFIG_XML" ]; then
                # Extract branch name from config.xml
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
                # Strip Jenkins hash (e.g., dev-main.k31dj -> dev-main)
                DISPLAY_NAME=$(echo "$BRANCH_DIR_NAME" | cut -d. -f1)
                # Reconstruct common branch patterns (e.g., dev-main -> dev/main)
                if [[ "$DISPLAY_NAME" == "dev-main" ]]; then
                    DISPLAY_NAME="dev/main"
                fi
            fi

            PERMALINKS="${{BRANCH_DIR_PATH}}/builds/permalinks"
            STATUS_STR=""

            if [ -f "$PERMALINKS" ]; then
                LAST_COMPLETED=$(grep "lastCompletedBuild" "$PERMALINKS" | awk '{{print $2}}')

                if [ "$LAST_COMPLETED" == "-1" ] || [ -z "$LAST_COMPLETED" ]; then
                    STATUS_STR="⚪️ (No builds)"
                else
                    BUILD_XML="${{BRANCH_DIR_PATH}}/builds/${{LAST_COMPLETED}}/build.xml"
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

            ENCODED_BRANCH=$(echo "$DISPLAY_NAME" | sed 's/\\//%2F/g')
            BRANCH_URL="http://localhost:8080/job/${{PROJECT}}/job/${{ENCODED_BRANCH}}"

            DROPDOWN_TEXT+="${{DISPLAY_NAME}}: ${{STATUS_STR}} | href=${{BRANCH_URL}}\\n"
        done
    else
        DROPDOWN_TEXT+="No branches found | color=red\\n"
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
echo "Open Jenkins | href=http://localhost:8080"
"""
        script_path.write_text(content)
        script_path.chmod(0o755)
        click.echo(f"  - Generated {script_path.name}")

    def _generate_github_script(self) -> None:
        """Generate the GitHub status script."""
        script_path = self.plugins_dir / "gh_status.10m.sh"

        content = """#!/bin/bash
# Generated by aisland SwiftBarService

# Configuration
# Fetch pinned repositories using GraphQL
if ! command -v gh &> /dev/null; then
    echo "GH: ⚠️"
    echo "---"
    echo "GitHub CLI (gh) not found."
    exit 0
fi

REPOS=$(gh api graphql -f query='
  query {
    viewer {
      pinnedItems(first: 30, types: REPOSITORY) {
        nodes {
          ... on Repository {
            nameWithOwner
          }
        }
      }
    }
  }
' --jq '.data.viewer.pinnedItems.nodes[].nameWithOwner' 2>/dev/null)

if [ -z "$REPOS" ]; then
    echo "GH: ⚪️"
    echo "---"
    echo "No pinned repositories found or GH API error."
    exit 0
fi

GLOBAL_FAILED=false
DROPDOWN_TEXT=""

for REPO in $REPOS; do
    LAST_RUN=$(gh run list --repo "$REPO" --limit 1 --json conclusion,workflowName --jq '.[0]' 2>/dev/null)

    CONCLUSION=$(echo "$LAST_RUN" | jq -r '.conclusion')
    WORKFLOW=$(echo "$LAST_RUN" | jq -r '.workflowName')

    if [ "$CONCLUSION" == "failure" ]; then
        GLOBAL_FAILED=true
        DROPDOWN_TEXT+="${REPO}: 🔴 (Failed: ${WORKFLOW}) | href=https://github.com/${REPO}/actions\\n"
    elif [ "$CONCLUSION" == "success" ]; then
        DROPDOWN_TEXT+="${REPO}: 🟢 | href=https://github.com/${REPO}/actions\\n"
    elif [ "$CONCLUSION" == "null" ] || [ -z "$CONCLUSION" ]; then
        DROPDOWN_TEXT+="${REPO}: ⚪️ (No runs) | href=https://github.com/${REPO}/actions\\n"
    else
        DROPDOWN_TEXT+="${REPO}: 🟡 (${CONCLUSION}) | href=https://github.com/${REPO}/actions\\n"
    fi
done

if [ "$GLOBAL_FAILED" = true ]; then
    echo "Actions: 🔴"
else
    echo "Actions: 🟢"
fi

echo "---"
echo -e "$DROPDOWN_TEXT"
echo "---"
echo "Refresh Now | refresh=true"
"""
        script_path.write_text(content)
        script_path.chmod(0o755)
        click.echo(f"  - Generated {script_path.name}")
