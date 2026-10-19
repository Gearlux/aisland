#!/bin/bash
# git pull-all — pull the whole workspace: every active submodule, then the root.
#
# Installed as the `git pull-all` alias by setup_git.sh, which runs this file from the
# workspace root wherever the alias is typed (the root or any submodule).
#
#   git pull-all [--autostash]
#
# Why a script and not a one-line alias: the root records one commit per submodule (its
# "pointer"). When two machines both commit in a submodule and record it, the root's merge
# stops on `CONFLICT (submodule)` even after the submodule itself merged cleanly — git names
# the submodule's merge commit as "a possible merge resolution" but does not take it. This
# script takes it, under one rule: a pointer conflict is settled to the submodule's
# checked-out commit only when that commit contains BOTH sides.
#
# Steps:
#   1. fetch the root and every submodule from origin, in parallel, before anything merges;
#   2. merge origin/<branch> into each submodule that is on the root's branch;
#   3. merge origin/<branch> into the root, settling pointer conflicts by the rule above —
#      skipped while a submodule is left mid-merge, since its pointer depends on it;
#   4. record, in one root commit, each submodule holding commits from origin that the root
#      does not record yet. A pointer never moves backwards, and a submodule whose only
#      unrecorded commits are local ones is left for whoever made them.
# Whatever it cannot do is listed under "needs you:" with exit status 1; a rerun after
# fixing it continues where this run stopped (it also finishes a root merge left on pointer
# conflicts, e.g. by a plain `git pull`).
#
# --autostash: a merge blocked by uncommitted edits to files the merge changes sets those
# files — only those — aside in a stash named "git pull-all autostash", merges, and puts
# them back. If the merge itself conflicts, they stay aside until it is committed and the
# next run puts them back. If putting them back clashes, the file keeps conflict markers
# and a copy stays in `git stash list` renamed "... (applied with conflicts)", which no
# later run applies again.
#
# Merges are explicit (`git fetch` + `git merge`), never `git pull`: the result does not
# depend on a machine's pull.rebase / pull.ff settings, and `submodule.recurse` never checks
# the recorded pointers out into the submodules.
#
# Pinned by aisland/tests/test_git_pull_push_all.py. Written for macOS's /bin/bash 3.2.

# Typed inside a submodule, the alias inherits that repository's GIT_DIR and friends; every
# command below names its repository with -C, so drop them (as git's own scripts do).
unset $(git rev-parse --local-env-vars)

# The root: GIT_PULL_ALL_ROOT when the alias passes it (this file installed outside the workspace,
# aisland from a wheel); else this file is aisland/aisland/git/git_pull_all.sh, three levels below it.
WORKSPACE_ROOT="$(cd "${GIT_PULL_ALL_ROOT:-$(dirname "$0")/../../..}" && pwd)"
STASH_MSG="git pull-all autostash"
CLASH_MSG="$STASH_MSG (applied with conflicts)"
USAGE="usage: git pull-all [--autostash]"
FETCH_JOBS=8

AUTOSTASH=0
for arg in "$@"; do
    case "$arg" in
        --autostash) AUTOSTASH=1 ;;
        -h | --help)
            echo "$USAGE"
            exit 0
            ;;
        *)
            echo "$USAGE" >&2
            exit 2
            ;;
    esac
done

# --- output -------------------------------------------------------------------------------

NEEDS=""    # the "needs you:" lines, printed at the end
LISTED=""   # labels with a "needs you:" line, one per line
REPORTED=0  # whether anything changed

report() {
    printf '%-22s %s\n' "$1" "$2"
    REPORTED=1
}

need() {
    NEEDS="${NEEDS}$(printf '  %-20s %s' "$1" "$2")"$'\n'
    LISTED="${LISTED}$1"$'\n'
}

listed() {
    printf '%s' "$LISTED" | grep -Fxq -- "$1"
}

# Lines on stdin → "a, b, c".
join() {
    awk 'NR > 1 { printf ", " } { printf "%s", $0 }'
}

