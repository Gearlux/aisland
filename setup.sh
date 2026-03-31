#!/bin/bash
# Gearlux Workspace Bootstrap Script
# This script creates the project virtual environment and installs the aisland manager.
set -e

# Resolve the project root (one level up from this script)
WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$WORKSPACE_ROOT/.venv"

echo "==> Gearlux Bootstrap: Initializing environment at $WORKSPACE_ROOT"

# 1. Create/Verify Virtual Environment
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating virtual environment (Python 3.12)..."
    python3.12 -m venv "$VENV_DIR"
fi

# 2. Activate and upgrade core tools
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

echo "==> Setup complete! Your environment is ready."
echo "==> Run 'source .venv/bin/activate' to start working."
