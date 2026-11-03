# Jenkins Setup Workflow

This document describes the end-to-end workflow for setting up and managing a local Jenkins CI/CD environment using the `aisland` tool.
The Java Jenkins runs on, the nightly run and each stage's time limit are on [services](services.md#manage-jenkins);
the files the jobs run, on [ci](ci.md).

## Starting Jenkins

### 1. Start Jenkins
To download the base Jenkins binary (if missing) and launch the server:
```bash
aisland jenkins start --background
```
- **Default URL:** [http://localhost:9080](http://localhost:9080)
- **Home Directory:** `.aisland/data/jenkins/` in the workspace (`aisland/data/jenkins/` when aisland runs from its own checkout)
- **Which Jenkins:** the current long-term-support WAR from `get.jenkins.io` (`war-stable/latest`), downloaded once and
  kept. It is not pinned to a version or checked against a digest, unlike the tools `aisland setup` installs; delete
  `.aisland/bin/jenkins.war` (`aisland/bin/jenkins.war` from a checkout) to fetch the current one.

*Note: `start` does not install plugins. It brings the server up and checks whether you have configured credentials.*

### 2. Initial Setup & Unlock
If this is a clean installation, `aisland jenkins start` will detect that no credentials exist and print instructions to the console. 
The first start keeps Jenkins' setup wizard, so security is on from the start. Unlock it with the initial admin password; `aisland jenkins start` prints it, and `aisland jenkins status` prints it as `Initial Admin Password:` while Jenkins runs:
```bash
aisland jenkins status
```
Paste it into the Jenkins web interface to unlock it, then create your admin user.

---

## Credentials, Plugins and Pipelines

### 1. Add Git Credentials (Manual Step)
If your projects use private GitHub repositories, add your credentials to Jenkins *before* initializing pipelines:
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
This command performs the following steps:
1. **Plugins:** checks that every required plugin is installed and downloads the missing ones with the Jenkins
   Plugin Manager.
2. **Pipelines:** finds every project of the workspace with a `Jenkinsfile`, determines its Git remote, links a
   GitHub repository to the `aisland-github` credential and writes a **Multibranch Pipeline** job for it, then the
   orchestrator job that builds them all.
3. **Restart:** Jenkins must be stopped while its `config.xml` is edited — a running Jenkins writes its own copy over
   the changes. A stopped Jenkins is first started once, so that it writes `config.xml`; then Jenkins is **stopped**,
   the dashboard view and the workspace's global properties are written, and Jenkins is started again in the
   background. A Jenkins that was running is restarted the same way: builds running at that moment stop.

`aisland jenkins init --local` seeds `<project>.local` jobs instead, each with its `Jenkinsfile.local` embedded: the
pipeline runs on this machine's checkout and workspace `.venv`, with nothing fetched from Git. It also seeds the
nightly orchestrator and the job that reports its nightly runs (`aisland tests nightly`). Before the restart it asks
two questions, for a Jenkins only you reach:

```text
Reduce Jenkins security for faster local access? (Disables CSRF, allows anonymous read) [y/N]:
Disable Jenkins security entirely? (No login required, most fast/unsecure) [y/N]:
```

The second is asked only after a yes to the first; a no to it keeps the login, allowing anonymous read and turning
CSRF protection off.

#### Initialize a Single Project:
```bash
aisland jenkins init models
```

---

## Command Reference

| Command | Description |
| :--- | :--- |
| `aisland jenkins start` | Starts the Jenkins server (use `--background` or `-b` for detached mode). |
| `aisland jenkins stop` | Stops the running Jenkins server. |
| `aisland jenkins status` | Shows if Jenkins is running, its PID, and the Initial Admin Password. |
| `aisland jenkins restart` | Stops and starts Jenkins (`--port`, `--background`). |
| `aisland jenkins info` | Shows its URL, `JENKINS_HOME` and the initial admin password. |
| `aisland jenkins init [PROJECT]` | Creates Multibranch Pipeline jobs for projects in the workspace, then restarts Jenkins. |
| `aisland jenkins init --local [PROJECT]` | Creates `<project>.local` jobs that run `Jenkinsfile.local` on this machine's checkout. |
| `aisland jenkins scaffold [PROJECT]` | Writes a project's `Jenkinsfile`, `Jenkinsfile.local` and GitHub Actions `ci.yml` ([ci](ci.md)). |
| `aisland jenkins templates` | Shows the template set `scaffold` writes from, and copies aisland's to start your own ([ci](ci.md)). |
| `aisland jenkins remove [PROJECT]` | Removes a specific project pipeline or all pipelines from Jenkins. |
| `aisland jenkins reset` | Stops Jenkins and wipes ALL data, including jobs, logs, and configurations. |
| `aisland jenkins reset --project PROJECT` | Removes the pipeline and build data for a specific project. |

---

## Troubleshooting

### Plugin Failures
If Jenkins reports missing dependencies during startup, ensure you are using the latest version of the `aisland` tool and perform a `aisland jenkins reset` to force a clean re-download of the plugin suite.

### Credentials Matching
The `init` command expects credentials for GitHub repositories to be named exactly `aisland-github`. If it warns you during `init` that it cannot find this ID, delete your current credentials in Jenkins and recreate them, ensuring the **ID** field explicitly says `aisland-github`.
