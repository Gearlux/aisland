"""`aisland setup` makes a `git pull` typed in a project merge: `pull.rebase false` in each project's own config."""

import subprocess
from pathlib import Path

from aisland import machine_config
from aisland.setup import git_settings
from tests.records import project_metadata


def test_each_project_gets_pull_rebase_false_in_its_own_config(tmp_path: Path) -> None:
    folders = [tmp_path / "lib", tmp_path / "app"]
    for folder in folders:
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
    git_settings.merge_on_pull([project_metadata(folder.name, folder) for folder in folders])
    assert [machine_config.get(folder, "pull.rebase") for folder in folders] == ["false", "false"]
