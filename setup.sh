#!/bin/bash
# Gearlux Workspace Bootstrap Script
# This script creates the project virtual environment and installs the aisland manager.
set -e

# Resolve the project root (one level up from this script)
WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$WORKSPACE_ROOT/.venv"

echo "==> Gearlux Bootstrap: Initializing environment at $WORKSPACE_ROOT"

# 1. Ensure Python 3.12+ is available
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>/dev/null; then
    PYTHON_BIN=$(command -v python3)
elif command -v python3.12 &> /dev/null; then
    PYTHON_BIN=$(command -v python3.12)
else
    echo "==> Python 3.12+ not found. Installing Python 3.12 via Homebrew..."
    if ! command -v brew &> /dev/null; then
        echo "Error: Homebrew is not installed. Please install Homebrew or Python 3.12 manually."
        exit 1
    fi
    brew install python@3.12
    PYTHON_BIN=$(command -v python3.12)
fi

# 2. Create/Verify Virtual Environment
# Try to get the git project name for the prompt
PROMPT_NAME=$(git -C "$WORKSPACE_ROOT" remote get-url origin 2>/dev/null | sed 's/.*\/\([^/]*\)\.git/\1/')
if [ -z "$PROMPT_NAME" ]; then
    PROMPT_NAME=$(basename "$WORKSPACE_ROOT")
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtual environment ($PYTHON_BIN) with prompt: $PROMPT_NAME..."
    if command -v uv &> /dev/null; then
        uv venv --prompt "$PROMPT_NAME" --python "$PYTHON_BIN" "$VENV_DIR"
    else
        "$PYTHON_BIN" -m venv --prompt "$PROMPT_NAME" "$VENV_DIR"
        # Fix the double-parenthesis prompt bug in some venv versions (e.g. Python 3.12)
        # The template in some versions is PS1="("__VENV_PROMPT__") ${PS1:-}"
        # while context.prompt already includes parentheses.
        if [ -d "$VENV_DIR/bin" ]; then
            # Bash/Zsh
            if [ -f "$VENV_DIR/bin/activate" ]; then
                sed -i "" 's/PS1="(\(.*\)) /PS1="\1 /' "$VENV_DIR/bin/activate" 2>/dev/null || \
                sed -i 's/PS1="(\(.*\)) /PS1="\1 /' "$VENV_DIR/bin/activate"
            fi
            # Fish
            if [ -f "$VENV_DIR/bin/activate.fish" ]; then
                sed -i "" "s/printf '%s%s%s' (set_color 4B8BBE) '(\(.*\)) ' (set_color normal)/printf '%s%s%s' (set_color 4B8BBE) '\1 ' (set_color normal)/" "$VENV_DIR/bin/activate.fish" 2>/dev/null || \
                sed -i "s/printf '%s%s%s' (set_color 4B8BBE) '(\(.*\)) ' (set_color normal)/printf '%s%s%s' (set_color 4B8BBE) '\1 ' (set_color normal)/" "$VENV_DIR/bin/activate.fish"
            fi
            # Csh
            if [ -f "$VENV_DIR/bin/activate.csh" ]; then
                sed -i "" 's/set env_name = "(\(.*\)) "/set env_name = "\1 "/' "$VENV_DIR/bin/activate.csh" 2>/dev/null || \
                sed -i 's/set env_name = "(\(.*\)) "/set env_name = "\1 "/' "$VENV_DIR/bin/activate.csh"
            fi
        fi
    fi
