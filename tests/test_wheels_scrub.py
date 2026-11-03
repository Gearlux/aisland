"""A target not on GitHub: its wheels' METADATA scrubbed (`wheels_scrub`), its folder written without commits."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from aisland import wheels as w
from aisland import wheels_files
from aisland import wheels_install as wi
from aisland.errors import AislandError
from aisland.wheels_scrub import scrub_metadata, scrub_wheel
from tests.test_wheels import _commit, _project, _pypi, _workspace

_NAMES = ("acme", "acmeorg")

_METADATA = """\
Metadata-Version: 2.4
Name: models
Version: 0.1.0
Summary: Models for the app.
Author: Acme
Author-email: Acme <info@acmeorg.example>
Maintainer: Pat Example
License: MIT
        with a second line
Project-URL: Homepage, https://github.com/acmeorg/models
Project-URL: Documentation, https://docs.example.com/models
Home-page: https://GitHub.com/acmeorg/models
Requires-Python: >=3.12
Requires-Dist: lib @ git+https://github.com/third/lib.git@v1
Description-Content-Type: text/markdown
Provides-Extra: dev

# models

See https://github.com/acmeorg/models for the source.
"""


class TestScrubMetadata:
    """What a wheel's METADATA keeps: everything but the description, GitHub URLs and authors naming the workspace."""

    def test_the_description_github_urls_and_authors_naming_the_workspace_go(self) -> None:
        assert scrub_metadata(_METADATA, _NAMES) == (
            "Metadata-Version: 2.4\n"
            "Name: models\n"
            "Version: 0.1.0\n"
            "Summary: Models for the app.\n"
            "Maintainer: Pat Example\n"
            "License: MIT\n"
            "        with a second line\n"
            "Project-URL: Documentation, https://docs.example.com/models\n"
            "Requires-Python: >=3.12\n"
            "Requires-Dist: lib @ git+https://github.com/third/lib.git@v1\n"
            "Provides-Extra: dev\n"
        )

    def test_an_older_description_field_goes_with_its_continuation_lines(self) -> None:
        text = "Name: models\nDescription: # models\n        |\n        | See the source.\nProvides-Extra: dev\n"

        assert scrub_metadata(text, _NAMES) == "Name: models\nProvides-Extra: dev\n"

    def test_metadata_with_nothing_to_drop_comes_back_as_it_was(self) -> None:
        text = "Metadata-Version: 2.4\nName: models\nVersion: 0.1.0\nAuthor: Pat Example\n"

        assert scrub_metadata(text, _NAMES) == text


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"{name},sha256={digest},{len(data)}"


