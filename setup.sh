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
pip install --upgrade pip setuptools wheel
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

# 5. Install liquifai shell completion into a workspace-local rc file.
# The generated file is sourced by project.bashrc and is gitignored, so
# this step keeps the user's global ~/.bashrc clean and leaves no diff
# in the tracked tree. Auto-discovers every Liquifai app in the venv.
COMPLETION_RC="$WORKSPACE_ROOT/.project.bashrc.completion"
echo "==> Installing liquifai shell completion → $COMPLETION_RC"
if command -v liquifai-install-completions >/dev/null 2>&1; then
    liquifai-install-completions --target-rc "$COMPLETION_RC" \
        || echo "    (completion install failed; non-fatal — re-run setup.sh after fixing)"
else
    echo "    skip: liquifai-install-completions not found (liquifai not installed yet?)"
fi

# 6. Initialize Git Configuration
echo "==> Setting up Git configurations..."
bash "$WORKSPACE_ROOT/aisland/setup_git.sh"

echo "==> Setup complete! Your environment is ready."
echo "==> Run 'source .venv/bin/activate' to start working."