else
    # Check version of existing venv
    VENV_PYTHON_VERSION=$("$VENV_DIR/bin/python" --version 2>/dev/null | cut -d' ' -f2 || echo "0.0.0")
    if [[ ! "$VENV_PYTHON_VERSION" =~ ^3\.1[2-9] ]]; then
         echo "==> Existing virtual environment is version $VENV_PYTHON_VERSION. Recreating with 3.12+..."
         rm -rf "$VENV_DIR"
         if command -v uv &> /dev/null; then
             uv venv --prompt "$PROMPT_NAME" --python "$PYTHON_BIN" "$VENV_DIR"
         else
             "$PYTHON_BIN" -m venv --prompt "$PROMPT_NAME" "$VENV_DIR"
             if [ -d "$VENV_DIR/bin" ]; then
                 # Bash/Zsh
                 if [ -f "$VENV_DIR/bin/activate" ]; then
                     sed -i "" 's/PS1="(\(.*\)) /PS1="\1 /' "$VENV_DIR/bin/activate" 2>/dev/null || \
                     sed -i 's/PS1="(\(.*\)) /PS1="\1 /' "$VENV_DIR/bin/activate"
                 fi
                 # Fish
                 if [ -f "$VENV_DIR/bin/activate.fish" ]; then
                     sed -i "" "s/printf '%s%s%s' (set_color 4B8BBE) '(\(.*\)) ' (set_color normal)/printf '%s%s%s' (set_color 4B8BBE) '\1 ' (set_color normal)/" "$VENV_DIR/bin/activate.fish" 2>/dev/null || \
                     sed -i "s/printf '%s%s%s' (set_color 4B8BBE) '(\(.*\)) ' (set_color normal)/printf '%s%s%s' (set_color 4B8BBE) '\1 ' (set_color normal)/" "$VENV_DIR/bin/activate.fish"
                 fi
                 # Csh
                 if [ -f "$VENV_DIR/bin/activate.csh" ]; then
                     sed -i "" 's/set env_name = "(\(.*\)) "/set env_name = "\1 "/' "$VENV_DIR/bin/activate.csh" 2>/dev/null || \
                     sed -i 's/set env_name = "(\(.*\)) "/set env_name = "\1 "/' "$VENV_DIR/bin/activate.csh"
                 fi
             fi
         fi
    fi
fi

# 3. Activate and upgrade core tools
source "$VENV_DIR/bin/activate"
echo "==> Upgrading core build tools..."
# torch 2.11.0 pins `setuptools<82`; unconstrained upgrades pull 82.0.1
# and break the next torch install. Cap the upgrade until torch lifts it.
pip install --upgrade pip "setuptools<82" wheel
echo "==> Installing uv for fast dependency resolution..."
pip install uv

# 3. Install Aisland Manager in editable mode
echo "==> Installing aisland manager..."
uv pip install -e "$WORKSPACE_ROOT/aisland"

# 4. Hand off to 'aisland setup' for full project provisioning
echo "==> Handing off to 'aisland setup' for full project provisioning..."
cd "$WORKSPACE_ROOT"
# We call it from the root so Path.glob("**/*.toml") works correctly
"$VENV_DIR/bin/aisland" setup

# 5. Install shell completion into a workspace-local rc file.
# The generated file is sourced by project.bashrc and is gitignored, so
# this step keeps the user's global ~/.bashrc clean and leaves no diff
# in the tracked tree. Auto-discovers every Liquifai app in the venv.
#
# THREE completion families, because the workspace has three kinds of CLI:
#   (a) liquifai apps  — the `liquifai.apps` entry-point group (recordstream,
#       marainer, sonair, annotaide, ...): handled by liquifai-install-completions.
#   (b) click apps     — `aisland`: click ships its own `_<PROG>_COMPLETE` protocol.
#   (c) argparse apps  — `streamstudio`: neither of the above applies, so it uses
#       argcomplete (declared as a streamstudio dependency; cli.py calls
#       argcomplete.autocomplete).
# (b) and (c) were previously missing entirely, which is why `aisland <TAB>` and
# `streamstudio <TAB>` did nothing while every liquifai app completed fine.
COMPLETION_RC="$WORKSPACE_ROOT/.project.bashrc.completion"
echo "==> Installing shell completion → $COMPLETION_RC"
if command -v liquifai-install-completions >/dev/null 2>&1; then
    liquifai-install-completions --target-rc "$COMPLETION_RC" \
        || echo "    (completion install failed; non-fatal — re-run setup.sh after fixing)"
else
    echo "    skip: liquifai-install-completions not found (liquifai not installed yet?)"
fi

