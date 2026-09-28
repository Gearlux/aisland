"""Tests for the workspace's single committed config file (aisland/config.py).

Three concerns share `config.toml` since 2026-08-01 (`sources.toml` / `frameworks.toml` /
`trackers.toml` were folded into it), so what this covers is what sharing a file costs:

- a write to one section leaves every other one intact — the property the merge is only
  acceptable because of;
- scalars are emitted before tables, or `frameworks = [...]` would silently be read as
  `sources.<last-project>.frameworks`;
- the legacy files are still READ (a checkout predating the merge must not read as "nothing
  is configured") and are deleted once anything writes;
- the commands the file's own comments name are commands the CLI actually accepts.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import click
import pytest

from aisland import config

# --------------------------------------------------------------------------- #
# round trip
# --------------------------------------------------------------------------- #


def test_missing_file_is_an_empty_document(tmp_path: Path) -> None:
    assert config.load(tmp_path) == {}


def test_every_section_round_trips(tmp_path: Path) -> None:
    config.update(tmp_path, "frameworks", ["torch", "keras"])
    config.update(tmp_path, "trackers", ["mlflow"])
    config.update(tmp_path, "sources", {"loggair": {"source": "pypi", "distribution": "loggair"}})

    assert config.load(tmp_path) == {
        "frameworks": ["torch", "keras"],
        "trackers": ["mlflow"],
        "sources": {"loggair": {"source": "pypi", "distribution": "loggair"}},
    }
    assert tomllib.loads((tmp_path / config.CONFIG_FILE).read_text()) == config.load(tmp_path)


def test_updating_one_key_preserves_the_others(tmp_path: Path) -> None:
    """The invariant the whole merge rests on: `aisland tracker set` must not touch sources."""
    config.update(tmp_path, "sources", {"confluid": {"source": "local", "ref": "dev/main"}})
    config.update(tmp_path, "frameworks", ["torch"])
    config.update(tmp_path, "trackers", ["aim"])

    data = config.load(tmp_path)
    assert data["sources"] == {"confluid": {"source": "local", "ref": "dev/main"}}
    assert data["frameworks"] == ["torch"]


def test_scalars_are_emitted_before_tables(tmp_path: Path) -> None:
    """A bare key after `[sources.x]` would bind INTO that table — a silent, total corruption."""
    config.update(tmp_path, "sources", {"loggair": {"source": "pypi"}})
    config.update(tmp_path, "frameworks", ["torch"])

    text = (tmp_path / config.CONFIG_FILE).read_text()
    assert text.index("frameworks = ") < text.index("[sources.loggair]")
    assert tomllib.loads(text)["frameworks"] == ["torch"]


def test_removing_a_key_keeps_the_file_for_the_rest(tmp_path: Path) -> None:
    config.update(tmp_path, "frameworks", ["torch"])
    config.update(tmp_path, "trackers", ["aim"])
    config.update(tmp_path, "trackers", None)

    assert config.load(tmp_path) == {"frameworks": ["torch"]}


def test_removing_the_last_section_removes_the_file(tmp_path: Path) -> None:
    """Absence of a key is what `unset` means; a header-only stub would just dirty the tree."""
    config.update(tmp_path, "frameworks", ["torch"])
    config.update(tmp_path, "frameworks", None)

    assert not (tmp_path / config.CONFIG_FILE).exists()


def test_an_empty_section_is_not_emitted(tmp_path: Path) -> None:
    config.update(tmp_path, "frameworks", ["torch"])
    config.update(tmp_path, "sources", {})

    assert "sources" not in config.load(tmp_path)


def test_an_empty_selection_is_kept_because_it_is_a_choice(tmp_path: Path) -> None:
    """`[]` means "none, deliberately" and must survive; only `None` means unchosen."""
    config.update(tmp_path, "frameworks", [])

    assert config.load(tmp_path) == {"frameworks": []}


def test_unknown_keys_survive_a_write(tmp_path: Path) -> None:
    """A hand-added section must not be collateral damage of `aisland tracker set`."""
    (tmp_path / config.CONFIG_FILE).write_text('experiment = "yes"\n\n[custom]\nk = 1\n')
    config.update(tmp_path, "frameworks", ["torch"])

    data = config.load(tmp_path)
    assert data["experiment"] == "yes" and data["custom"] == {"k": 1}


def test_a_project_name_needing_quotes_round_trips(tmp_path: Path) -> None:
    config.update(tmp_path, "sources", {"weird.name": {"source": "pypi"}})

    assert config.load(tmp_path)["sources"] == {"weird.name": {"source": "pypi"}}


def test_an_unsupported_value_is_skipped_with_a_warning(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Skipping loudly beats mis-serialising: the file must always stay parseable."""
    config.write(tmp_path, {"frameworks": ["torch"], "odd": {1, 2}})

    assert "unsupported type set" in capsys.readouterr().out
    assert config.load(tmp_path) == {"frameworks": ["torch"]}