commits() {
    if [ "$1" = 1 ]; then echo "1 commit"; else echo "$1 commits"; fi
}

# --- repository state ----------------------------------------------------------------------

mid_merge() {
    git -C "$1" rev-parse -q --verify MERGE_HEAD >/dev/null
}

unmerged() {
    git -C "$1" diff --name-only --diff-filter=U | join
}

# The newest stash of $1 whose message is exactly $2 ("stash@{n}"), or nothing.
stash_ref() {
    git -C "$1" stash list --format='%gd%x09%gs' |
        awk -F'\t' -v m="$2" '{ s = $2; sub(/^[^:]*: /, "", s); if (s == m) { print $1; exit } }'
}

# Files with uncommitted edits that merging origin/$BRANCH into $1 would change. Submodule
# pointers are left out: the root merges over a moved submodule without touching it.
blocking_files() {
    local changed dirty
    changed=$(git -C "$1" diff --name-only --ignore-submodules=all HEAD..."origin/$BRANCH")
    dirty=$(git -C "$1" diff --name-only --ignore-submodules=all HEAD)
    if [ -z "$changed" ] || [ -z "$dirty" ]; then return 0; fi
    printf '%s\n' "$dirty" | grep -Fx -f <(printf '%s\n' "$changed")
}

# --- autostash -----------------------------------------------------------------------------

# Put back the edits a run set aside in $1 (label $2). Returns 1 when they clash.
put_back() {
    local repo="$1" label="$2" ref files out sha left
    ref=$(stash_ref "$repo" "$STASH_MSG")
    [ -n "$ref" ] || return 0
    files=$(git -C "$repo" stash show --name-only "$ref" | join)
    if out=$(git -C "$repo" stash pop -q "$ref" 2>&1); then
        report "$label" "put back your uncommitted edits to $files"
        return 0
    fi
    left=$(unmerged "$repo")
    if [ -z "$left" ]; then
        need "$label" "could not put back your uncommitted edits to $files ($(printf '%s\n' "$out" | head -n 1)); they stay in git stash list as '$STASH_MSG'"
        return 1
    fi
    # `stash pop` keeps the entry when it clashes. Rename it, so no later run applies it twice.
    sha=$(git -C "$repo" rev-parse "$ref")
    git -C "$repo" stash drop -q "$ref"
    git -C "$repo" stash store -q -m "On $(git -C "$repo" branch --show-current): $CLASH_MSG" "$sha"
    need "$label" "your uncommitted edits to $files clash with the pulled changes: conflict markers in $left — fix them, then git reset -q -- <file>; a copy stays in git stash list as '$CLASH_MSG'"
    return 1
}

STASHED_FILES=""  # what merge_origin set aside for the merge it left in conflict

stash_note() {
    [ -n "$STASHED_FILES" ] || return 0
    printf '; your uncommitted edits to %s are set aside until the merge is committed — the next git pull-all puts them back' "$STASHED_FILES"
}

# --- merging -------------------------------------------------------------------------------