# 5a. Prune stale per-app blocks. liquifai-install-completions MERGES by marker
# block (liquifai/completion/install.py::_replace_block) and leaves blocks it did
# not write untouched — so a renamed or uninstalled app keeps a dead block forever
# (the fluxstudio -> streamstudio rename left exactly that). Drop any block whose
# program is no longer on PATH; the next run re-adds anything still installed.
#
# Gated on the venv being active (`liquifai` itself resolving): without it NO app is
# on PATH and a naive pruner would delete every block. setup.sh sources the venv at
# step 2, so this holds — the guard only protects a standalone re-run of this section.
if [ -f "$COMPLETION_RC" ] && command -v liquifai >/dev/null 2>&1; then
    for prog in $(grep -oE '^# >>> liquifai completion for [A-Za-z0-9_.-]+' "$COMPLETION_RC" \
                  | sed 's/.*for //' | sort -u); do
        if ! command -v "$prog" >/dev/null 2>&1; then
            echo "    pruning stale completion block: $prog (no longer on PATH)"
            awk -v p="$prog" '
                $0 == "# >>> liquifai completion for " p " >>>" { skip = 1 }
                skip != 1 { print }
                $0 == "# <<< liquifai completion for " p " <<<" { skip = 0 }
            ' "$COMPLETION_RC" > "$COMPLETION_RC.tmp" && mv "$COMPLETION_RC.tmp" "$COMPLETION_RC"
        fi
    done
fi

# 5b. Non-liquifai CLIs. Written with the same >>>/<<< marker convention so this
# block is idempotent (rewritten wholesale on every setup) and so 5a's pruner and
# liquifai's installer never collide with it.
if [ -f "$COMPLETION_RC" ]; then
    awk '
        $0 == "# >>> acme completion for non-liquifai CLIs >>>" { skip = 1 }
        skip != 1 { print }
        $0 == "# <<< acme completion for non-liquifai CLIs <<<" { skip = 0 }
    ' "$COMPLETION_RC" > "$COMPLETION_RC.tmp" && mv "$COMPLETION_RC.tmp" "$COMPLETION_RC"
fi
{
    echo "# >>> acme completion for non-liquifai CLIs >>>"
    echo "# Generated by: aisland/setup.sh (section 5b) — DO NOT EDIT BY HAND."
    echo "# To regenerate: bash aisland/setup.sh  (or re-run just this section)"
    echo "# click apps expose \`_<PROG>_COMPLETE=bash_source <prog>\`; argparse apps"
    echo "# use argcomplete's \`register-python-argcomplete <prog>\`."
    # (b) click — aisland. Click refuses to emit for bash < 4.4 (macOS /bin/bash is
    # 3.2), so guard on BASH_VERSINFO rather than emitting a script that errors.
    if command -v aisland >/dev/null 2>&1; then
        echo 'if [ "${BASH_VERSINFO[0]}" -gt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 4 ]; }; then'
        echo '    eval "$(_AISLAND_COMPLETE=bash_source aisland 2>/dev/null)" || true'
        echo 'fi'
    fi
    # (c) argcomplete — streamstudio.
    if command -v streamstudio >/dev/null 2>&1 && command -v register-python-argcomplete >/dev/null 2>&1; then
        echo 'eval "$(register-python-argcomplete streamstudio 2>/dev/null)" || true'
    fi
    echo "# <<< acme completion for non-liquifai CLIs <<<"
} >> "$COMPLETION_RC"

# 6. Initialize Git Configuration
echo "==> Setting up Git configurations..."
bash "$WORKSPACE_ROOT/aisland/setup_git.sh"

# 7. Link workspace .vscode/tasks.json to aisland/tasks.json so the integrated
# Simple Browser shortcuts (Jenkins, MLflow, LabelStudio, FiftyOne, ...) are
# available in every fresh checkout. The canonical, version-controlled copy
# lives in aisland/tasks.json; idempotent `ln -sf` replaces any pre-existing
# link without erroring.
TASKS_LINK="$WORKSPACE_ROOT/.vscode/tasks.json"
if [ -f "$WORKSPACE_ROOT/aisland/tasks.json" ]; then
    mkdir -p "$WORKSPACE_ROOT/.vscode"
    echo "==> Linking .vscode/tasks.json → aisland/tasks.json"
    ln -sf "../aisland/tasks.json" "$TASKS_LINK"
fi

echo "==> Setup complete! Your environment is ready."
echo "==> Run 'source .venv/bin/activate' to start working."
