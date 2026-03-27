# Jenkins Setup Workflow

This document describes the end-to-end workflow for setting up and managing a local Jenkins CI/CD environment using the `sandbox` tool.

## Phase 1: Service Lifecycle

### 1. Start Jenkins
To download the binary (if missing), install essential plugins, and launch the server:
```bash
sandbox jenkins start --background
```
- **Default URL:** [http://localhost:8080](http://localhost:8080)
- **Home Directory:** `sandbox/data/jenkins/`

### 2. Initial Setup
The `sandbox` tool automatically skips the Jenkins setup wizard and pre-installs all necessary plugins and their dependencies.

- **Username:** `admin` (or your OS username)
- **Password:** If prompted for a password, you can retrieve the initial one by running:
```bash
sandbox jenkins status
```
Paste this into the Jenkins web interface if security is enabled.

---

## Phase 2: Configuration & Credentials

### 1. Add Git Credentials (Manual Step)
Since the Gearlux projects use private GitHub repositories, you must add your credentials to Jenkins:
1.  Navigate to **Manage Jenkins** -> **Credentials** -> **System** -> **Global credentials (unrestricted)**.
2.  Click **Add Credentials**.
3.  **Kind:** SSH Username with private key OR Username with password (personal access token).
4.  **ID:** Use something descriptive (e.g., `github-gertbehi`). 
5.  **Username:** Your GitHub username.
6.  **Password/Key:** Your GitHub Personal Access Token or Private SSH Key.

### 2. Initialize Project Pipelines
Once credentials are in place, use the `init` command to automatically seed the Gearlux project jobs:

#### Initialize All Projects:
```bash
sandbox jenkins init
```
This discovers all projects in the workspace containing a `Jenkinsfile`, determines their Git remote, fuzzy-matches the credentials you added, and creates a **Multibranch Pipeline** job for each.

#### Initialize a Single Project:
```bash
sandbox jenkins init logflow
```

---

## Command Reference

| Command | Description |
| :--- | :--- |
| `sandbox jenkins start` | Starts the Jenkins server (use `--background` or `-b` for detached mode). |
| `sandbox jenkins stop` | Stops the running Jenkins server. |
| `sandbox jenkins status` | Shows if Jenkins is running, its PID, and the Initial Admin Password. |
| `sandbox jenkins init [PROJECT]` | Creates Multibranch Pipeline jobs for projects in the workspace. |
| `sandbox jenkins remove [PROJECT]` | Removes a specific project pipeline or all pipelines from Jenkins. |
| `sandbox jenkins reset` | Stops Jenkins and wipes ALL data, including jobs, logs, and configurations. |
| `sandbox jenkins reset --project PROJECT` | Removes the pipeline and build data for a specific project. |

---

## Troubleshooting

### Plugin Failures
If Jenkins reports missing dependencies during startup, ensure you are using the latest version of the `sandbox` tool and perform a `sandbox jenkins reset` to force a clean re-download of the plugin suite.

### Credentials Matching
The `init` command uses fuzzy matching to link your GitHub remote URL to your Jenkins credentials. If it fails to find a match, ensure your credential **Username** or **Description** contains part of the repository URL (e.g., your GitHub username or the word "gearlux").
