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

git config --global alias.push-all '!git push origin $(git branch --show-current) && git submodule foreach "git push origin \$(git branch --show-current)"'

git config --global alias.switch-all '!f() { git checkout "$1" && git submodule foreach "branch=\$(git config -f \"\$toplevel/.gitmodules\" submodule.\$name.branch); [ \"\$branch\" = \".\" ] && branch=\"$1\"; git checkout \${branch:-main}"; }; f'

git config --global alias.merge-all '!f() { git merge "$1" && git submodule foreach "git merge $1"; }; f'

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

git config --global alias.safe '!f() { if [ -z "$1" ]; then echo "Error: Please provide a commit message. Example: git safe \"feat: my message\""; exit 1; fi; if [ -d ".venv" ]; then echo "Activating virtual environment..."; source .venv/bin/activate; fi; echo "Running formatting (isort, black)..."; isort . && black . || { echo "Formatting failed!"; exit 1; }; echo "Running linting (flake8)..."; flake8 . || { echo "Flake8 linting failed!"; exit 1; }; echo "Running type checking (mypy)..."; mypy . || { echo "Mypy type checking failed!"; exit 1; }; echo "Running tests (pytest)..."; pytest || { echo "Tests failed!"; exit 1; }; echo "All checks passed! Committing and pushing..."; git add . && git commit -m "$1"; CURRENT_BRANCH=$(git branch --show-current); if [ -z "$CURRENT_BRANCH" ]; then git push origin HEAD:dev/main || git push origin HEAD:main; else git push origin "$CURRENT_BRANCH" || { echo "Falling back to dev/main push..."; git push origin HEAD:dev/main; }; fi; }; f'

echo "==> Configuring Notebook Clean Filter..."
# Only configure locally if inside a git repository
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # We use a local config so it only applies here
    git config filter.ipynb-cleaner.clean "aisland notebooks clean"
    git config filter.ipynb-cleaner.smudge cat
    
    # Create .gitattributes in root if it doesn't exist
    if [ ! -f ".gitattributes" ]; then
        echo "*.ipynb filter=ipynb-cleaner" > .gitattributes
    elif ! grep -q "filter=ipynb-cleaner" .gitattributes; then
        echo "*.ipynb filter=ipynb-cleaner" >> .gitattributes
    fi
fi

echo "==> Git configuration complete!"