# Merge origin/$BRANCH into $1 (label $2). Returns 0 when merged or nothing came in, 1 when
# it is listed under "needs you:" and left as it was, 3 when the merge stopped on conflicts
# (left mid-merge; the caller decides what to say).
merge_origin() {
    local repo="$1" label="$2" left incoming out
    local -a files=()
    STASHED_FILES=""
    left=$(unmerged "$repo")
    if [ -n "$left" ]; then
        if [ -n "$(stash_ref "$repo" "$CLASH_MSG")" ]; then
            need "$label" "unresolved conflict in $left from putting back your edits — fix it, then git reset -q -- <file>"
        else
            need "$label" "unresolved conflict in $left — fix it first"
        fi
        return 1
    fi
    put_back "$repo" "$label" || return 1
    if ! git -C "$repo" rev-parse -q --verify "refs/remotes/origin/$BRANCH" >/dev/null; then
        need "$label" "origin has no $BRANCH — not pulled"
        return 1
    fi
    incoming=$(git -C "$repo" rev-list --count "HEAD..origin/$BRANCH")
    [ "$incoming" = 0 ] && return 0

    while IFS= read -r f; do
        [ -n "$f" ] && files+=("$f")
    done < <(blocking_files "$repo")
    if [ ${#files[@]} -gt 0 ]; then
        if [ "$AUTOSTASH" != 1 ]; then
            need "$label" "not merged: uncommitted edits to $(printf '%s\n' "${files[@]}" | join) would be overwritten — git pull-all --autostash sets them aside and puts them back"
            return 1
        fi
        if ! out=$(git -C "$repo" stash push -q -m "$STASH_MSG" -- "${files[@]}" 2>&1); then
            need "$label" "not merged: could not set aside your edits ($(printf '%s\n' "$out" | head -n 1))"
            return 1
        fi
        STASHED_FILES=$(printf '%s\n' "${files[@]}" | join)
    fi

    if out=$(git -C "$repo" merge -q --no-edit "origin/$BRANCH" 2>&1); then
        report "$label" "merged origin/$BRANCH ($(commits "$incoming"))"
        put_back "$repo" "$label"
        return 0
    fi
    mid_merge "$repo" && return 3
    need "$label" "not merged: $(printf '%s\n' "$out" | grep -v '^hint:' | head -n 3 | tr '\n\t' '  ')"
    put_back "$repo" "$label"
    return 1
}

# Settle each submodule-pointer conflict of the root's merge to the submodule's checked-out
# commit, when that commit contains both sides.
settle_pointers() {
    local path ours theirs head sub
    while IFS= read -r path; do
        [ -n "$path" ] || continue
        sub="$WORKSPACE_ROOT/$path"
        ours=$(git -C "$WORKSPACE_ROOT" rev-parse -q --verify ":2:$path") || continue
        theirs=$(git -C "$WORKSPACE_ROOT" rev-parse -q --verify ":3:$path") || continue
        [ -e "$sub/.git" ] || continue
        head=$(git -C "$sub" rev-parse -q --verify HEAD) || continue
        git -C "$sub" merge-base --is-ancestor "$ours" "$head" 2>/dev/null || continue
        git -C "$sub" merge-base --is-ancestor "$theirs" "$head" 2>/dev/null || continue
        git -C "$WORKSPACE_ROOT" add -- "$path"
        report root "pointer conflict $path → ${head:0:7} (holds both sides)"
    done < <(git -C "$WORKSPACE_ROOT" ls-files -u |
        awk -F'\t' '{ split($1, f, " "); if (f[1] == "160000") print $2 }' | sort -u)
}

# Commit the root's merge once nothing is left unresolved; returns 1 (listed) otherwise.
conclude_root_merge() {
    local left
    settle_pointers
    left=$(unmerged "$WORKSPACE_ROOT")
    if [ -n "$left" ]; then
        need root "CONFLICT in $left — resolve, commit, then run git pull-all again$(stash_note)"
        return 1
    fi
    # --cleanup=strip: without an editor git keeps MERGE_MSG's "# Conflicts:" lines.
    git -C "$WORKSPACE_ROOT" commit -q --no-edit --cleanup=strip
}

root_step() {
    local incoming
    if mid_merge "$WORKSPACE_ROOT"; then
        STASHED_FILES=""
        conclude_root_merge || return 1
        report root "finished the merge in progress"
    fi
    if [ "$SUBMODULE_MID_MERGE" = 1 ]; then
        need root "not merged: finish the submodule merges above first, then run git pull-all again"
        return 1
    fi
    incoming=$(git -C "$WORKSPACE_ROOT" rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null)
    merge_origin "$WORKSPACE_ROOT" root
    case $? in
        3)
            conclude_root_merge || return 1
            report root "merged origin/$BRANCH ($(commits "$incoming"))"
            put_back "$WORKSPACE_ROOT" root
            ;;
    esac
}

# One root commit recording each submodule that holds commits from origin the root does not
# record yet. Only once the root holds everything origin has and no merge is in progress.
record_pointers() {
    local sm sub up rec head names
    local -a paths=()
    mid_merge "$WORKSPACE_ROOT" && return 0
    git -C "$WORKSPACE_ROOT" merge-base --is-ancestor "origin/$BRANCH" HEAD 2>/dev/null || return 0
    for sm in "${SUBMODULES[@]}"; do
        listed "$sm" && continue
        sub="$WORKSPACE_ROOT/$sm"
        [ "$(git -C "$sub" branch --show-current)" = "$BRANCH" ] || continue
        up=$(git -C "$sub" rev-parse -q --verify "refs/remotes/origin/$BRANCH") || continue
        rec=$(git -C "$WORKSPACE_ROOT" rev-parse -q --verify "HEAD:$sm") || continue
        head=$(git -C "$sub" rev-parse HEAD)
        [ "$rec" = "$head" ] && continue
        if ! git -C "$sub" merge-base --is-ancestor "$rec" "$head" 2>/dev/null; then
            need "$sm" "the root records ${rec:0:7}, which $sm's $BRANCH does not contain — pointer left as recorded"
            continue
        fi
        # origin brought nothing the root lacks: the unrecorded commits are local work.
        git -C "$sub" merge-base --is-ancestor "$up" "$rec" && continue
        git -C "$sub" merge-base --is-ancestor "$up" "$head" || continue
        # someone staged a different pointer by hand: theirs to commit.
        [ "$(git -C "$WORKSPACE_ROOT" rev-parse -q --verify ":$sm")" = "$rec" ] || continue
        paths+=("$sm")
    done
    [ ${#paths[@]} -gt 0 ] || return 0
    names=$(printf '%s\n' "${paths[@]}" | join)
    # A pathspec commits only these paths: anything else staged in the root stays staged.
    git -C "$WORKSPACE_ROOT" commit -q -m "chore: sync submodule pointers after pull-all ($names)" -- "${paths[@]}"
    report root "recorded submodule pointers: $names"
}

# Print what needs the user and exit 1, or say nothing changed; exit 0.
finish() {
    if [ -n "$NEEDS" ]; then
        printf '\nneeds you:\n%s' "$NEEDS"
        exit 1
    fi
    [ "$REPORTED" = 1 ] || echo "everything up to date"
    exit 0
}

# Fetch the root (label `root`) and the folders named after it, in parallel; a failure is listed.
fetch_all() {
    local fails
    fails=$(mktemp)
    printf '%s\0' "$@" | xargs -0 -n 1 -P "$FETCH_JOBS" sh -c '
        dir="$2/$3"; [ "$3" = root ] && dir="$2"
        out=$(git -C "$dir" fetch -q --no-recurse-submodules origin 2>&1) ||
            printf "%s\t%s\n" "$3" "$(printf "%s\n" "$out" | head -n 1)" >> "$1"
    ' _ "$fails" "$WORKSPACE_ROOT"
    while IFS="$(printf '\t')" read -r label err; do
        need "$label" "not fetched: $err"
    done <"$fails"
    rm -f "$fails"
}

# A workspace without .gitmodules: each subfolder that is its own git repository (a clone) is merged
# with origin on ITS OWN branch — independent clones share none — then the root, when it is a
# repository. Nothing records the clones' commits, so there are no pointers to record.
folder_main() {
    local -a clones=() labels=()
    local dir name branch noun
    for dir in "$WORKSPACE_ROOT"/*/; do
        dir="${dir%/}"
        [ -e "$dir/.git" ] && clones+=("$(basename "$dir")")
    done
    if [ ${#clones[@]} -eq 1 ]; then noun="1 repository"; else noun="${#clones[@]} repositories"; fi
    if [ -d "$WORKSPACE_ROOT/.git" ]; then
        labels=(root)
        echo "fetching the root and $noun…"
    elif [ ${#clones[@]} -gt 0 ]; then
        echo "fetching $noun…"
    else
        echo "no git repository in $WORKSPACE_ROOT — nothing pulled"
        exit 1
    fi
    [ ${#clones[@]} -gt 0 ] && labels+=("${clones[@]}")
    fetch_all "${labels[@]}"

    for name in "${clones[@]}"; do
        dir="$WORKSPACE_ROOT/$name"
        if mid_merge "$dir"; then
            need "$name" "CONFLICT in $(unmerged "$dir") — resolve, commit, then run git pull-all again"
            continue
        fi
        branch=$(git -C "$dir" branch --show-current)
        if [ -z "$branch" ]; then
            need "$name" "detached HEAD — not pulled"
            continue
        fi
        BRANCH="$branch"
        merge_origin "$dir" "$name"
        if [ $? = 3 ]; then
            need "$name" "CONFLICT in $(unmerged "$dir") — resolve, commit, then run git pull-all again$(stash_note)"
        fi
    done

    if [ -d "$WORKSPACE_ROOT/.git" ]; then
        BRANCH=$(git -C "$WORKSPACE_ROOT" branch --show-current)
        if [ -z "$BRANCH" ]; then
            need root "detached HEAD — not pulled"
        else
            SUBMODULE_MID_MERGE=0
            root_step
        fi
    fi
    finish
}

# --- main ----------------------------------------------------------------------------------

[ -f "$WORKSPACE_ROOT/.gitmodules" ] || folder_main

BRANCH=$(git -C "$WORKSPACE_ROOT" branch --show-current)
if [ -z "$BRANCH" ]; then
    echo "the root is not on a branch — nothing pulled"
    exit 1
fi

if ! SUBMODULE_PATHS=$(git -C "$WORKSPACE_ROOT" submodule --quiet foreach 'echo "$sm_path"' 2>&1); then
    echo "cannot list the submodules of $WORKSPACE_ROOT — nothing pulled: $SUBMODULE_PATHS"
    exit 1
fi
SUBMODULES=()
while IFS= read -r sm; do
    [ -n "$sm" ] && SUBMODULES+=("$sm")
done <<<"$SUBMODULE_PATHS"

# 1. Fetch everything first, so an unreachable remote is known before anything merges.
#    --no-recurse-submodules: each submodule is fetched here once, not again by the root.
if [ ${#SUBMODULES[@]} = 1 ]; then echo "fetching the root and 1 submodule…"; else echo "fetching the root and ${#SUBMODULES[@]} submodules…"; fi
fetch_all root "${SUBMODULES[@]}"

# 2. The submodules.
SUBMODULE_MID_MERGE=0
for sm in "${SUBMODULES[@]}"; do
    sub="$WORKSPACE_ROOT/$sm"
    if mid_merge "$sub"; then
        left=$(unmerged "$sub")
        if [ -n "$left" ]; then
            need "$sm" "CONFLICT in $left — resolve, commit, then run git pull-all again"
        else
            need "$sm" "merge in progress — commit it, then run git pull-all again"
        fi
        SUBMODULE_MID_MERGE=1
        continue
    fi
    branch=$(git -C "$sub" branch --show-current)
    if [ -z "$branch" ]; then
        need "$sm" "detached HEAD — not pulled"
        continue
    fi
    if [ "$branch" != "$BRANCH" ]; then
        need "$sm" "on $branch, the root is on $BRANCH — not pulled"
        continue
    fi
    merge_origin "$sub" "$sm"
    if [ $? = 3 ]; then
        need "$sm" "CONFLICT in $(unmerged "$sub") — resolve, commit, then run git pull-all again$(stash_note)"
        SUBMODULE_MID_MERGE=1
    fi
done

# 3. The root, then 4. its pointers.
root_step
record_pointers
finish
