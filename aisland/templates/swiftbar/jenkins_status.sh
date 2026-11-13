
# Jenkins Data Root
JENKINS_HOME=<[ jenkins_home ]>
JENKINS_URL=<[ jenkins_url ]>
JOBS_DIR="${JENKINS_HOME}/jobs"

GLOBAL_FAILED=false
DROPDOWN_TEXT=""

# Define projects to check
PROJECTS=(<[ projects ]>)

# Function to process a job/branch directory
process_item() {
    local ITEM_DIR="$1"
    local PROJECT_NAME="$2"
    local DISPLAY_NAME="$3"
    local IS_BRANCH="$4"

    CONFIG_XML="${ITEM_DIR}/config.xml"
    if [ -z "$DISPLAY_NAME" ] && [ -f "$CONFIG_XML" ]; then
        # Extract name from config.xml
        DISPLAY_NAME=$(sed -n 's/.*<displayName>\([^<]*\)<\/displayName>.*/\1/p' \
            "$CONFIG_XML" | head -n 1)
        if [ -z "$DISPLAY_NAME" ]; then
            DISPLAY_NAME=$(sed -n 's/.*<sourceBranch>\([^<]*\)<\/sourceBranch>.*/\1/p' \
                "$CONFIG_XML" | head -n 1)
        fi
        if [ -z "$DISPLAY_NAME" ]; then
            DISPLAY_NAME=$(sed -n 's/.*<name>\([^<]*\)<\/name>.*/\1/p' \
                "$CONFIG_XML" | head -n 1)
        fi
    fi

    if [ -z "$DISPLAY_NAME" ]; then
        ITEM_NAME=$(basename "$ITEM_DIR")
        # Strip Jenkins hash (e.g., dev-main.k31dj -> dev-main)
        DISPLAY_NAME=$(echo "$ITEM_NAME" | cut -d. -f1)
        # Reconstruct common branch patterns (e.g., dev-main -> dev/main)
        if [[ "$DISPLAY_NAME" == "dev-main" ]]; then
            DISPLAY_NAME="dev/main"
        fi
    fi

    PERMALINKS="${ITEM_DIR}/builds/permalinks"
    STATUS_STR=""

    if [ -f "$PERMALINKS" ]; then
        LAST_COMPLETED=$(grep "lastCompletedBuild" "$PERMALINKS" | awk '{print $2}')

        if [ "$LAST_COMPLETED" == "-1" ] || [ -z "$LAST_COMPLETED" ]; then
            STATUS_STR="⚪️ (No builds)"
        else
            BUILD_XML="${ITEM_DIR}/builds/${LAST_COMPLETED}/build.xml"
            if [ -f "$BUILD_XML" ]; then
                RESULT=$(sed -n 's/.*<result>\([^<]*\)<\/result>.*/\1/p' "$BUILD_XML" | head -n 1)

                if [ "$RESULT" == "SUCCESS" ]; then
                    STATUS_STR="🟢"
                elif [ "$RESULT" == "FAILURE" ]; then
                    STATUS_STR="🔴"
                    GLOBAL_FAILED=true
                elif [ "$RESULT" == "ABORTED" ]; then
                    STATUS_STR="⚪️ (Aborted)"
                else
                    STATUS_STR="🟡 (${RESULT})"
                fi
            else
                STATUS_STR="❔ (xml missing)"
            fi
        fi
    else
        STATUS_STR="⚪️ (Not indexed)"
    fi

    if [ "$IS_BRANCH" == "true" ]; then
        ENCODED_BRANCH=$(echo "$DISPLAY_NAME" | sed 's/\//%2F/g')
        URL="${JENKINS_URL}/job/${PROJECT_NAME}/job/${ENCODED_BRANCH}"
        DROPDOWN_TEXT+="${DISPLAY_NAME}: ${STATUS_STR} | href=${URL}\n"
    else
        URL="${JENKINS_URL}/job/${PROJECT_NAME}"
        DROPDOWN_TEXT+="Status: ${STATUS_STR} | href=${URL}\n"
    fi
}

for PROJECT in "${PROJECTS[@]}"; do
    PROJECT_DIR="${JOBS_DIR}/${PROJECT}"

    if [ ! -d "$PROJECT_DIR" ]; then
        continue
    fi

    DROPDOWN_TEXT+="${PROJECT} | color=gray size=11\n"

    BRANCHES_DIR="${PROJECT_DIR}/branches"
    if [ -d "$BRANCHES_DIR" ]; then
        # Handle multibranch pipeline
        HAS_BRANCHES=false
        for BRANCH_DIR_PATH in "${BRANCHES_DIR}"/*; do
            if [ ! -d "$BRANCH_DIR_PATH" ]; then continue; fi
            HAS_BRANCHES=true
            process_item "$BRANCH_DIR_PATH" "$PROJECT" "" "true"
        done
        if [ "$HAS_BRANCHES" = false ]; then
            DROPDOWN_TEXT+="No branches discovered | color=red\n"
        fi
    elif [ -d "${PROJECT_DIR}/builds" ]; then
        # Handle direct job (like .local jobs)
        process_item "$PROJECT_DIR" "$PROJECT" "$PROJECT" "false"
    else
        DROPDOWN_TEXT+="No builds or branches found | color=red\n"
    fi
    DROPDOWN_TEXT+="---\n"
done

if [ "$GLOBAL_FAILED" = true ]; then
    echo "Jenkins: 🔴"
else
    echo "Jenkins: 🟢"
fi

echo "---"
echo -e "$DROPDOWN_TEXT"
echo "---"
echo "Refresh Now | refresh=true"
echo "Open Jenkins | href=${JENKINS_URL}"
