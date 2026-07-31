#!/bin/bash
# Gearlux Workspace Bootstrap Script
# This script creates the project virtual environment and installs the aisland manager.
set -e

# Resolve the project root (one level up from this script)
WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$WORKSPACE_ROOT/.venv"

echo "==> Gearlux Bootstrap: Initializing environment at $WORKSPACE_ROOT"

# 1. Ensure a SUPPORTED Python is available.
#
# The workspace pins a MAXIMUM, not just a minimum, and the reason is wheels rather than
# language features: TensorFlow 2.21 (the latest) publishes cp310-cp313 only, so a venv built
# on 3.14 cannot install it AT ALL — `uv pip install tensorflow` fails with "we only found
# wheels ... cp310, cp311, cp312, cp313". That is not a size or policy decision; it is a wall.
# Measured 2026-07-31 on a 3.14.6 venv, which is why this pin exists.
#
# `WORKSPACE_PYTHON` picks the interpreter. Raise it when the frameworks this workspace
# installs all publish wheels for the next version — check TensorFlow first, it lags most.
WORKSPACE_PYTHON="${WORKSPACE_PYTHON:-3.13}"

if command -v "python$WORKSPACE_PYTHON" &> /dev/null; then
    PYTHON_BIN=$(command -v "python$WORKSPACE_PYTHON")
else
    echo "==> Python $WORKSPACE_PYTHON not found. Installing it via Homebrew..."
    if ! command -v brew &> /dev/null; then
        echo "Error: Homebrew is not installed. Install Homebrew or Python $WORKSPACE_PYTHON manually."
        exit 1
    fi
    brew install "python@$WORKSPACE_PYTHON"
    PYTHON_BIN=$(command -v "python$WORKSPACE_PYTHON")
fi
echo "==> Using $PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"

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
    # Check version of existing venv. Matched against WORKSPACE_PYTHON exactly rather than a
    # ">= 3.12" range: the pin has an upper bound too (see above), so a venv on a NEWER
    # interpreter is just as wrong as one on an older, and a range check would silently keep it.
    VENV_PYTHON_VERSION=$("$VENV_DIR/bin/python" --version 2>/dev/null | cut -d' ' -f2 || echo "0.0.0")
    if [[ ! "$VENV_PYTHON_VERSION" == "$WORKSPACE_PYTHON".* ]]; then
         echo "==> Existing virtual environment is $VENV_PYTHON_VERSION, not $WORKSPACE_PYTHON. Recreating..."
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
# `uv venv` does NOT seed pip (unlike `python -m venv`), so a freshly uv-created venv has no
# `pip` on PATH at all and this step used to die with "pip: command not found" — hit for real
# while moving the workspace to 3.13. Seed it explicitly rather than assuming.
if ! "$VENV_DIR/bin/python" -m pip --version &> /dev/null; then
    echo "==> Seeding pip (uv venv does not create one)..."
    "$VENV_DIR/bin/python" -m ensurepip --upgrade
fi
# torch 2.11.0 pins `setuptools<82`; unconstrained upgrades pull 82.0.1
# and break the next torch install. Cap the upgrade until torch lifts it.
"$VENV_DIR/bin/python" -m pip install --upgrade pip "setuptools<82" wheel
echo "==> Installing uv for fast dependency resolution..."
"$VENV_DIR/bin/python" -m pip install uv

# 3. Install Aisland Manager in editable mode
echo "==> Installing aisland manager..."
uv pip install -e "$WORKSPACE_ROOT/aisland"

# 4. Hand off to 'aisland setup' for full project provisioning
echo "==> Handing off to 'aisland setup' for full project provisioning..."
cd "$WORKSPACE_ROOT"
# We call it from the root so Path.glob("**/*.toml") works correctly.
#
# `-y` REUSES the activation decisions already recorded in .git/config instead of re-asking
# about every optional submodule. Without it this script cannot finish anywhere there is no
# TTY — a background shell, CI, or an agent — because the prompt blocks forever on stdin
# (hit for real: `Activate 'raidar-ultralytics'? [y/N]:` with no terminal to answer it).
# A genuinely NEW optional module still prompts even under `-y`, which is deliberate: a
# machine must consciously opt into a private project before cloning its remote.
"$VENV_DIR/bin/aisland" setup -y

# 5. Install shell completion for every workspace CLI.
# One command, owned by `aisland/aisland/completion.py`. This used to be ~70 lines of inline
# shell right here (three CLI families + a stale-block pruner), which meant a completion fix
# could only reach a machine by re-running this whole provisioning script — so it silently
# rotted. `aisland completion install` is re-runnable in a second and is unit-tested.
echo "==> Installing shell completion"
"$VENV_DIR/bin/aisland" completion install \
    || echo "    (completion install failed; non-fatal — re-run 'aisland completion install')"

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
