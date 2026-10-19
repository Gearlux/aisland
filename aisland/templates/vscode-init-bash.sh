# =========================================================================
# Written by: aisland workspace init (template: aisland/aisland/templates/vscode-init-bash.sh)
# A starting point that is yours to edit: init writes it once, into a new workspace, never over a file.
# =========================================================================
#
# Sourced by VS Code's integrated terminal for this workspace, wired in through the
# terminal profile of the workspace file `aisland setup` writes (<workspace>.code-workspace).
#
# VS Code's Python extensions don't activate the workspace .venv on every bash, and neither
# sources project.bashrc's aliases/completions. This shim is the deterministic replacement:
#   bash --rcfile vscode-init-bash.sh  -->  ~/.bash_profile  -->  project.bashrc

# 1. Normal interactive bash startup (PATH, ssh-agent, brew shellenv). Prefer ~/.bash_profile
#    (macOS convention); fall back to ~/.bashrc where .bash_profile isn't used.
if [ -f "$HOME/.bash_profile" ]; then
    source "$HOME/.bash_profile"
elif [ -f "$HOME/.bashrc" ]; then
    source "$HOME/.bashrc"
fi

# 2. Prompt with cwd, set before step 3 so the venv's `(.venv) ` prefix lands in front of it.
PS1='\[\e[36m\]\w\[\e[0m\] \$ '

# 3. Workspace activation: the root is this file's folder, so nothing here names a user or a path.
WORKSPACE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
if [ -f "$WORKSPACE_ROOT/project.bashrc" ]; then
    cd "$WORKSPACE_ROOT" || true
    source "$WORKSPACE_ROOT/project.bashrc"
fi
