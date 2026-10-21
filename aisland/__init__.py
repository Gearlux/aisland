"""
Aisland Manager: Cross-platform infrastructure CLI.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the installed distribution's metadata, i.e. pyproject.toml's
    # `version` (the distribution is `aisland`). Never type the number in here as well.
    __version__ = version("aisland")
except PackageNotFoundError:  # pragma: no cover - uninstalled source checkout
    __version__ = "0.0.0.dev0"