def test_a_malformed_file_degrades_to_empty(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    (tmp_path / config.CONFIG_FILE).write_text("[sources.loggair\n")

    assert config.load(tmp_path) == {}
    assert "could not read" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# migration from the three per-concern files
# --------------------------------------------------------------------------- #


def _write_legacy(root: Path) -> None:
    (root / "frameworks.toml").write_text('frameworks = ["torch"]\n')
    (root / "trackers.toml").write_text('trackers = ["mlflow"]\n')
    (root / "sources.toml").write_text('[loggair]\nsource = "pypi"\ndistribution = "loggair"\n')


def test_legacy_files_are_still_read(tmp_path: Path) -> None:
    """The silent failure this prevents: a pre-merge checkout reading as "nothing configured",
    which for sources means every project quietly unmanaged."""
    _write_legacy(tmp_path)

    data = config.load(tmp_path)
    assert data["frameworks"] == ["torch"]
    assert data["trackers"] == ["mlflow"]
    assert data["sources"] == {"loggair": {"source": "pypi", "distribution": "loggair"}}


def test_config_toml_wins_over_a_legacy_file(tmp_path: Path) -> None:
    """Per KEY, so a migrated section can never be resurrected by a file left behind."""
    _write_legacy(tmp_path)
    (tmp_path / config.CONFIG_FILE).write_text('frameworks = ["keras"]\n')

    data = config.load(tmp_path)
    assert data["frameworks"] == ["keras"]
    assert data["trackers"] == ["mlflow"]  # still only in the legacy file


def test_migrate_folds_and_deletes(tmp_path: Path) -> None:
    _write_legacy(tmp_path)

    assert set(config.migrate(tmp_path)) == set(config.LEGACY_FILES.values())
    assert not any((tmp_path / name).exists() for name in config.LEGACY_FILES.values())
    assert config.load(tmp_path)["sources"] == {"loggair": {"source": "pypi", "distribution": "loggair"}}


def test_migrate_is_a_no_op_without_legacy_files(tmp_path: Path) -> None:
    """It runs on every `aisland setup`, so it must not rewrite (and reformat) a settled file."""
    config.update(tmp_path, "frameworks", ["torch"])
    before = (tmp_path / config.CONFIG_FILE).read_bytes()

    assert config.migrate(tmp_path) == []
    assert (tmp_path / config.CONFIG_FILE).read_bytes() == before


def test_any_write_completes_the_migration(tmp_path: Path) -> None:
    """A lingering frameworks.toml beside an authoritative config.toml could only mislead."""
    _write_legacy(tmp_path)
    config.update(tmp_path, "trackers", ["aim"])

    assert not any((tmp_path / name).exists() for name in config.LEGACY_FILES.values())
    data = config.load(tmp_path)
    assert data["trackers"] == ["aim"] and data["frameworks"] == ["torch"]


# --------------------------------------------------------------------------- #
# the file's own instructions
# --------------------------------------------------------------------------- #


_COMMAND_RE = re.compile(r"aisland ((?:[a-z][a-z-]*)(?: [a-z][a-z-]*)*)")


def test_every_command_the_file_names_is_one_the_cli_accepts(tmp_path: Path) -> None:
    """The comments are the only instruction the file carries. Resolved against the REAL command
    tree rather than string-matched, so a renamed group fails here instead of in a user's shell —
    the same rule the generated Jenkinsfiles' `To regenerate:` line is pinned by."""
    from aisland.cli import main

    config.write(tmp_path, {"frameworks": ["torch"], "trackers": ["aim"], "sources": {"loggair": {"source": "pypi"}}})
    text = (tmp_path / config.CONFIG_FILE).read_text()

    documented = {match.group(1) for match in _COMMAND_RE.finditer(text)}
    assert documented, "the file must document the commands that maintain it"
    for phrase in documented:
        node: click.Command = main
        resolved: list[str] = []
        for token in phrase.split():
            if not isinstance(node, click.Group) or token not in node.commands:
                break
            node = node.commands[token]
            resolved.append(token)
        assert resolved, f"`aisland {phrase}` names no such command"


def test_the_header_points_at_its_own_source_module(tmp_path: Path) -> None:
    config.update(tmp_path, "frameworks", ["torch"])
    text = (tmp_path / config.CONFIG_FILE).read_text()

    assert "AUTO-GENERATED" in text
    assert "aisland/aisland/config.py" in text


def test_the_header_says_uv_toml_is_not_here(tmp_path: Path) -> None:
    """uv rejects a config file carrying foreign tables, so the separation is permanent —
    a reader looking for the uv overrides must be told where they went."""
    config.update(tmp_path, "frameworks", ["torch"])

    assert "uv.toml" in (tmp_path / config.CONFIG_FILE).read_text()
