# Development Aisland

This directory contains the infrastructure and server management tools for the development environment. It is designed to be cross-platform and easily portable across development machines.

## Prerequisites
- **Python 3.12+**
- **Java OpenJDK 17 or higher** (Required for Jenkins)

## Default Ports

| Service       | Port | URL                        |
|---------------|------|----------------------------|
| Jenkins       | 9080 | http://localhost:9080      |
| Label Studio  | 9081 | http://localhost:9081      |
| MLflow        | 5005 | http://localhost:5005      |

All ports are configurable via `--port` flag.

## Setup & Usage

### Bootstrap

`bash aisland/setup.sh` (run from the workspace root) provisions the
`.venv`, installs aisland in editable mode, hands off to `aisland setup`
for project provisioning, installs liquifai shell completion for every
Liquifai CLI in the venv into a workspace-local `.project.bashrc.completion`
file (sourced by `project.bashrc`, gitignored — so the user's global
`~/.bashrc` stays clean and re-running setup never dirties the tree),
and finally runs `setup_git.sh` to configure submodule + git aliases.

`aisland setup` also writes a workspace-root **`pyrightconfig.json`** so
Pyright / Pylance resolve cross-project imports against the same project
set installed editable into the `.venv`. It uses relative paths only
(`venvPath: "."`, plus each project dir on `extraPaths`), so the file is
portable across machines — and it is gitignored + regenerated on every
setup, so it always tracks the live project list and never dirties the
tree. Don't hand-edit it; add a project's `pyproject.toml` and re-run
`aisland setup`.

#### Optional submodules & the generated `uv.toml`

Some projects are **optional** — they ship their own `uv.toml` (a private
package index) or are marked `active = false` in `.gitmodules` (e.g. the
corporate-only `vendorcli` / `signals-bridge`). `aisland setup` **prompts**
whether to activate each one and remembers the answer **per-machine** in
`.git/config` (`submodule.<name>.active`), never in the committed
`.gitmodules`. A decided module is honored silently on later runs; a *new*
optional module is prompted **even with `--non-interactive/-y`** (a machine
must consciously opt into a private project). Drive it non-interactively with
`--activate <name>` / `--deactivate <name>` (repeatable). Activating runs
`git submodule update --init`; deactivating deinits the working tree.

```bash
aisland setup                          # interactive: prompts for optional submodules
aisland setup -y                       # reuse saved decisions; still ask about new modules
aisland setup --activate vendorcli        # opt this machine into vendorcli (no prompt)
aisland setup --deactivate vendorcli      # opt out (deinits its working tree)
```

From the activated set, setup **generates the workspace-root `uv.toml`** =
a workspace base (e.g. `override-dependencies`) merged with each *activated*
project's own `uv.toml` (union of `extra-index-url`, etc.). Like
`pyrightconfig.json`, it is **gitignored and regenerated** on every setup, so
a private index (the Company Artifactory in `vendorcli/uv.toml`) lands in the
root file **only on a machine that activates that project** — a non-corporate
machine that skips it never tries to contact the unreachable index.

### 1. Check Status
Retrieve service information for all infrastructure components:
```bash
aisland status
```

### 2. Manage Jenkins
For a complete guide on setting up project pipelines and credentials, see the [Jenkins Setup Workflow](JENKINS_WORKFLOW.md).

