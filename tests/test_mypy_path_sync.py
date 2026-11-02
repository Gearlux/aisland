"""Tests for ``editor_files.sync_mypy_path`` — the append-only mypy_path updater.

The root ``mypy.ini`` is committed, hand-maintained policy; ``aisland setup``
only guarantees the ``mypy_path`` line covers every discovered project. The
invariants pinned here: append-only (never remove — inactive submodules stay
listed so the committed file is identical across machines), preserve
hand-added nested entries and entry order, and touch the file only when
something is actually missing.
"""

from __future__ import annotations

from pathlib import Path

from aisland.editor_files import sync_mypy_path
from aisland.projects import ProjectMetadata
from tests.records import project_metadata

_INI_TEMPLATE = """\
[mypy]
python_version = 3.12
mypy_path = loggair:confluid:confluid/confluid:aisland:aisland/aisland

[mypy-yaml.*]
ignore_missing_imports = True
"""


def _projects(root: Path, *names: str) -> list[ProjectMetadata]:
    return [project_metadata(n, root / n) for n in names]


def test_appends_missing_projects(tmp_path: Path) -> None:
    ini = tmp_path / "mypy.ini"
    ini.write_text(_INI_TEMPLATE)

    sync_mypy_path(tmp_path, _projects(tmp_path, "loggair", "recordstream", "chainwind"))

    line = next(ln for ln in ini.read_text().splitlines() if ln.startswith("mypy_path"))
    # Existing entries keep their order (incl. nested hand-added ones); the
    # missing projects append at the end, sorted.
    assert line == "mypy_path = loggair:confluid:confluid/confluid:aisland:aisland/aisland:chainwind:recordstream"


def test_never_removes_entries(tmp_path: Path) -> None:
    """Inactive submodules (listed but not discovered) must survive the sync."""
    ini = tmp_path / "mypy.ini"
    ini.write_text(_INI_TEMPLATE)

    sync_mypy_path(tmp_path, _projects(tmp_path, "loggair"))

    line = next(ln for ln in ini.read_text().splitlines() if ln.startswith("mypy_path"))
    assert "confluid/confluid" in line
    assert "aisland/aisland" in line


def test_noop_when_everything_known(tmp_path: Path) -> None:
    ini = tmp_path / "mypy.ini"
    ini.write_text(_INI_TEMPLATE)
    before = ini.read_text()

    sync_mypy_path(tmp_path, _projects(tmp_path, "loggair", "confluid", "aisland"))

    assert ini.read_text() == before  # byte-identical — no gratuitous rewrite


def test_preserves_other_sections(tmp_path: Path) -> None:
    ini = tmp_path / "mypy.ini"
    ini.write_text(_INI_TEMPLATE)

    sync_mypy_path(tmp_path, _projects(tmp_path, "purifai"))

    text = ini.read_text()
    assert "[mypy-yaml.*]" in text
    assert "python_version = 3.12" in text


def test_preserves_comma_separator(tmp_path: Path) -> None:
    ini = tmp_path / "mypy.ini"
    ini.write_text("[mypy]\nmypy_path = loggair, confluid\n")

    sync_mypy_path(tmp_path, _projects(tmp_path, "recordstream"))

    line = next(ln for ln in ini.read_text().splitlines() if ln.startswith("mypy_path"))
    assert line == "mypy_path = loggair,confluid,recordstream"


def test_missing_ini_is_skipped(tmp_path: Path) -> None:
    # Must not raise or create the file — mypy.ini is committed, not generated.
    sync_mypy_path(tmp_path, _projects(tmp_path, "loggair"))
    assert not (tmp_path / "mypy.ini").exists()


def test_missing_mypy_path_line_is_skipped(tmp_path: Path) -> None:
    ini = tmp_path / "mypy.ini"
    ini.write_text("[mypy]\npython_version = 3.12\n")
    before = ini.read_text()

    sync_mypy_path(tmp_path, _projects(tmp_path, "loggair"))

    assert ini.read_text() == before
