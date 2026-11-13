#!/bin/bash
# Bootstrap a workspace that holds aisland's checkout as one of its projects: the interpreter, the workspace's
# .venv with aisland installed editable from the checkout, then `aisland setup -y` and shell completion.
# Run it from anywhere as `bash aisland/setup.sh`; it works on the folder above the checkout. A workspace that
# installs aisland from PyPI does not need it (README → "A first workspace").

# Re-exec under bash when launched as `sh setup.sh` (dash on Debian/Ubuntu): the script uses `[[ ]]`, `source`
# and `&>`, and under dash it half-runs — `&>` forks a background job, the rest prints "not found".
# Only POSIX syntax may appear above this line.
if [ -z "$BASH_VERSION" ]; then
    exec bash "$0" "$@"
fi
set -e

# The workspace root: the folder above this script's checkout.
WORKSPACE_ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
VENV_DIR="$WORKSPACE_ROOT/.venv"

echo "==> aisland bootstrap: setting up the workspace at $WORKSPACE_ROOT"

# 1. The interpreter.
#
# The workspace's Python is one minor version, matched exactly, not a minimum: a workspace's heaviest dependencies
# publish wheels for a short list of Python versions, and a venv on a newer interpreter than they support cannot
# install them at all (no wheel, and no source build). Raise it once every package the workspace installs ships
# wheels for the next version. WORKSPACE_PYTHON=3.x in the environment picks another one for one run.
WORKSPACE_PYTHON="${WORKSPACE_PYTHON:-3.13}"

# The markers delimit the block tests/test_setup_sh.py runs in a real bash against stub tools.
# --- python-provisioning ---
# The uv release installed when this machine has no uv: a pinned version, its archive checked against the SHA-256
# the release publishes beside it. UV_VERSION=x.y.z in the environment picks another one.
UV_VERSION="${UV_VERSION:-0.12.0}"

# Install uv $UV_VERSION into ~/.local/bin: the release archive for this machine, downloaded to a scratch folder and
# unpacked only when its SHA-256 is the one the release lists (SECURITY.md: "Downloads are checked").
install_uv() {
    local triple archive release scratch expected actual
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64) triple=aarch64-apple-darwin ;;
        Darwin-x86_64) triple=x86_64-apple-darwin ;;
        Linux-x86_64) triple=x86_64-unknown-linux-gnu ;;
        Linux-aarch64 | Linux-arm64) triple=aarch64-unknown-linux-gnu ;;
        *)
            echo "==> No uv build for $(uname -s) $(uname -m): install uv (https://docs.astral.sh/uv/) and run this again." >&2
            return 1
            ;;
    esac
    archive="uv-$triple.tar.gz"
    release="https://github.com/astral-sh/uv/releases/download/$UV_VERSION"
    scratch="$(mktemp -d)"
    curl -fLsS -o "$scratch/$archive" "$release/$archive"
    curl -fLsS -o "$scratch/$archive.sha256" "$release/$archive.sha256"
    expected="$(cut -d ' ' -f 1 < "$scratch/$archive.sha256")"
    if command -v sha256sum &> /dev/null; then
        actual="$(sha256sum "$scratch/$archive" | cut -d ' ' -f 1)"
    else
        actual="$(shasum -a 256 "$scratch/$archive" | cut -d ' ' -f 1)"
    fi
    if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
        echo "==> $archive does not match the SHA-256 its release lists ($expected); nothing was installed." >&2
        rm -rf "$scratch"
        return 1
    fi
    tar -xzf "$scratch/$archive" -C "$scratch"
    mkdir -p "$HOME/.local/bin"
    mv "$scratch/uv-$triple/uv" "$scratch/uv-$triple/uvx" "$HOME/.local/bin/"
    rm -rf "$scratch"
}

if command -v "python$WORKSPACE_PYTHON" &> /dev/null; then
    PYTHON_BIN=$(command -v "python$WORKSPACE_PYTHON")
else
    # No interpreter of that version on PATH: uv installs one into its own store (~/.local/share/uv/python), with
    # no root and on every platform.
    echo "==> Python $WORKSPACE_PYTHON not found. Installing it with uv..."
    # ~/.local/bin is where uv is installed, and a fresh non-login shell often lacks it on PATH; without this a
    # second run would download uv again. For this shell only.
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv &> /dev/null; then
        echo "==> uv not found. Installing uv $UV_VERSION into ~/.local/bin..."
        install_uv
    fi
    uv python install "$WORKSPACE_PYTHON"
    # `uv python find` names the interpreter in uv's store. `--system`: without it uv prefers a `.venv` in the
    # current folder, which step 2 may delete when its version is not WORKSPACE_PYTHON.
    PYTHON_BIN=$(uv python find --system "$WORKSPACE_PYTHON")
fi
# --- /python-provisioning ---
echo "==> Using $PYTHON_BIN ($("$PYTHON_BIN" -V 2>&1))"

# 2. The virtual environment, its prompt the workspace's repository name (else its folder's).
PROMPT_NAME=$(git -C "$WORKSPACE_ROOT" remote get-url origin 2>/dev/null | sed 's/.*\/\([^/]*\)\.git/\1/')
if [ -z "$PROMPT_NAME" ]; then
    PROMPT_NAME=$(basename "$WORKSPACE_ROOT")
