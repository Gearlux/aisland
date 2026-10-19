#!/bin/bash
# Initialize Git aliases and configuration for the workspace
set -e

# Usage: setup_git.sh [workspace-root]
#
# The root is the argument (what `aisland` passes, wherever this file is installed); without one, this
# file is aisland/aisland/git/setup_git.sh inside the workspace, three levels below the root.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${1:-}" ]; then
    WORKSPACE_ROOT="$(cd "$1" && pwd)"
else
    WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
fi
cd "$WORKSPACE_ROOT"

# A workspace without .gitmodules whose subfolders are git repositories of their own (clones): each
# clone, and the root when it is a repository, gets `pull-all` and `push-all` naming the workspace
# root (from inside a clone git cannot find it), each repository on its own branch. The aliases that
# fan out over submodules (switch-all, merge-all, tag-all, status-all, safe) are not installed:
# here they would act on one clone and say nothing of the others. No .gitmodules and no clone:
# a root repository goes on below as before; a folder that is no repository gets no aliases.
if [ ! -f "$WORKSPACE_ROOT/.gitmodules" ]; then
    CLONES=()
    for dir in "$WORKSPACE_ROOT"/*/; do
        dir="${dir%/}"
        if [ -e "$dir/.git" ]; then CLONES+=("$(basename "$dir")"); fi
    done
    if [ ${#CLONES[@]} -eq 0 ] && [ ! -d "$WORKSPACE_ROOT/.git" ]; then
        echo "==> No git repository in $WORKSPACE_ROOT: no git aliases installed."
        exit 0
    fi
    if [ ${#CLONES[@]} -gt 0 ]; then
        echo "==> Configuring Git Aliases (no .gitmodules: the clones $(printf '%s\n' "${CLONES[@]}" | paste -sd, - | sed 's/,/, /g'))..."
        REPOS=("${CLONES[@]}")
        if [ -d "$WORKSPACE_ROOT/.git" ]; then REPOS+=("."); fi
        for repo in "${REPOS[@]}"; do
            git -C "$WORKSPACE_ROOT/$repo" config --local alias.pull-all \
                "!GIT_PULL_ALL_ROOT=\"$WORKSPACE_ROOT\" bash \"$SCRIPT_DIR/git_pull_all.sh\""
            git -C "$WORKSPACE_ROOT/$repo" config --local alias.push-all \
                "!GIT_PULL_ALL_ROOT=\"$WORKSPACE_ROOT\" bash \"$SCRIPT_DIR/git_push_all.sh\""
        done
        if [ -d "$WORKSPACE_ROOT/.git" ]; then
            # The root's notebook filter, as for a superproject; the clones' .gitattributes are theirs.
            if [ -x "$WORKSPACE_ROOT/.venv/bin/aisland" ]; then
                AISLAND_CLEAN_CMD="$WORKSPACE_ROOT/.venv/bin/aisland notebooks clean"
            else
                AISLAND_CLEAN_CMD="aisland notebooks clean"
            fi
            git config filter.ipynb-cleaner.clean "$AISLAND_CLEAN_CMD"
            git config filter.ipynb-cleaner.smudge cat
            if [ ! -f ".gitattributes" ]; then
                echo "*.ipynb filter=ipynb-cleaner" > .gitattributes
            elif ! grep -q "filter=ipynb-cleaner" .gitattributes; then
                echo "*.ipynb filter=ipynb-cleaner" >> .gitattributes
            fi
        fi
        echo "==> Git configuration complete!"
        exit 0
    fi
fi

echo "==> Configuring Git Submodule Tracking..."
git config submodule.recurse true

# Submodules declared `active = false` in .gitmodules are externally
# inaccessible to most contributors (e.g. private Bitbucket hosts).
# `git submodule init` skips populating their URL in .git/config, but a
# contributor whose local config has `submodule.active = .` (a common
# ergonomic default that recurses into all submodules) will still see
# `git pull --recurse-submodules` try to fetch+clone them. Set a per-name
# `active = false` in local .git/config — it takes precedence over the
# multi-pattern `submodule.active` setting and makes the skip stick.
INACTIVE_SUBMODULES=()
if [ -f "$WORKSPACE_ROOT/.gitmodules" ]; then
    while read -r key value; do
        if [ "$value" = "false" ]; then
            name="${key#submodule.}"
            name="${name%.active}"
            # Respect a pre-existing local `true` — that's a user opt-in
            # (e.g. a contributor with access to a private submodule's host
            # enabling it). Without this guard, every setup.sh run
            # would silently flip the user's choice back to inactive.
            current=$(git config --local --get "submodule.$name.active" 2>/dev/null || true)
            if [ "$current" = "true" ]; then
                echo "    - keeping submodule '$name' locally active (user opt-in)"
                continue
            fi
            echo "    - marking submodule '$name' as locally inactive"
            git config --local "submodule.$name.active" false
            INACTIVE_SUBMODULES+=("$name")
        fi
    done < <(git config -f "$WORKSPACE_ROOT/.gitmodules" --get-regexp '^submodule\..*\.active$' 2>/dev/null)
fi

echo "==> Configuring Git Aliases..."
# The aliases below assume the acme mono-repo layout and Python toolchain
# (submodule fan-out, .venv, isort/black/flake8/mypy/pytest), so they are
# written to LOCAL config only — a global alias would leak workspace-specific
# behavior into every unrelated repo on this machine. Because each submodule
# has its own .git config (and e.g. `git safe` is invoked from INSIDE a
# project submodule), the aliases are registered on the root repo AND every
# active submodule — the same propagation the ipynb-cleaner filter uses.
# Re-run this script after activating a new submodule.

# Active submodule worktree paths, resolved once. Registration uses
# `git -C <path> config` instead of `git submodule foreach "git config ..."`
# so the alias bodies don't need a second layer of shell quoting.
ACTIVE_SUBMODULE_PATHS=()
while IFS= read -r sm_path; do
    ACTIVE_SUBMODULE_PATHS+=("$sm_path")
done < <(git submodule --quiet foreach 'echo "$sm_path"' 2>/dev/null)

set_alias() {
    local name="$1" value="$2"
    git config --local "alias.$name" "$value"
    local sm
    for sm in "${ACTIVE_SUBMODULE_PATHS[@]}"; do
        git -C "$sm" config --local "alias.$name" "$value"
    done
    # Migration: earlier versions of this script wrote these aliases to the
    # GLOBAL config. The names are this workspace's invention, so any global
    # copy is ours — drop it now that local definitions cover every repo the
    # aliases are meant for (locals also shadow globals, so a leftover would
    # only ever fire in UNrelated repos, which is exactly the pollution).
    if git config --global --get "alias.$name" >/dev/null 2>&1; then
        git config --global --unset-all "alias.$name"
        echo "    - removed stale GLOBAL alias.$name (now configured per-repo)"
    fi
}

# pull-all: fetch + merge every submodule, then the root, settling submodule-pointer
# conflicts and recording what came in — see git_pull_all.sh. In a checkout the script lives in the
# workspace root's aisland/aisland/git/, found from the root or from inside any submodule. Installed
# elsewhere (aisland from a wheel), the alias names the script by its path and hands it the root.
# Usage: git pull-all [--autostash]
if [ "$SCRIPT_DIR" = "$WORKSPACE_ROOT/aisland/aisland/git" ]; then
    set_alias pull-all '!bash "$(git rev-parse --show-superproject-working-tree --show-toplevel | head -n 1)/aisland/aisland/git/git_pull_all.sh"'
else
    set_alias pull-all "!GIT_PULL_ALL_ROOT=\"\$(git rev-parse --show-superproject-working-tree --show-toplevel | head -n 1)\" bash \"$SCRIPT_DIR/git_pull_all.sh\""
fi

# push-all: the submodules first, the root last and only when they all pushed, so the root
# on origin never records a submodule commit origin does not have.
set_alias push-all '!git submodule foreach "git push origin \$(git branch --show-current)" && git push origin $(git branch --show-current)'

set_alias switch-all '!f() { git checkout "$1" && git submodule foreach "branch=\$(git config -f \"\$toplevel/.gitmodules\" submodule.\$name.branch); [ \"\$branch\" = \".\" ] && branch=\"$1\"; git checkout \${branch:-main}"; }; f'

set_alias merge-all '!f() { git merge "$1" && git submodule foreach "git merge $1"; }; f'

# tag-all: stamp the SAME annotated tag on the parent + every active submodule at
# their current HEAD, then push it to each remote — the cross-repo analogue of a
# single `git tag`. Tags are created EVERYWHERE first (cheap, local) before any
# push, so an unreachable remote (e.g. an off-VPN private host) leaves every local
# tag intact and you just re-run the push. Message defaults to the tag name.
# Usage: git tag-all <tag> [message]
set_alias tag-all '!f() {
    if [ -z "$1" ]; then echo "Usage: git tag-all <tag> [message]"; exit 1; fi;
    msg="${2:-$1}";
    git tag -a "$1" -m "$msg" && git submodule foreach --quiet "git tag -a \"$1\" -m \"$msg\"";
    git push origin "$1" && git submodule foreach --quiet "git push origin \"$1\"";
}; f'

set_alias status-all '!f() {
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

# safe: quality-gate + commit + push. Runs from the repo toplevel (git `!`
# alias semantics). The workspace root is resolved via the superproject (the
# alias usually runs inside a project submodule): the shared .venv lives at
# the root, and mypy MUST run from the root so the root mypy.ini's mypy_path
# is honored (a project-local `mypy .` yields spurious import-not-found).
set_alias safe '!f() {
    if [ -z "$1" ]; then echo "Error: Please provide a commit message. Example: git safe \"feat: my message\""; exit 1; fi;
    WS_ROOT=$(git rev-parse --show-superproject-working-tree);
    PROJECT=$(basename "$(git rev-parse --show-toplevel)");
    if [ -z "$WS_ROOT" ]; then WS_ROOT=$(git rev-parse --show-toplevel); PROJECT=.; fi;
    if [ -d "$WS_ROOT/.venv" ]; then echo "Activating virtual environment..."; . "$WS_ROOT/.venv/bin/activate"; fi;
    echo "Running formatting (isort, black)..."; isort . && black . || { echo "Formatting failed!"; exit 1; };
    echo "Running linting (flake8)..."; flake8 . || { echo "Flake8 linting failed!"; exit 1; };
    echo "Running type checking (mypy from workspace root)..."; (cd "$WS_ROOT" && mypy "$PROJECT") || { echo "Mypy type checking failed!"; exit 1; };
    echo "Running tests (pytest)..."; pytest || { echo "Tests failed!"; exit 1; };
    echo "All checks passed! Committing and pushing...";
    git add . && git commit -m "$1";
    CURRENT_BRANCH=$(git branch --show-current);
    if [ -z "$CURRENT_BRANCH" ]; then git push origin HEAD:dev/main || git push origin HEAD:main;
    else git push origin "$CURRENT_BRANCH" || { echo "Falling back to dev/main push..."; git push origin HEAD:dev/main; }; fi;
}; f'

echo "==> Configuring Notebook Clean Filter..."
# Only configure locally if inside a git repository
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # The filter has to invoke aisland by absolute path: git runs filters in a
    # minimal env where the workspace .venv is not activated and `aisland`
    # is not on PATH. Falling back to bare `aisland` means every `git status`
    # / `git add` from a fresh shell spews `aisland: command not found`.
    if [ -x "$WORKSPACE_ROOT/.venv/bin/aisland" ]; then
        AISLAND_CLEAN_CMD="$WORKSPACE_ROOT/.venv/bin/aisland notebooks clean"
    else
        AISLAND_CLEAN_CMD="aisland notebooks clean"
    fi

    # We use a local config so it only applies here
    git config filter.ipynb-cleaner.clean "$AISLAND_CLEAN_CMD"
    git config filter.ipynb-cleaner.smudge cat

    # Create .gitattributes in root if it doesn't exist
    if [ ! -f ".gitattributes" ]; then
        echo "*.ipynb filter=ipynb-cleaner" > .gitattributes
    elif ! grep -q "filter=ipynb-cleaner" .gitattributes; then
        echo "*.ipynb filter=ipynb-cleaner" >> .gitattributes
    fi

    # Submodules have their own .git/config — the root registration above
    # does not propagate, so commits inside a submodule would write notebook
    # outputs as-is. Register the filter in every active submodule too.
    # `git submodule foreach` skips submodules marked `active = false`,
    # which is what we want (e.g. a private submodule on a host without access).
    git submodule foreach --quiet "
        git config filter.ipynb-cleaner.clean '$AISLAND_CLEAN_CMD'
        git config filter.ipynb-cleaner.smudge cat
    "
fi

echo "==> Git configuration complete!"

if [ ${#INACTIVE_SUBMODULES[@]} -gt 0 ]; then
    echo ""
    echo "Note: the following submodules are inactive on this machine and will be"
    echo "skipped by 'git pull --recurse-submodules' and 'git submodule update'."
    echo "If you have access and want to enable one, copy/paste the matching line:"
    echo ""
    for name in "${INACTIVE_SUBMODULES[@]}"; do
        url=$(git config -f "$WORKSPACE_ROOT/.gitmodules" --get "submodule.$name.url" 2>/dev/null || echo "")
        echo "    # $name  ($url)"
        echo "    git config --local submodule.$name.active true && git submodule update --init --recursive $name"
        echo ""
    done
    echo "To opt back out later:"
    echo "    git config --local submodule.<name>.active false"
fi
