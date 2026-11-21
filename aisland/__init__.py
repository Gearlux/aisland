"""
aisland: one workspace of many Python projects — a shared venv, local services, generated CI and git aliases.
"""

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _version_beside_the_package() -> str:
    """``[project] version`` of the ``pyproject.toml`` beside the package, else ``0.0.0.dev0``; never raises.

    A checkout the venv has not installed yet (the first `aisland setup` after a rename, which writes workspace.toml's
    how-to before it installs) has one; no wheel carries one, so a wheel without its record (a `--bundle`) falls back.
    A function, so no name it binds becomes an attribute of the package beside its submodules."""
    try:
        with open(Path(__file__).parent.parent / "pyproject.toml", "rb") as file:
            return str(tomllib.load(file)["project"]["version"])
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError):
        return "0.0.0.dev0"


try:
    # Single source of truth: the installed distribution's metadata, i.e. pyproject.toml's
    # `version` (the distribution is `aisland`). Never type the number in here as well.
    __version__ = version("aisland")
except PackageNotFoundError:  # pragma: no cover - run in a fresh interpreter by tests/test_version.py
    __version__ = _version_beside_the_package()