fi

make_venv() {
    if command -v uv &> /dev/null; then
        uv venv --prompt "$PROMPT_NAME" --python "$PYTHON_BIN" "$VENV_DIR"
        return
    fi
    "$PYTHON_BIN" -m venv --prompt "$PROMPT_NAME" "$VENV_DIR"
    # Some venv versions write the prompt in parentheses twice ("((name))"): drop the template's own pair.
    if [ -f "$VENV_DIR/bin/activate" ]; then
        sed -i "" 's/PS1="(\(.*\)) /PS1="\1 /' "$VENV_DIR/bin/activate" 2>/dev/null || \
        sed -i 's/PS1="(\(.*\)) /PS1="\1 /' "$VENV_DIR/bin/activate"
    fi
    if [ -f "$VENV_DIR/bin/activate.fish" ]; then
        sed -i "" "s/printf '%s%s%s' (set_color 4B8BBE) '(\(.*\)) ' (set_color normal)/printf '%s%s%s' (set_color 4B8BBE) '\1 ' (set_color normal)/" "$VENV_DIR/bin/activate.fish" 2>/dev/null || \
        sed -i "s/printf '%s%s%s' (set_color 4B8BBE) '(\(.*\)) ' (set_color normal)/printf '%s%s%s' (set_color 4B8BBE) '\1 ' (set_color normal)/" "$VENV_DIR/bin/activate.fish"
    fi
    if [ -f "$VENV_DIR/bin/activate.csh" ]; then
        sed -i "" 's/set env_name = "(\(.*\)) "/set env_name = "\1 "/' "$VENV_DIR/bin/activate.csh" 2>/dev/null || \
        sed -i 's/set env_name = "(\(.*\)) "/set env_name = "\1 "/' "$VENV_DIR/bin/activate.csh"
    fi
}

if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating the virtual environment ($PYTHON_BIN) with prompt: $PROMPT_NAME..."
    make_venv
else
    # Matched against WORKSPACE_PYTHON exactly: a venv on a newer interpreter is as wrong as one on an older one.
    VENV_PYTHON_VERSION=$("$VENV_DIR/bin/python" --version 2>/dev/null | cut -d' ' -f2 || echo "0.0.0")
    if [[ ! "$VENV_PYTHON_VERSION" == "$WORKSPACE_PYTHON".* ]]; then
        echo "==> The virtual environment runs Python $VENV_PYTHON_VERSION, not $WORKSPACE_PYTHON. Recreating it..."
        rm -rf "$VENV_DIR"
        make_venv
    fi
fi

# 3. pip, the build tools and uv in the venv.
source "$VENV_DIR/bin/activate"
echo "==> Upgrading the build tools..."
# `uv venv` creates no pip (`python -m venv` does): seed it.
if ! "$VENV_DIR/bin/python" -m pip --version &> /dev/null; then
    echo "==> Seeding pip..."
    "$VENV_DIR/bin/python" -m ensurepip --upgrade
fi
# setuptools stays below 82: some packages a workspace installs (torch among them) pin `setuptools<82`, and an
# unbounded upgrade would break their next install. Lift the cap once they do.
"$VENV_DIR/bin/python" -m pip install --upgrade pip "setuptools<82" wheel
echo "==> Installing uv into the venv..."
"$VENV_DIR/bin/python" -m pip install uv

# 4. aisland itself, editable from this checkout.
echo "==> Installing aisland..."
uv pip install -e "$WORKSPACE_ROOT/aisland"

# 5. `aisland setup`, from the root, for everything else.
echo "==> Handing off to 'aisland setup'..."
cd "$WORKSPACE_ROOT"
# `-y` keeps the answers this machine already gave (which optional projects it has, the selections), so the script
# finishes without a terminal — a background shell, CI, an agent. A question this machine never answered is still
# asked: a machine opts into an optional project before its repository is cloned.
"$VENV_DIR/bin/aisland" setup -y

# 6. Shell completion for every command-line tool of the workspace (`aisland completion install`, re-runnable).
echo "==> Installing shell completion"
"$VENV_DIR/bin/aisland" completion install \
    || echo "    (completion install failed; non-fatal — re-run 'aisland completion install')"

# 7. The workspace's .vscode/tasks.json links to aisland/tasks.json: the editor's task that opens Jenkins in its
# Simple Browser. `ln -sf` replaces a link already there, never a file of the person's own.
# --- tasks-link ---
TASKS_LINK="$WORKSPACE_ROOT/.vscode/tasks.json"
if [ -f "$WORKSPACE_ROOT/aisland/tasks.json" ]; then
    mkdir -p "$WORKSPACE_ROOT/.vscode"
    if [ -e "$TASKS_LINK" ] && [ ! -L "$TASKS_LINK" ]; then
        echo "==> Kept .vscode/tasks.json, a file of your own (aisland's Jenkins task is in aisland/tasks.json)"
    else
        echo "==> Linking .vscode/tasks.json → aisland/tasks.json"
        ln -sf "../aisland/tasks.json" "$TASKS_LINK"
    fi
fi
# --- /tasks-link ---

echo "==> Setup complete."
echo "==> Run 'source project.bashrc' to start working: it activates .venv, exports .env and loads completion."