```bash
# Start Jenkins (foreground)
aisland jenkins start

# Start Jenkins (background)
aisland jenkins start --background

# Stop/Status
aisland jenkins stop
aisland jenkins status
```
Default: [http://localhost:9080](http://localhost:9080)

### 3. Manage MLflow
```bash
# Start MLflow (foreground)
aisland mlflow start

# Start MLflow (background)
aisland mlflow start --background

# Stop/Status
aisland mlflow stop
aisland mlflow status
```
Default: [http://localhost:5005](http://localhost:5005)

### 4. Manage Docker
The `act` tool and other containerized services require a Docker engine. Aisland uses **Colima** for a lightweight, CLI-first Docker experience on macOS.

```bash
# Start Docker engine
aisland docker start

# Stop Docker engine
aisland docker stop

# Check Docker status
aisland docker status
```

### 5. Manage Label Studio
```bash
# Start Label Studio (foreground)
aisland labelstudio start

# Start Label Studio (background)
aisland labelstudio start --background

# Stop/Status
aisland labelstudio stop
aisland labelstudio status
```
Default: [http://localhost:9081](http://localhost:9081)

## Jenkins Configuration
- **JENKINS_HOME:** All data, jobs, and configurations are stored in `aisland/data/jenkins`. This directory is ignored by Git to keep your local environment clean.
- **Pipelines:** Each project (e.g., `logflow/`) contains its own `Jenkinsfile`. When creating a new job in Jenkins, point it to the local repository path and select "Pipeline from SCM" with the script path set to `logflow/Jenkinsfile`.

### 6. Check Quality Gates
You can run automated quality checks across projects using the `check` command. This includes formatting (`isort`, `black`), linting (`flake8`), type checking (`mypy`), unit tests (`pytest`), and GitHub Actions validation (`act`).

```bash
# Run all checks for all projects
aisland check

# Run checks for a specific project
aisland check confluid

# Run a specific tool for all projects
aisland check pytest

# Run a specific tool for a specific project
aisland check pytest confluid
```

## GitHub Actions Local Testing

The `act` tool is installed to allow you to run and test GitHub Actions locally. 

```bash
# List all workflows and jobs in the current project
act --list

# Run all workflows for the current push event
act

# Run a specific job
act -j unit-tests
```
*Note: Running `act` requires Docker to be installed and running on your machine.*

## Submodule Management

This repository uses Git submodules to manage its constituent projects.

## Jupyter Notebook Management

To keep the repository size small and diffs clean, all Jupyter notebooks (`*.ipynb`) are automatically processed by a Git filter that clears their code cell outputs before staging.

### Marking as Documentation
If you have a notebook that serves as documentation and you **want** to preserve its outputs (e.g., visualizations, benchmark results), you must mark it as such in its metadata:

1. Open the notebook in VS Code or Jupyter.
2. Edit the notebook metadata (in VS Code: `Notebook Information` -> `Open Metadata`).
3. Add `"is_documentation": true` to the `metadata` object:
```json
{
  "metadata": {
    "is_documentation": true,
    ...
  },
  ...
}
```

### Manual Cleaning
You can also manually clean a notebook using the CLI:
```bash
cat my_notebook.ipynb | aisland notebooks clean > cleaned_notebook.ipynb
```

## Working with Submodules

To execute commands across all submodules (e.g., installing all projects in editable mode), use `git submodule foreach`:

```bash
# Install all projects in editable mode
git submodule foreach 'pip install -e .'

# Run tests in all submodules
git submodule foreach 'pytest'

# Check the status of all submodules
git submodule foreach 'git status'
```

### Initializing Submodules

If you've just cloned the repository or pulled new changes, ensure all submodules are up to date:

```bash
git submodule update --init --recursive
```

### Recommended Workspace Configuration

To ensure that your local submodules automatically stay in sync with the **parent repository's recorded hashes**, enable recursive submodule operations:

```bash
git config submodule.recurse true
```

*With this on, if a team member commits a new submodule hash to the parent repository, your next `git pull` will automatically download their updated submodule code.*

### Pulling Unrecorded Submodule Changes (`pull-all`)

If `submodule.recurse` keeps things in sync, why do you need `pull-all`? 

Because a standard `git pull` only syncs submodules to the exact snapshots recorded in the **parent repository**. If a teammate pushes a brand new commit directly to the `dataflux` submodule's remote branch, but *nobody* has updated the parent repository to point to it yet, a standard `git pull` will completely ignore that new `dataflux` commit.

The `pull-all` alias is for **eagerly fetching unrecorded changes**. It reaches out directly to all submodule remote repositories, bypasses the parent's locked hashes, and forces your submodules to fast-forward to the absolute latest commits on their tracked branches.

To configure the global `pull-all` Git alias:

```bash
git config --global alias.pull-all '!PARENT_BRANCH=$(git branch --show-current) && git pull origin $PARENT_BRANCH && git submodule foreach "git pull origin \$PARENT_BRANCH"'
```

To use it (e.g., to grab everything latest before starting a new feature):

```bash
git pull-all
```

### Pushing All Submodule Changes (`push-all`)

When you have committed changes across multiple submodules and the parent repository, you can use `push-all` to push the current branch of every project to its respective remote in one command.

To configure the global `push-all` Git alias:

```bash
git config --global alias.push-all '!git push origin $(git branch --show-current) && git submodule foreach "git push origin \$(git branch --show-current)"'
```

To use it:

```bash
git push-all
```

### Switching All Submodules (`switch-all`)

When moving between development streams (e.g., switching from a feature branch back to `dev/main`), you often need to update the branch context for both the parent repository and all its constituent submodules simultaneously.

To configure the global `switch-all` Git alias:

```bash
git config --global alias.switch-all '!f() { git checkout "$1" && git submodule foreach "branch=\$(git config -f \"\$toplevel/.gitmodules\" submodule.\$name.branch); [ \"\$branch\" = \".\" ] && branch=\"$1\"; git checkout \${branch:-main}"; }; f'
```

To use it:

```bash
git switch-all dev/main
```

### Merging All Submodules (`merge-all`)

When you need to merge a base branch (e.g., `main` or `dev/main`) into your current feature branch across all projects in the workspace, you can use `merge-all`. This ensures your entire dependency tree stays synchronized with upstream changes.

To configure the global `merge-all` Git alias:

```bash
git config --global alias.merge-all '!f() { git merge "$1" && git submodule foreach "git merge $1"; }; f'
```

To use it (e.g., to merge `dev/main` into your current branch everywhere):

```bash
git merge-all dev/main
```

### Tagging All Submodules (`tag-all`)

When you want to mark a workspace-wide snapshot (a release, or a proof-of-concept milestone), you need the *same* tag on the root repository and every active submodule, then pushed to each remote. `tag-all` is the cross-repo analogue of a single `git tag`: it creates one **annotated** tag at the current `HEAD` of the parent and each active submodule, then pushes it. Tags are created everywhere **first** (cheap, local) before any push, so an unreachable remote (e.g. an off-VPN private host) leaves every local tag intact — you simply re-run to push the rest. The tag message defaults to the tag name if omitted.

To configure the global `tag-all` Git alias:

```bash
git config --global alias.tag-all '!f() {
    if [ -z "$1" ]; then echo "Usage: git tag-all <tag> [message]"; exit 1; fi;
    msg="${2:-$1}";
    git tag -a "$1" -m "$msg" && git submodule foreach --quiet "git tag -a \"$1\" -m \"$msg\"";
    git push origin "$1" && git submodule foreach --quiet "git push origin \"$1\"";
}; f'
```

To use it (creates the tag locally **and** pushes it to every remote in one command):

```bash
git tag-all 'v1.0.0' 'First stable release'
```

> Tags are non-forcing: if `<tag>` already exists, the command aborts before pushing. Delete it first (`git tag -d <tag>` / `git submodule foreach 'git tag -d <tag>'`) to re-tag.

### Checking Status of All Submodules (`status-all`)

To quickly see the status and current branch of the root repository and all its constituent submodules, you can use the `status-all` alias.

To configure the global `status-all` Git alias:

```bash
git config --global alias.status-all '!f() { 
    printf "%-25s (@%s" "ROOT" "$(git branch --show-current)";
    git rev-list --left-right --count HEAD...@{u} 2>/dev/null | while read a b; do
        if [ -n "$a" ] && ([ "$a" -gt 0 ] || [ "$b" -gt 0 ]); then
            printf " ("; [ "$a" -gt 0 ] && printf "\033[32m%s ahead\033[0m" "$a";
            [ "$a" -gt 0 ] && [ "$b" -gt 0 ] && printf ", ";
            [ "$b" -gt 0 ] && printf "\033[31m%s behind\033[0m" "$b"; printf ")";
        fi;
    done;
    printf ")\n"; git status --short;
    git submodule foreach -q '\''
        printf "\n%-25s (@%s" "$name" "$(git branch --show-current)";
        git rev-list --left-right --count HEAD...@{u} 2>/dev/null | while read a b; do
            if [ -n "$a" ] && ([ "$a" -gt 0 ] || [ "$b" -gt 0 ]); then
                printf " ("; [ "$a" -gt 0 ] && printf "\033[32m%s ahead\033[0m" "$a";
                [ "$a" -gt 0 ] && [ "$b" -gt 0 ] && printf ", ";
                [ "$b" -gt 0 ] && printf "\033[31m%s behind\033[0m" "$b"; printf ")";
            fi;
        done;
        printf ")\n"; git status --short
    '\''; 
}; f'
```

To use it:

```bash
git status-all
```

### Safely Pushing Changes (`safe`)

A custom Git alias exists to enforce the workspace mandates before pushing code. It automatically sets up the environment, formats the code with `isort` and `black`, lints with `flake8`, types checks with `mypy`, and runs `pytest`. If everything passes, it commits and pushes safely to the remote.

To configure the global `safe` Git alias:

```bash
git config --global alias.safe '!f() { if [ -z "$1" ]; then echo "Error: Please provide a commit message. Example: git safe \"feat: my message\""; exit 1; fi; if [ -d ".venv" ]; then echo "Activating virtual environment..."; source .venv/bin/activate; fi; echo "Running formatting (isort, black)..."; isort . && black . || { echo "Formatting failed!"; exit 1; }; echo "Running linting (flake8)..."; flake8 . || { echo "Flake8 linting failed!"; exit 1; }; echo "Running type checking (mypy)..."; mypy . || { echo "Mypy type checking failed!"; exit 1; }; echo "Running tests (pytest)..."; pytest || { echo "Tests failed!"; exit 1; }; echo "All checks passed! Committing and pushing..."; git add . && git commit -m "$1"; CURRENT_BRANCH=$(git branch --show-current); if [ -z "$CURRENT_BRANCH" ]; then git push origin HEAD:dev/main || git push origin HEAD:main; else git push origin "$CURRENT_BRANCH" || { echo "Falling back to dev/main push..."; git push origin HEAD:dev/main; }; fi; }; f'
```

To use the shortcut:

```bash
git safe "feat(logflow): fixed the configuration parser bug"
```
