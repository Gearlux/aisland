# Jenkins Setup Workflow

This document describes the end-to-end workflow for setting up and managing a local Jenkins CI/CD environment using the `aisland` tool.

## Phase 1: Service Startup

### 1. Start Jenkins
To download the base Jenkins binary (if missing) and launch the server:
```bash
aisland jenkins start --background
```
- **Default URL:** [http://localhost:8080](http://localhost:8080)
- **Home Directory:** `aisland/data/jenkins/`

*Note: The `start` command intentionally does NOT aggressively install plugins. It simply brings the server up and checks if you have configured credentials.*

### 2. Initial Setup & Unlock
If this is a clean installation, `aisland jenkins start` will detect that no credentials exist and print instructions to the console. 
The `aisland` tool skips the Jenkins setup wizard automatically.

- **Username:** `admin` (or your OS username)
- **Password:** If prompted for a password, you can retrieve the initial one by running:
```bash
aisland jenkins status
```
Paste this into the Jenkins web interface to unlock it.

---

## Phase 2: Configuration & Plugins

### 1. Add Git Credentials (Manual Step)
Since the Gearlux projects use private GitHub repositories, you must add your credentials to Jenkins *before* initializing pipelines:
1.  Navigate to **Manage Jenkins** -> **Credentials** -> **System** -> **Global credentials (unrestricted)**.
2.  Click **Add Credentials**.
3.  **Kind:** SSH Username with private key OR Username with password (personal access token).
4.  **ID:** You **MUST** enter exactly `aisland-github` in this field. If it is named anything else, local pipeline creation will fail to scan GitHub.
5.  **Username:** Your GitHub username.
6.  **Password/Key:** Your GitHub Personal Access Token or Private SSH Key.

### 2. Install Plugins & Initialize Pipelines
Once credentials are in place, use the `init` command. This single command handles plugin management and job seeding:

#### Initialize All Projects:
```bash
aisland jenkins init
```
This command performs the following automated workflow:
1. **Plugin Audit:** Checks if all required plugins are installed.
2. **Plugin Setup:** If missing, it downloads them using the Jenkins Plugin Manager.
3. **Safe Restart:** If Jenkins is already running and plugins/jobs were added, it issues a Safe Restart directly to the Jenkins API so your jobs load gracefully. If Jenkins is not running, it starts it.
4. **Pipeline Seeding:** Discovers all projects in the workspace containing a `Jenkinsfile`, determines their Git remote, and automatically links any GitHub repository to the `aisland-github` credential. It creates a **Multibranch Pipeline** job for each project.

#### Initialize a Single Project:
```bash
aisland jenkins init logflow
```

---

## Command Reference

| Command | Description |
| :--- | :--- |
| `aisland jenkins start` | Starts the Jenkins server (use `--background` or `-b` for detached mode). |
| `aisland jenkins stop` | Stops the running Jenkins server. |
| `aisland jenkins status` | Shows if Jenkins is running, its PID, and the Initial Admin Password. |
| `aisland jenkins init [PROJECT]` | Creates Multibranch Pipeline jobs for projects in the workspace. |
| `aisland jenkins remove [PROJECT]` | Removes a specific project pipeline or all pipelines from Jenkins. |
| `aisland jenkins reset` | Stops Jenkins and wipes ALL data, including jobs, logs, and configurations. |
| `aisland jenkins reset --project PROJECT` | Removes the pipeline and build data for a specific project. |

---

## Troubleshooting

### Plugin Failures
If Jenkins reports missing dependencies during startup, ensure you are using the latest version of the `aisland` tool and perform a `aisland jenkins reset` to force a clean re-download of the plugin suite.

### Credentials Matching
The `init` command aggressively expects credentials for GitHub repositories to be named exactly `aisland-github`. If it warns you during `init` that it cannot find this ID, delete your current credentials in Jenkins and recreate them, ensuring the **ID** field explicitly says `aisland-github`.
