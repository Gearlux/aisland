#!/bin/bash
# git push-all for a workspace without .gitmodules — every subfolder that is its own git repository
# (a clone) pushes its current branch to origin, then the root, when it is a repository and every
# clone pushed (so origin's root never names work its clones' origins lack, as with submodules).
#
# Installed as the `git push-all` alias by setup_git.sh, which passes the workspace root in
# GIT_PULL_ALL_ROOT: from inside a clone git cannot find it. A workspace WITH .gitmodules keeps the
# `git submodule foreach` alias instead. Written for macOS's /bin/bash 3.2. Pinned by
# aisland/tests/test_folder_git_aliases.py.
#
#   git push-all

# Typed inside a clone, the alias inherits that repository's GIT_DIR and friends; every command
# below names its repository with -C, so drop them.
unset $(git rev-parse --local-env-vars)

WORKSPACE_ROOT="$(cd "${GIT_PULL_ALL_ROOT:?set by the git push-all alias}" && pwd)"

report() {
    printf '%-22s %s\n' "$1" "$2"
}

commits() {
    if [ "$1" = 1 ]; then echo "1 commit"; else echo "$1 commits"; fi
}

# Push $1's current branch (label $2); returns 1 when it did not push.
push_repo() {
    local repo="$1" label="$2" branch ahead out
    branch=$(git -C "$repo" branch --show-current)
    if [ -z "$branch" ]; then
        report "$label" "detached HEAD — not pushed"
        return 1
    fi
    if git -C "$repo" rev-parse -q --verify "refs/remotes/origin/$branch" >/dev/null; then
        ahead=$(git -C "$repo" rev-list --count "origin/$branch..HEAD")
    else
        ahead=$(git -C "$repo" rev-list --count HEAD)
    fi
    if [ "$ahead" = 0 ]; then
        report "$label" "$branch up to date"
        return 0
    fi
    if out=$(git -C "$repo" push -q origin "$branch" 2>&1); then
        report "$label" "pushed $branch ($(commits "$ahead"))"
        return 0
    fi
    report "$label" "not pushed: $(printf '%s\n' "$out" | grep -v '^hint:' | head -n 1)"
    return 1
}

FAILED=0
FOUND=0
for dir in "$WORKSPACE_ROOT"/*/; do
    dir="${dir%/}"
    [ -e "$dir/.git" ] || continue
    FOUND=1
    push_repo "$dir" "$(basename "$dir")" || FAILED=1
done

if [ -d "$WORKSPACE_ROOT/.git" ]; then
    FOUND=1
    if [ "$FAILED" = 1 ]; then
        report root "not pushed: a repository above did not push"
    else
        push_repo "$WORKSPACE_ROOT" root || FAILED=1
    fi
fi

if [ "$FOUND" = 0 ]; then
    echo "no git repository in $WORKSPACE_ROOT — nothing pushed"
    exit 1
fi
exit "$FAILED"
