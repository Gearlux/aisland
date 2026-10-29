"""Where aisland keeps its state: the folders under :func:`aisland.locations.state_dir` — ``bin/``, ``data/``
and ``tools/`` — and the package's own folder."""

from aisland import locations

# Path Configuration (relative to the file itself)
SERVICES_DIR = locations.PACKAGE_DIR


# The state folder: the aisland checkout (aisland/aisland/paths.py -> aisland/), or `<workspace>/.aisland` when
# aisland runs from a wheel (aisland.locations).
BASE_DIR = locations.state_dir()


BIN_DIR = BASE_DIR / "bin"


DATA_DIR = BASE_DIR / "data"


TOOLS_DIR = BASE_DIR / "tools"
