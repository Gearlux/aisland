"""Tests for the activation-driven submodule lifecycle + generated root uv.toml.

Covers `aisland setup`'s new pieces in `manager.py`:
- `write_root_uv_toml` / `_merge_uv` / `_emit_uv_toml` — the generated (gitignored) root
  uv.toml = workspace base merged with each ACTIVATED project's uv.toml.
Which projects a machine has (the `active` plugin) is `tests/test_activation.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

from aisland import uv_config

# The workspace-wide override policy (uv_config._ROOT_UV_BASE), restated so a change to it is a
# deliberate edit here too: the GUI `opencv-python` is dropped workspace-wide, and headless
# opencv is pinned to the one range every consumer can share.
_OVERRIDE = [
    "opencv-python ; sys_platform == 'never'",
    "opencv-python-headless>=4.12,<5",
]

# The conditional half (uv_config._CONDITIONAL_OVERRIDES): dropping the unified `lightning` applies
# ONLY where a project names `pytorch-lightning`, because the two conflict with each other and
# nothing else. A workspace with no `pytorch-lightning` keeps `lightning` — stripping it there
# removes a dependency its projects declare outright.
_LIGHTNING_OVERRIDE = ["lightning ; sys_platform == 'never'"]


def _pyproject(project: Path, name: str, requires: list[str]) -> None:
    body = ", ".join(f'"{r}"' for r in requires)
    (project / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\ndependencies = [{body}]\n')


# --- root uv.toml generation --------------------------------------------------
class TestWriteRootUvToml:
    def test_lightning_is_kept_where_nothing_wants_pytorch_lightning(self, tmp_path: Path) -> None:
        """Another workspace, 2026-10-20: two projects require `lightning`, nothing requires the other one."""
        project = tmp_path / "my_project"
        project.mkdir()
        _pyproject(project, "my-project", ["lightning", "torch"])
        uv_config.write_root_uv_toml(tmp_path, [{"name": "my_project", "path": project}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _OVERRIDE
        assert "lightning ; sys_platform == 'never'" not in data["override-dependencies"]

    def test_lightning_is_dropped_where_a_project_wants_pytorch_lightning(self, tmp_path: Path) -> None:
        """The workspace the override was written in: matrainer and friends declare pytorch-lightning."""
        project = tmp_path / "matrainer"
        project.mkdir()
        _pyproject(project, "matrainer", ["pytorch-lightning"])
        uv_config.write_root_uv_toml(tmp_path, [{"name": "matrainer", "path": project}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _LIGHTNING_OVERRIDE + _OVERRIDE

    def test_pytorch_lightning_behind_an_extra_still_triggers_the_override(self, tmp_path: Path) -> None:
        """matrainer declares it under [lightning]; an override that came and went with the
        selected extras would resolve differently run to run."""
        project = tmp_path / "matrainer"
        project.mkdir()
        (project / "pyproject.toml").write_text(
            '[project]\nname = "matrainer"\nversion = "0.1.0"\ndependencies = []\n\n'
            "[project.optional-dependencies]\n"
            'lightning = ["pytorch-lightning", "torchmetrics"]\n'
        )
        uv_config.write_root_uv_toml(tmp_path, [{"name": "matrainer", "path": project}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _LIGHTNING_OVERRIDE + _OVERRIDE

    def test_the_distribution_name_is_normalised(self, tmp_path: Path) -> None:
        """`pytorch_lightning` and `pytorch-lightning` are the same distribution (PEP 503)."""
        project = tmp_path / "p"
        project.mkdir()
        _pyproject(project, "p", ["pytorch_lightning"])
        uv_config.write_root_uv_toml(tmp_path, [{"name": "p", "path": project}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _LIGHTNING_OVERRIDE + _OVERRIDE

    def test_merges_base_with_activated_project(self, tmp_path: Path) -> None:
        app = tmp_path / "app"
        app.mkdir()
        (app / "uv.toml").write_text('extra-index-url = ["https://idx/simple"]\n')
        uv_config.write_root_uv_toml(tmp_path, [{"name": "app", "path": app}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["override-dependencies"] == _OVERRIDE
        assert data["extra-index-url"] == ["https://idx/simple"]

    def test_carries_a_project_constraint_through_to_the_root(self, tmp_path: Path) -> None:
        # A project that depends on a package whose own metadata names a direct URL must be able to
        # constrain that URL (uv refuses a transitive URL otherwise). The merge is key-agnostic, so
        # `constraint-dependencies` reaches the root exactly like `extra-index-url` — and only when
        # the project is activated.
        pin = "pkg @ https://idx/pkg-0.0.0%2Balpha.1-py3-none-any.whl"
        app = tmp_path / "app"
        app.mkdir()
        (app / "uv.toml").write_text(f'constraint-dependencies = ["{pin}"]\n')
        uv_config.write_root_uv_toml(tmp_path, [{"name": "app", "path": app}])
        assert tomllib.loads((tmp_path / "uv.toml").read_text())["constraint-dependencies"] == [pin]
        uv_config.write_root_uv_toml(tmp_path, [])
        assert "constraint-dependencies" not in tomllib.loads((tmp_path / "uv.toml").read_text())

    def test_base_only_when_no_uv_toml(self, tmp_path: Path) -> None:
        ordinary = tmp_path / "confluid"
        ordinary.mkdir()
        uv_config.write_root_uv_toml(tmp_path, [{"name": "confluid", "path": ordinary}])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert "extra-index-url" not in data  # no private index leaks in
        assert data["override-dependencies"] == _OVERRIDE

    def test_empty_activated_is_base_only(self, tmp_path: Path) -> None:
        uv_config.write_root_uv_toml(tmp_path, [])
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data == {"override-dependencies": _OVERRIDE}

    def test_unions_indexes_across_projects(self, tmp_path: Path) -> None:
        for name, url in (("a", "https://x/simple"), ("b", "https://x/simple")):  # duplicate url
            p = tmp_path / name
            p.mkdir()
            (p / "uv.toml").write_text(f'extra-index-url = ["{url}"]\n')
        (tmp_path / "b" / "uv.toml").write_text('extra-index-url = ["https://x/simple", "https://y/simple"]\n')
        uv_config.write_root_uv_toml(
            tmp_path, [{"name": "a", "path": tmp_path / "a"}, {"name": "b", "path": tmp_path / "b"}]
        )
        data = tomllib.loads((tmp_path / "uv.toml").read_text())
        assert data["extra-index-url"] == ["https://x/simple", "https://y/simple"]  # deduped

    def test_output_is_valid_toml_with_header(self, tmp_path: Path) -> None:
        uv_config.write_root_uv_toml(tmp_path, [])
        raw = (tmp_path / "uv.toml").read_text()
        assert raw.startswith("# ")  # provenance header
        tomllib.loads(raw)  # parses

    def test_regenerates_idempotently(self, tmp_path: Path) -> None:
        app = tmp_path / "app"
        app.mkdir()
        (app / "uv.toml").write_text('extra-index-url = ["https://idx/simple"]\n')
        uv_config.write_root_uv_toml(tmp_path, [{"name": "app", "path": app}])
        assert "idx" in (tmp_path / "uv.toml").read_text()
        uv_config.write_root_uv_toml(tmp_path, [])  # app deactivated on the second run
        assert "idx" not in (tmp_path / "uv.toml").read_text()


def test_merge_uv_unions_lists_and_last_wins_scalars() -> None:
    into: dict[str, Any] = {"extra-index-url": ["a"]}
    uv_config._merge_uv(into, {"extra-index-url": ["a", "b"], "index-url": "u"})
    assert into == {"extra-index-url": ["a", "b"], "index-url": "u"}


def test_emit_uv_toml_round_trips_supported_types() -> None:
    emitted = uv_config._emit_uv_toml({"s": "x", "b": True, "n": 3, "lst": ["a", "b"]})
    assert tomllib.loads(emitted) == {"s": "x", "b": True, "n": 3, "lst": ["a", "b"]}


# --- a project's wheels folder ------------------------------------------------
def _wheels_folder(project: Path) -> Path:
    """The folder `aisland wheels build` writes: wheels beside their `wheels.yaml`."""
    folder = project / "wheels"
    folder.mkdir(parents=True)
    (folder / "wheels.yaml").write_text(f'project: "{project.name}"\nwheels: []\nnot_built: []\n')
    return folder


def _installable_wheel(folder: Path, name: str, version: str) -> None:
    """A minimal wheel uv installs: one module, METADATA, WHEEL and a RECORD naming every member."""
    info = f"{name}-{version}.dist-info"
    members = {
        f"{name}/__init__.py": "",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = "".join(f"{member},,\n" for member in [*members, f"{info}/RECORD"])
    with zipfile.ZipFile(folder / f"{name}-{version}-py3-none-any.whl", "w") as archive:
        for member, text in {**members, f"{info}/RECORD": record}.items():
            archive.writestr(member, text)


class TestWheelsFolders:
    def test_an_activated_projects_wheels_folder_is_a_find_links_path_from_the_root(self, tmp_path: Path) -> None:
        bridge = tmp_path / "bridge"
        _wheels_folder(bridge)
        uv_config.write_root_uv_toml(tmp_path, [{"name": "bridge", "path": bridge}])
        assert tomllib.loads((tmp_path / "uv.toml").read_text())["find-links"] == ["bridge/wheels"]

    def test_a_folder_without_wheels_yaml_is_no_wheels_folder(self, tmp_path: Path) -> None:
        (tmp_path / "bridge" / "wheels").mkdir(parents=True)
        uv_config.write_root_uv_toml(tmp_path, [{"name": "bridge", "path": tmp_path / "bridge"}])
        assert "find-links" not in tomllib.loads((tmp_path / "uv.toml").read_text())

    def test_without_wheels_folders_the_file_is_what_it_was(self, tmp_path: Path) -> None:
        (tmp_path / "plain").mkdir()
        uv_config.write_root_uv_toml(tmp_path, [{"name": "plain", "path": tmp_path / "plain"}])
        assert tomllib.loads((tmp_path / "uv.toml").read_text()) == {"override-dependencies": _OVERRIDE}

    def test_every_wheels_folder_is_listed_in_install_order_and_a_deactivated_one_goes(self, tmp_path: Path) -> None:
        for name in ("tools", "bridge"):
            _wheels_folder(tmp_path / name)
        both = [{"name": "tools", "path": tmp_path / "tools"}, {"name": "bridge", "path": tmp_path / "bridge"}]
        uv_config.write_root_uv_toml(tmp_path, both)
        assert tomllib.loads((tmp_path / "uv.toml").read_text())["find-links"] == ["tools/wheels", "bridge/wheels"]
        uv_config.write_root_uv_toml(tmp_path, both[1:])
        assert tomllib.loads((tmp_path / "uv.toml").read_text())["find-links"] == ["bridge/wheels"]

    def test_uv_run_in_the_workspace_installs_a_package_only_the_wheels_folder_has(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real uv, offline and without this machine's own uv settings, reads the root file from the workspace and
        from a folder below it."""
        bridge = tmp_path / "bridge"
        _installable_wheel(_wheels_folder(bridge), "onlyhere", "0.3.0")
        uv_config.write_root_uv_toml(tmp_path, [{"name": "bridge", "path": bridge}])
        uv = str(Path(sys.executable).with_name("uv"))
        env = {
            **os.environ,
            "UV_OFFLINE": "1",
            "UV_CACHE_DIR": str(tmp_path / "cache"),
            "XDG_CONFIG_HOME": str(tmp_path),
        }
        subprocess.run([uv, "venv", "-q", "--python", sys.executable, str(tmp_path / "venv")], check=True, env=env)
        for where in (tmp_path, bridge):
            result = subprocess.run(
                [uv, "pip", "install", "--python", str(tmp_path / "venv"), "--no-index", "--reinstall", "onlyhere"],
                cwd=where,
                env=env,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr
            assert " onlyhere==0.3.0" in result.stderr  # + the first time, ~ (reinstalled) the second