def _real_wheel(path: Path, metadata: str, files: Optional[Dict[str, str]] = None) -> Path:
    """A wheel as a build writes one: compressed members of one fixed time, and a RECORD naming each one's digest."""
    distribution, version = path.name.split("-")[:2]
    info = f"{distribution}-{version}.dist-info"
    members = {
        f"{distribution}/__init__.py": "",
        **(files or {}),
        f"{info}/METADATA": metadata,
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = [_record_line(name, text.encode()) for name, text in members.items()] + [f"{info}/RECORD,,"]
    members[f"{info}/RECORD"] = "\n".join(record) + "\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            member = zipfile.ZipInfo(name, date_time=wheels_files.ZIP_TIME)
            member.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(member, text)
    return path


def _member(wheel: Path, name: str) -> str:
    with zipfile.ZipFile(wheel) as archive:
        return archive.read(name).decode()


class TestScrubWheel:
    """The wheel rewritten in place: its METADATA scrubbed, RECORD following, every other member as it was."""

    def test_the_record_names_the_new_metadata_and_the_other_members_stay_as_built(self, tmp_path: Path) -> None:
        wheel = _real_wheel(tmp_path / "models-0.1.0-py3-none-any.whl", _METADATA, {"models/core.py": "X = 1\n"})
        with zipfile.ZipFile(wheel) as archive:
            before = {i.filename: (i.date_time, i.compress_type, archive.read(i)) for i in archive.infolist()}

        assert scrub_wheel(wheel, _NAMES) is True

        metadata = _member(wheel, "models-0.1.0.dist-info/METADATA")
        assert metadata == scrub_metadata(_METADATA, _NAMES)
        record = _member(wheel, "models-0.1.0.dist-info/RECORD").splitlines()
        assert _record_line("models-0.1.0.dist-info/METADATA", metadata.encode()) in record
        with zipfile.ZipFile(wheel) as archive:
            after = {i.filename: (i.date_time, i.compress_type, archive.read(i)) for i in archive.infolist()}
        assert list(after) == list(before)
        unchanged = [name for name in before if not name.endswith(("/METADATA", "/RECORD"))]
        assert unchanged and all(after[name] == before[name] for name in unchanged)
        assert not (tmp_path / f"{wheel.name}.part").exists()

    def test_a_scrub_is_a_function_of_the_wheel(self, tmp_path: Path) -> None:
        first = _real_wheel(tmp_path / "models-0.1.0-py3-none-any.whl", _METADATA)
        (tmp_path / "copy").mkdir()
        second = Path(shutil.copy(first, tmp_path / "copy" / first.name))

        scrub_wheel(first, _NAMES)
        scrub_wheel(second, _NAMES)

        assert first.read_bytes() == second.read_bytes()
        assert scrub_wheel(first, _NAMES) is False

    def test_a_wheel_with_nothing_to_scrub_is_left_untouched(self, tmp_path: Path) -> None:
        wheel = _real_wheel(tmp_path / "models-0.1.0-py3-none-any.whl", "Metadata-Version: 2.4\nName: models\n")
        os.utime(wheel, (1_000_000_000, 1_000_000_000))
        data = wheel.read_bytes()

        assert scrub_wheel(wheel, _NAMES) is False

        assert wheel.read_bytes() == data
        assert wheel.stat().st_mtime == 1_000_000_000

    def test_a_wheel_without_one_metadata_is_refused_naming_it(self, tmp_path: Path) -> None:
        wheel = tmp_path / "models-0.1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("models/__init__.py", "")

        with pytest.raises(AislandError, match="models-0.1.0-py3-none-any.whl: expected one dist-info METADATA"):
            scrub_wheel(wheel, _NAMES)

    def test_pip_installs_a_scrubbed_wheel(self, tmp_path: Path) -> None:
        wheel = _real_wheel(tmp_path / "models-0.1.0-py3-none-any.whl", _METADATA)
        scrub_wheel(wheel, _NAMES)
        target = tmp_path / "site"
        environment = {**os.environ, "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        offline = ["-q", "--no-deps", "--no-index", "--target", str(target)]

        subprocess.run([sys.executable, "-m", "pip", "install", *offline, str(wheel)], check=True, env=environment)

        installed = (target / "models-0.1.0.dist-info" / "METADATA").read_text()
        assert installed == scrub_metadata(_METADATA, _NAMES)


class TestARunForATargetNotOnGitHub:
    """`aisland wheels build` for a project whose remote is elsewhere: no commit in its folder, its wheels scrubbed."""

    @staticmethod
    def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_url: str) -> Tuple[Path, List[bytes]]:
        """`app` (its remote `app_url`) needs `lib` (on GitHub, owner Gearlux, not on PyPI); each build hands back a
        wheel whose METADATA names GitHub and the owner. Returns the root and the bytes of each wheel built."""
        root = _workspace(tmp_path, "app", "lib")
        (root / ".gitmodules").write_text(
            '[submodule "app"]\n\tpath = app\n\turl = ' + app_url + "\n"
            '[submodule "lib"]\n\tpath = lib\n\turl = git@github.com:Gearlux/lib.git\n'
        )
        _project(root, "app", deps=["lib"])
        _project(root, "lib")
        _commit(root / "app")
        _commit(root / "lib")
        _pypi(monkeypatch, {})
        built: List[bytes] = []

        def _build(uv: str, source: Path, out: Path, timestamp: Optional[int] = None) -> Path:
            out.mkdir(parents=True, exist_ok=True)
            metadata = (
                f"Metadata-Version: 2.4\nName: {source.name}\nVersion: 0.1.0\nAuthor-email: Gearlux <info@example.com>\n"
                f"Project-URL: Repository, https://github.com/Gearlux/{source.name}\n\n# {source.name}\n"
            )
            wheel = _real_wheel(out / f"{source.name}-0.1.0-py3-none-any.whl", metadata)
            built.append(wheel.read_bytes())
            return wheel

        monkeypatch.setattr(w, "_uv", lambda root: "uv")
        monkeypatch.setattr(w, "build_wheel", _build)
        monkeypatch.setattr(w, "install_and_import", lambda uv, target, wheels, packages, scratch: (None, {}, packages))
        return root, built

    def test_its_folder_names_no_commit_and_its_wheels_no_github(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, _ = self._workspace(tmp_path, monkeypatch, "https://git.example.com/scm/app.git")

        report = w.vendor_wheels(root, "app")

        assert report.ok
        folder = root / "app" / "wheels"
        listing = (folder / "wheels.yaml").read_text()
        readme = (folder / "README.md").read_text()
        assert "commit:" not in listing and "dev/main" not in listing
        assert "| Wheel | Project | Needed |\n| --- | --- | --- |\n| lib-0.1.0-py3-none-any.whl | lib | yes |" in readme
        assert "dev/main" not in readme and "built from its last\ncommit." in readme
        assert [(e.project, e.install) for e in wi.read_wheels(folder / "wheels.yaml")[1]] == [("lib", True)]
        metadata = _member(folder / "lib-0.1.0-py3-none-any.whl", "lib-0.1.0.dist-info/METADATA")
        assert metadata == "Metadata-Version: 2.4\nName: lib\nVersion: 0.1.0\n"

    def test_a_target_on_github_keeps_its_commits_and_its_wheels_as_built(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, built = self._workspace(tmp_path, monkeypatch, "git@github.com:Gearlux/app.git")

        w.vendor_wheels(root, "app")

        folder = root / "app" / "wheels"
        assert 'commit: "dev/main ' in (folder / "wheels.yaml").read_text()
        assert "| Wheel | Project | Branch | Commit | Needed |" in (folder / "README.md").read_text()
        assert (folder / "lib-0.1.0-py3-none-any.whl").read_bytes() == built[0]
