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
    "$PYTHON_BIN" -m venv --prompt "$PROMPT_NAME" "$VENV_DIR"
else
    # Check version of existing venv
    VENV_PYTHON_VERSION=$("$VENV_DIR/bin/python" --version 2>/dev/null | cut -d' ' -f2 || echo "0.0.0")
    if [[ ! "$VENV_PYTHON_VERSION" =~ ^3\.1[2-9] ]]; then
         echo "==> Existing virtual environment is version $VENV_PYTHON_VERSION. Recreating with 3.12+..."
         rm -rf "$VENV_DIR"
         "$PYTHON_BIN" -m venv --prompt "$PROMPT_NAME" "$VENV_DIR"
    fi
fi

# 3. Activate and upgrade core tools
source "$VENV_DIR/bin/activate"
echo "==> Upgrading core build tools..."
pip install --upgrade pip setuptools wheel

# 3. Install Aisland Manager in editable mode
echo "==> Installing aisland manager..."
pip install -e "$WORKSPACE_ROOT/aisland"

# 4. Hand off to 'aisland setup' for full project provisioning
echo "==> Handing off to 'aisland setup' for full project provisioning..."
cd "$WORKSPACE_ROOT"
# We call it from the root so Path.glob("**/*.toml") works correctly
"$VENV_DIR/bin/aisland" setup

# 5. Initialize Git Configuration
echo "==> Setting up Git configurations..."
bash "$WORKSPACE_ROOT/aisland/setup_git.sh"

echo "==> Setup complete! Your environment is ready."
echo "==> Run 'source .venv/bin/activate' to start working."
