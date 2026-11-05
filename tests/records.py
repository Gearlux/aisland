"""The project records the tests hand to aisland's workspace-file writers, as :func:`aisland.workspace.projects.get_project_metadata`
returns them."""

from pathlib import Path

from aisland.workspace.projects import ProjectMetadata


def project_metadata(name: str, path: Path, *dependencies: str, has_dev: bool = False) -> ProjectMetadata:
    """The record of the project ``name`` in ``path`` requiring ``dependencies``."""
    return {"name": name, "path": path, "dependencies": list(dependencies), "has_dev": has_dev}
