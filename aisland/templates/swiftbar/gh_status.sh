
# Configuration
if ! command -v gh &> /dev/null; then
    echo "GH: ⚠️"
    echo "---"
    echo "GitHub CLI (gh) not found."
    exit 0
fi

# Monitored repositories
REPOS=$(echo -e "<[ repos ]>")

if [ -z "$REPOS" ]; then
    echo "GH: ⚪️"
    echo "---"
    echo "No GitHub repositories found in workspace."
    exit 0
fi

GLOBAL_FAILED=false
GLOBAL_RUNNING=false
GLOBAL_SUCCESS=false
DROPDOWN_TEXT=""

for REPO in $REPOS; do
    # Get last run status
    LAST_RUN=$(gh run list --repo "$REPO" --limit 1 --json status,conclusion,workflowName --jq '.[0]' 2>/dev/null)

    # If LAST_RUN is empty or "null", there are no runs
    if [ -z "$LAST_RUN" ] || [ "$LAST_RUN" == "null" ]; then
        DROPDOWN_TEXT+="${REPO}: ⚪️ (No runs) | href=https://github.com/${REPO}/actions\n"
        continue
    fi

    STATUS=$(echo "$LAST_RUN" | jq -r '.status')
    CONCLUSION=$(echo "$LAST_RUN" | jq -r '.conclusion')
    WORKFLOW=$(echo "$LAST_RUN" | jq -r '.workflowName')

    if [ "$STATUS" == "in_progress" ] || [ "$STATUS" == "queued" ]; then
        GLOBAL_RUNNING=true
        DROPDOWN_TEXT+="${REPO}: 🟡 (Running) | href=https://github.com/${REPO}/actions\n"
    elif [ "$CONCLUSION" == "failure" ]; then
        GLOBAL_FAILED=true
        DROPDOWN_TEXT+="${REPO}: 🔴 (Failed: ${WORKFLOW}) | href=https://github.com/${REPO}/actions\n"
    elif [ "$CONCLUSION" == "success" ]; then
        GLOBAL_SUCCESS=true
        DROPDOWN_TEXT+="${REPO}: 🟢 | href=https://github.com/${REPO}/actions\n"
    elif [ "$CONCLUSION" == "null" ] || [ -z "$CONCLUSION" ]; then
        # This can happen if it is currently running or just completed without conclusion yet
        if [ "$STATUS" == "completed" ]; then
             DROPDOWN_TEXT+="${REPO}: ⚪️ (Completed, no status) | href=https://github.com/${REPO}/actions\n"
        else
             GLOBAL_RUNNING=true
             DROPDOWN_TEXT+="${REPO}: 🟡 (Running) | href=https://github.com/${REPO}/actions\n"
        fi
    else
        DROPDOWN_TEXT+="${REPO}: 🟡 (${CONCLUSION}) | href=https://github.com/${REPO}/actions\n"
    fi
done

if [ "$GLOBAL_FAILED" = true ]; then
    echo "Actions: 🔴"
elif [ "$GLOBAL_RUNNING" = true ]; then
    echo "Actions: 🟡"
elif [ "$GLOBAL_SUCCESS" = true ]; then
    echo "Actions: 🟢"
else
    echo "Actions: ⚪️"
fi

echo "---"
echo -e "$DROPDOWN_TEXT"
echo "---"
echo "Refresh Now | refresh=true"
