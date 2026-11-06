# Jenkins

A Jenkins of the workspace's own, on this machine: started, unlocked, given the GitHub credential, then seeded with a
job per project. The Java Jenkins runs on, the nightly run and each stage's time limit are on
[services](services.md#manage-jenkins); the files the jobs run, on [ci](ci.md).

## Starting Jenkins

### 1. Start Jenkins
To download the base Jenkins binary (if missing) and launch the server:
```bash
aisland jenkins start --background
```
- **Default URL:** [http://localhost:9080](http://localhost:9080). Jenkins listens on this machine only
  (`127.0.0.1`): another machine on the network cannot reach it. From a remote machine, forward the port (the
  [tunnel](tunnel.md) does).
- **Home Directory:** `.aisland/data/jenkins/` in the workspace (`aisland/data/jenkins/` when aisland runs from its own checkout)
- **Which Jenkins:** one long-term-support release, pinned by version and sha256 (`JenkinsService.WAR_VERSION` and
  `WAR_SHA256`), downloaded from `get.jenkins.io` once and kept. A download that does not match the digest is refused
  and nothing is installed. A WAR already in `.aisland/bin/jenkins.war` (`aisland/bin/jenkins.war` from a checkout) is
  used as it is; delete it to get the pinned release.
- **Its plugin manager:** the plugin installation manager `aisland jenkins init` runs is pinned the same way
  (`JenkinsService.MANAGER_VERSION` and `MANAGER_SHA256`), downloaded from its GitHub release and checked before it
  is ever run.

*Note: `start` does not install plugins. It brings the server up and checks whether you have configured credentials.*

### 2. Initial Setup & Unlock
If this is a clean installation, `aisland jenkins start` will detect that no credentials exist and print instructions to the console. 
The first start keeps Jenkins' setup wizard, so security is on from the start. Unlock it with the initial admin
password. aisland never prints the password itself; `aisland jenkins start`, `status` and `info` name the file that
holds it:
```bash
cat .aisland/data/jenkins/secrets/initialAdminPassword   # aisland/data/jenkins/… from aisland's own checkout
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
one question:

```text
Allow anonymous read access? (the nightly report, the SwiftBar menu and the scaffold's linter read Jenkins without logging in; the login and CSRF protection stay) [y/N]:
```

A yes lets a visitor who has not logged in read jobs and builds. The login and CSRF protection always stay on: Jenkins
listens on this machine only, but a page open in your browser can still send it requests.

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
| `aisland jenkins status` | Shows if Jenkins is running, its PID, and the file holding the initial admin password. |
| `aisland jenkins restart` | Stops and starts Jenkins (`--port`, `--background`). |
| `aisland jenkins info` | Shows its URL, `JENKINS_HOME` and the file holding the initial admin password. |
| `aisland jenkins init [PROJECT]` | Creates Multibranch Pipeline jobs for projects in the workspace, then restarts Jenkins. |
| `aisland jenkins init --local [PROJECT]` | Creates `<project>.local` jobs that run `Jenkinsfile.local` on this machine's checkout. |
| `aisland jenkins scaffold [PROJECT]` | Writes a project's `Jenkinsfile`, `Jenkinsfile.local` and GitHub Actions `ci.yml` ([ci](ci.md)). |
| `aisland jenkins templates` | Shows the template set `scaffold` writes from, and copies aisland's to start your own ([ci](ci.md)). |
| `aisland jenkins remove [PROJECT]` | Removes a specific project pipeline or all pipelines from Jenkins. |
| `aisland jenkins reset` | Stops Jenkins and wipes ALL data, including jobs, logs, and configurations. |
| `aisland jenkins reset --project PROJECT` | Removes the pipeline and build data for a specific project. |

---

## Troubleshooting

### A plugin is missing

When Jenkins reports a missing plugin or dependency at start, run `aisland jenkins init` again: it installs every
required plugin that is missing and keeps the jobs. `aisland jenkins reset` downloads every plugin again too, but it
deletes every job, build and setting first, so keep it for a Jenkins you mean to start over.

### The credential is not found

`init` looks for the GitHub credential under the ID `aisland-github` exactly. When it warns that it found none, open
the credential in Jenkins and set its **ID** to `aisland-github`. Jenkins cannot rename an ID, so delete the credential
and add it again with that ID, then run `aisland jenkins init` again.
