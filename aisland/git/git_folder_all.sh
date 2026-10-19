#!/bin/bash
# The workspace git aliases `status-all`, `switch-all`, `merge-all`, `tag-all` and `safe` for a
# workspace without .gitmodules — where each subfolder that is its own git repository (a clone) takes
# a submodule's place. A workspace WITH .gitmodules keeps the `git submodule foreach` aliases
# setup_git.sh writes for it; `pull-all` and `push-all` have scripts of their own
# (git_pull_all.sh, git_push_all.sh).
#
# Installed by setup_git.sh as `!GIT_PULL_ALL_ROOT="<root>" bash "<this file>" <verb>`: from inside a
# clone git cannot find the workspace root. Written for macOS's /bin/bash 3.2. Pinned by
# aisland/tests/test_folder_git_aliases.py.
#
#   git status-all                  each repository's branch, ahead/behind and changes; the root first
#   git switch-all <branch>         check out <branch> in every repository
#   git merge-all <ref>             merge <ref> in every repository
#   git tag-all <tag> [message]     tag every repository's HEAD, then push each tag
#   git safe "<message>"            this clone's gates, then commit and push it

# Typed inside a clone, the alias inherits that repository's GIT_DIR and friends; every command
# below names its repository with -C, so drop them. `safe` still knows its clone: git runs a `!`
# alias from the repository's top level, which is the folder this starts in.
CLONE_DIR="$(pwd)"
unset $(git rev-parse --local-env-vars)

WORKSPACE_ROOT="$(cd "${GIT_PULL_ALL_ROOT:?set by the workspace git aliases}" && pwd)"
VERB="${1:-}"
[ $# -gt 0 ] && shift

report() {
    printf '%-22s %s\n' "$1" "$2"
}

first_line() {
    grep -v '^hint:' | head -n 1
}

# The repositories, as "<label>\t<dir>" lines: the clones in folder order, the root (when it is a
# repository) first or last as $1 says.
repositories() {
    local dir
    if [ "$1" = first ] && [ -d "$WORKSPACE_ROOT/.git" ]; then printf 'root\t%s\n' "$WORKSPACE_ROOT"; fi
    for dir in "$WORKSPACE_ROOT"/*/; do
        dir="${dir%/}"
        if [ -e "$dir/.git" ]; then printf '%s\t%s\n' "$(basename "$dir")" "$dir"; fi
    done
    if [ "$1" = last ] && [ -d "$WORKSPACE_ROOT/.git" ]; then printf 'root\t%s\n' "$WORKSPACE_ROOT"; fi
}

usage() {
    echo "usage: git $1" >&2
    exit 2
}

status_all() {
    local label dir branch counts ahead behind extra
    while IFS="$(printf '\t')" read -r label dir; do
        branch=$(git -C "$dir" branch --show-current)
        extra=""
        if counts=$(git -C "$dir" rev-list --left-right --count 'HEAD...@{u}' 2>/dev/null); then
            ahead=${counts%%[[:space:]]*}
            behind=${counts##*[[:space:]]}
            [ "$ahead" -gt 0 ] && extra="$extra, $ahead ahead"
            [ "$behind" -gt 0 ] && extra="$extra, $behind behind"
        fi
        report "$label" "(@${branch:-detached}$extra)"
        git -C "$dir" status --short
    done < <(repositories first)
}

# Run "git <args>" in every repository, the root last; $1 = what was done, $2 = what git's "Already up
# to date" means, $3 = the failure word.
each_repository() {
    local done_word="$1" same_word="$2" fail_word="$3" label dir out failed=0
    shift 3
    while IFS="$(printf '\t')" read -r label dir; do
        if out=$(git -C "$dir" "$@" 2>&1); then
            if printf '%s\n' "$out" | grep -q "Already up to date"; then
                report "$label" "$same_word"
            else
                report "$label" "$done_word"
            fi
        else
            report "$label" "$fail_word: $(printf '%s\n' "$out" | first_line)"
            failed=1
        fi
    done < <(repositories last)
    return "$failed"
}

tag_all() {
    local tag="$1" message="${2:-$1}" label dir out failed=0
    # Tags are made everywhere before any push, so an unreachable remote leaves every local tag in place
    # and a re-run only has the pushes left.
    while IFS="$(printf '\t')" read -r label dir; do
        if out=$(git -C "$dir" tag -a "$tag" -m "$message" 2>&1); then
            report "$label" "tagged $tag"
        else
            report "$label" "not tagged: $(printf '%s\n' "$out" | first_line)"
            failed=1
        fi
    done < <(repositories last)
    while IFS="$(printf '\t')" read -r label dir; do
        git -C "$dir" rev-parse -q --verify "refs/tags/$tag" >/dev/null || continue
        if out=$(git -C "$dir" push -q origin "$tag" 2>&1); then
            report "$label" "pushed tag $tag"
        else
            report "$label" "not pushed: $(printf '%s\n' "$out" | first_line)"
            failed=1
        fi
    done < <(repositories last)
    return "$failed"
}

# The gates of the clone `safe` was typed in, with the workspace's venv, mypy from the workspace root
# against the clone's folder (as in a superproject); then commit and push the clone.
safe() {
    local message="$1" project branch
    project=$(basename "$CLONE_DIR")
    cd "$CLONE_DIR" || exit 1
    if [ -f "$WORKSPACE_ROOT/.venv/bin/activate" ]; then
        echo "Activating virtual environment..."
        . "$WORKSPACE_ROOT/.venv/bin/activate"
    fi
    echo "Running formatting (isort, black)..."
    { isort . && black .; } || { echo "Formatting failed!"; exit 1; }
    echo "Running linting (flake8)..."
    flake8 . || { echo "Flake8 linting failed!"; exit 1; }
    echo "Running type checking (mypy from workspace root)..."
    (cd "$WORKSPACE_ROOT" && mypy "$project") || { echo "Mypy type checking failed!"; exit 1; }
    echo "Running tests (pytest)..."
    pytest || { echo "Tests failed!"; exit 1; }
    echo "All checks passed! Committing and pushing..."
    git -C "$CLONE_DIR" add . && git -C "$CLONE_DIR" commit -q -m "$message" || exit 1
    branch=$(git -C "$CLONE_DIR" branch --show-current)
    if [ -z "$branch" ]; then
        echo "detached HEAD — committed, not pushed"
        exit 1
    fi
    git -C "$CLONE_DIR" push -q origin "$branch"
}

case "$VERB" in
    status) status_all ;;
    switch)
        [ -n "${1:-}" ] || usage "switch-all <branch>"
        each_repository "switched to $1" "switched to $1" "not switched" checkout -q "$1"
        ;;
    merge)
        [ -n "${1:-}" ] || usage "merge-all <ref>"
        each_repository "merged $1" "up to date with $1" "not merged" merge --no-edit "$1"
        ;;
    tag)
        [ -n "${1:-}" ] || usage "tag-all <tag> [message]"
        tag_all "$1" "${2:-}"
        ;;
    safe)
        [ -n "${1:-}" ] || usage 'safe "<commit message>"'
        safe "$1"
        ;;
    *) usage "status-all | switch-all <branch> | merge-all <ref> | tag-all <tag> [message] | safe <message>" ;;
esac
