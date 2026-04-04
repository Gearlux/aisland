#!/bin/bash
# Initialize Git aliases and configuration for the workspace
set -e

# Resolve the project root
WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKSPACE_ROOT"

echo "==> Configuring Git Submodule Tracking..."
git config submodule.recurse true

echo "==> Configuring Git Aliases..."
git config --global alias.pull-all '!PARENT_BRANCH=$(git branch --show-current) && git pull origin $PARENT_BRANCH && git submodule foreach "git pull origin \$PARENT_BRANCH"'

git config --global alias.switch-all '!f() { git checkout "$1" && git submodule foreach "branch=\$(git config -f \"\$toplevel/.gitmodules\" submodule.\$name.branch); [ \"\$branch\" = \".\" ] && branch=\"$1\"; git checkout \${branch:-main}"; }; f'

git config --global alias.safe '!f() { if [ -z "$1" ]; then echo "Error: Please provide a commit message. Example: git safe \"feat: my message\""; exit 1; fi; if [ -d ".venv" ]; then echo "Activating virtual environment..."; source .venv/bin/activate; fi; echo "Running formatting (isort, black)..."; isort . && black . || { echo "Formatting failed!"; exit 1; }; echo "Running linting (flake8)..."; flake8 . || { echo "Flake8 linting failed!"; exit 1; }; echo "Running type checking (mypy)..."; mypy . || { echo "Mypy type checking failed!"; exit 1; }; echo "Running tests (pytest)..."; pytest || { echo "Tests failed!"; exit 1; }; echo "All checks passed! Committing and pushing..."; git add . && git commit -m "$1"; CURRENT_BRANCH=$(git branch --show-current); if [ -z "$CURRENT_BRANCH" ]; then git push origin HEAD:dev/main || git push origin HEAD:main; else git push origin "$CURRENT_BRANCH" || { echo "Falling back to dev/main push..."; git push origin HEAD:dev/main; }; fi; }; f'

echo "==> Git configuration complete!"
