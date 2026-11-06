"""`aisland workspace env-example` and `aisland workspace env-test`: the workspace's `env.complete` (every variable)
and `env.example` (the ones a person normally sets), made of each project's own two files, and the test that holds
each project's files to what its code reads.

What is pinned: a project's file is read line by line, every line commented out, a malformed one refused naming its
file and line; each workspace file lists a variable once, under the project that defines it (each library before the
projects depending on it), the others named and their own wording kept, the projects without a file named, and
carries its provenance header whose regenerate line the CLI accepts; an unchanged file is left; `--missing` prints
what `.env` does not mention; `workspace init` seeds `.env` from `env.example` (else `env.complete`) and never touches
an existing one; the generated test passes on a project whose `env.complete` lists what its code reads and fails,
naming the file and line, on one that does not, and on an `env.example` variable `env.complete` lacks.
"""

import subprocess
import sys
from pathlib import Path
from typing import Dict

import click
import click.testing
import pytest
from click.testing import CliRunner

from aisland import cli, locations
from aisland.setup import env_example

LIB = """\
# lib: where the library finds its data

# -- paths
# DATA_ROOT=/path/to/datasets   the folder every config reads its recordings from (README → Data)
# LIB_CACHE=   a cache folder; empty: none
# not for .env: COMP_LINE COMP_POINT — set by the shell while it completes
"""

APP = """\
# app: the application's switches

# APP_LEVEL=INFO   TRACE | DEBUG | INFO — the console's log level
# DATA_ROOT=/data   the datasets the app opens
"""

LIB_EXAMPLE = """\
# lib: where the library finds its data

# DATA_ROOT=/path/to/datasets   the folder every config reads its recordings from (README → Data)
"""


def _project(root: Path, name: str, complete: str = "", code: str = "", needs: str = "", example: str = "") -> Path:
    folder = root / name
    (folder / name).mkdir(parents=True)
    dependencies = f'dependencies = ["{needs}"]\n' if needs else ""
    (folder / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "0.1.0"\n{dependencies}')
    (folder / name / "__init__.py").write_text(code)
    if complete:
        (folder / env_example.COMPLETE).write_text(complete)
    if example:
        (folder / env_example.EXAMPLE).write_text(example)
    return folder


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace without `.gitmodules`: `app`, which depends on `lib` (so `lib` comes first, though `app` sorts
    first by name), and `lib`, which has an `env.example` too."""
    workspace = tmp_path / "acme"
    workspace.mkdir()
    _project(workspace, "app", APP, code='import os\nos.environ["APP_LEVEL"]\n', needs="lib")
    _project(
        workspace,
        "lib",
        LIB,
        code='import os\nos.environ.get("DATA_ROOT")\nos.getenv("LIB_CACHE")\n',
        example=LIB_EXAMPLE,
    )
    monkeypatch.setattr(locations, "workspace_root", lambda: workspace)
    return workspace


def _run(*args: str) -> click.testing.Result:
    return CliRunner().invoke(cli.main, ["workspace", *args])


def _body(path: Path) -> str:
    text = path.read_text()
    return text[text.index("\n# ===== ") + 1 :]


# ------------------------------------------------------------------ a project's file ----------------------------


def test_a_projects_file_is_read_into_its_variables_headings_and_plumbing(root: Path) -> None:
    env = env_example.read_project(root / "lib" / env_example.COMPLETE, "lib")
    assert env.title == "lib: where the library finds its data"
    assert [(e.name, e.value, e.description, e.heading) for e in env.entries] == [
        (
            "DATA_ROOT",
            "/path/to/datasets",
            "the folder every config reads its recordings from (README → Data)",
            "paths",
        ),
        ("LIB_CACHE", "", "a cache folder; empty: none", "paths"),
    ]
    reason = "set by the shell while it completes"
    assert env.not_for_env == {"COMP_LINE": reason, "COMP_POINT": reason}


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("DATA_ROOT=/data", "sets DATA_ROOT outside a comment — every line of env.complete is commented out"),
        ("export DATA_ROOT=/data", "sets DATA_ROOT outside a comment — every line of env.complete is commented out"),
        ("# DATA_ROOT=/data the folder", "two spaces part the value of DATA_ROOT from its description"),
        ("hello", "is not a comment — every line of env.complete is a comment or blank"),
    ],
)
def test_a_line_that_would_set_something_or_is_malformed_is_refused_naming_its_line(
    tmp_path: Path, line: str, message: str
) -> None:
    path = tmp_path / env_example.COMPLETE
    path.write_text(f"# lib: x\n\n{line}\n")
    with pytest.raises(click.ClickException) as raised:
        env_example.read_project(path, "lib")
    assert raised.value.message == f"{path}:3: {message}"


# ------------------------------------------------------------------ the workspace's files -------------------------


def test_both_files_are_written_and_the_projects_without_one_named(root: Path) -> None:
    _project(root, "models")  # a project with neither file
    assert _run("env-example").output == (
        "==> Wrote env.complete (3 variables from 2 projects)\n"
        "    no env.complete: models\n"
        "==> Wrote env.example (1 variable from 1 project)\n"
        "    no env.example: app, models\n"
    )


def test_a_shared_variable_is_listed_once_under_the_project_that_defines_it(root: Path) -> None:
    assert _run("env-example").exit_code == 0
    assert _body(root / env_example.COMPLETE) == (
        "# ===== lib: where the library finds its data\n"
        "\n"
        "# -- paths\n"
        "# DATA_ROOT=/path/to/datasets   the folder every config reads its recordings from (README → Data)\n"
        "#     also read by: app\n"
        "#     app: the datasets the app opens\n"
        "# LIB_CACHE=   a cache folder; empty: none\n"
        "\n"
        "# ===== app: the application's switches\n"
        "\n"
        "# APP_LEVEL=INFO   TRACE | DEBUG | INFO — the console's log level\n"
    )
    text = (root / env_example.COMPLETE).read_text()
    assert all(not line.strip() or line.startswith("#") for line in text.splitlines()), "a copy to .env sets nothing"
    assert "COMP_LINE" not in text  # plumbing stays in the project's file


def test_env_example_holds_the_projects_short_lists(root: Path) -> None:
    assert _run("env-example").exit_code == 0
    assert _body(root / env_example.EXAMPLE) == (
        "# ===== lib: where the library finds its data\n"
        "\n"
        "# DATA_ROOT=/path/to/datasets   the folder every config reads its recordings from (README → Data)\n"
        "\n"
        "# ===== without an env.example: app\n"
    )


def test_with_no_short_list_yet_env_example_says_so(root: Path) -> None:
    (root / "lib" / env_example.EXAMPLE).unlink()
    assert _run("env-example").exit_code == 0
    assert (
        (root / env_example.EXAMPLE)
        .read_text()
        .endswith("# (no project has an env.example yet)\n\n# ===== without an env.example: lib, app\n")
    )


def test_each_file_says_how_it_was_made_and_the_command_it_names_runs(root: Path) -> None:
    assert _run("env-example").exit_code == 0
    for name in (env_example.COMPLETE, env_example.EXAMPLE):
        header = (root / name).read_text().splitlines()[:6]
        assert "# Generated by: aisland workspace env-example" in header
        assert f"# Source: each project's {name} (aisland/setup/env_example.py)" in header
        regenerate = next(line for line in header if line.startswith("# To regenerate:"))
        words = regenerate.split(":", 1)[1].split()
        assert words == ["aisland", "workspace", "env-example"]
        group = cli.main.get_command(click.Context(cli.main), words[1])  # the CLI has the command the line names
        assert isinstance(group, click.Group) and group.get_command(click.Context(group), words[2]) is not None


def test_unchanged_files_are_left_and_said_to_be_up_to_date(root: Path) -> None:
    assert _run("env-example").exit_code == 0
    before = {name: (root / name).stat().st_mtime_ns for name in (env_example.COMPLETE, env_example.EXAMPLE)}
    result = _run("env-example")
    assert result.output == (
        "==> Up to date: env.complete (3 variables from 2 projects)\n"
        "==> Up to date: env.example (1 variable from 1 project)\n"
        "    no env.example: app\n"
    )
    assert {name: (root / name).stat().st_mtime_ns for name in before} == before


def test_a_malformed_project_file_stops_the_command_with_one_error_line_and_writes_neither(root: Path) -> None:
    (root / "app" / env_example.COMPLETE).write_text("# app: x\nAPP_LEVEL=INFO\n")
    result = _run("env-example")
    assert result.exit_code == 1
    assert result.output.startswith(f"Error: {root / 'app' / env_example.COMPLETE}:2: ")
    assert not (root / env_example.COMPLETE).exists() and not (root / env_example.EXAMPLE).exists()


def test_missing_lists_the_variables_dotenv_does_not_mention(root: Path) -> None:
    (root / ".env").write_text("DATA_ROOT=/Volumes/data\n# APP_LEVEL=DEBUG\n")
    result = _run("env-example", "--missing")
    assert result.exit_code == 0, result.output
    assert result.output.endswith(
        "==> Not in .env (1) — copy the lines you need:\n# -- lib\n# LIB_CACHE=   a cache folder; empty: none\n"
    )
    assert (root / ".env").read_text() == "DATA_ROOT=/Volumes/data\n# APP_LEVEL=DEBUG\n"  # never edited


def test_missing_without_a_dotenv_says_how_to_make_one(root: Path) -> None:
    assert _run("env-example", "--missing").output.endswith("==> No .env yet: cp env.example .env\n")


def test_missing_with_everything_mentioned_says_so(root: Path) -> None:
    (root / ".env").write_text("DATA_ROOT=/d\nLIB_CACHE=\nexport APP_LEVEL=INFO\n")
    assert _run("env-example", "--missing").output.endswith("==> .env mentions every variable\n")


def test_the_workspace_gitignore_ignores_both_generated_files() -> None:
    from aisland.workspace import init as workspace

    lines = workspace._GITIGNORE.splitlines()
    assert "/env.complete" in lines and "/env.example" in lines


def test_one_project_and_one_variable_are_said_in_the_singular(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "acme"
    workspace.mkdir()
    _project(workspace, "lib", "# lib: x\n\n# LIB_CACHE=   a cache\n", example="# lib: x\n\n# LIB_CACHE=   a cache\n")
    monkeypatch.setattr(locations, "workspace_root", lambda: workspace)
    assert _run("env-example").output == (
        "==> Wrote env.complete (1 variable from 1 project)\n==> Wrote env.example (1 variable from 1 project)\n"
    )


def test_an_optional_project_checked_out_by_hand_is_read_and_an_empty_folder_is_not(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "tools").mkdir()  # an empty folder: a project without a checkout
    folders = [("app", "app", False), ("lib", "lib", True), ("tools", "tools", True)]
    monkeypatch.setattr(env_example, "workspace_folders", lambda r: folders)
    result = _run("env-example")
    assert result.output.startswith("==> Wrote env.complete (3 variables from 2 projects)\n    no checkout: tools\n")
    assert "APP_LEVEL" in (root / env_example.COMPLETE).read_text()


def test_a_second_heading_gets_a_blank_line_above_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "acme"
    workspace.mkdir()
    _project(workspace, "lib", "# lib: x\n\n# -- a\n# A_ONE=1  one\n\n# -- b\n# B_TWO=2  two\n")
    monkeypatch.setattr(locations, "workspace_root", lambda: workspace)
    assert _run("env-example").exit_code == 0
    assert (
        (workspace / env_example.COMPLETE)
        .read_text()
        .endswith("# ===== lib: x\n\n# -- a\n# A_ONE=1   one\n\n# -- b\n# B_TWO=2   two\n")
    )


# ------------------------------------------------------------------ workspace init and setup ----------------------


def test_init_seeds_dotenv_from_env_example_and_keeps_an_existing_one(root: Path) -> None:
    from aisland.workspace import init as workspace

    files = workspace._starting_files(root)
    assert files[".env"] == env_example.render(env_example.collect(root, "example"))
    (root / ".env").write_text("DATA_ROOT=/mine\n")
    workspace._write_starting_files(root, ())
    assert (root / ".env").read_text() == "DATA_ROOT=/mine\n"


def test_init_without_a_short_list_seeds_from_env_complete(root: Path) -> None:
    from aisland.workspace import init as workspace

    (root / "lib" / env_example.EXAMPLE).unlink()
    assert "# APP_LEVEL=INFO" in workspace._starting_files(root)[".env"]


def test_init_without_projects_writes_the_short_template(tmp_path: Path) -> None:
    from aisland.workspace import init as workspace

    assert workspace._starting_files(tmp_path)[".env"] == workspace._ENV


def test_setup_writes_both_files_and_says_each(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_example.write_for_setup(root)
    assert capsys.readouterr().out == (
        "==> Wrote env.complete (3 variables from 2 projects)\n==> Wrote env.example (1 variable from 1 project)\n"
    )
    env_example.write_for_setup(root)
    assert capsys.readouterr().out == ""  # nothing changed, nothing said


def test_setup_says_a_malformed_project_file_and_goes_on(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / "app" / env_example.COMPLETE).write_text("APP_LEVEL=INFO\n")
    env_example.write_for_setup(root)
    assert "env.complete and env.example not written:" in capsys.readouterr().out


def test_setup_hands_the_workspace_root_to_the_writer(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    written: Dict[str, Path] = {}

    def write(at: Path) -> Dict[env_example.Kind, env_example.Written]:
        written["root"] = at
        return {}

    monkeypatch.setattr(env_example, "write", write)
    env_example.write_for_setup(root)
    assert written == {"root": root}


# ------------------------------------------------------------------ the generated test ---------------------------


def _pytest(project: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:aisland_test_stats", "tests"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_env_test_writes_each_projects_test_and_leaves_an_unchanged_one(root: Path) -> None:
    result = _run("env-test")
    assert result.exit_code == 0, result.output
    assert result.output == "==> Wrote app/tests/test_env_example.py\n==> Wrote lib/tests/test_env_example.py\n"
    text = (root / "lib" / "tests" / "test_env_example.py").read_text()
    assert "# Generated by: aisland workspace env-test lib" in text
    assert _run("env-test", "lib").output == "==> lib/tests/test_env_example.py is up to date\n"


def test_env_test_skips_a_project_without_a_file_and_refuses_an_unknown_one(root: Path) -> None:
    _project(root, "models")
    assert "==> models has no env.complete: no test written\n" in _run("env-test").output
    refused = _run("env-test", "nope")
    assert refused.exit_code == 1 and "Error: nope is no project of this workspace" in refused.output


def test_the_generated_test_passes_when_the_files_list_what_the_code_reads(root: Path) -> None:
    assert _run("env-test", "lib").exit_code == 0
    result = _pytest(root / "lib")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_generated_test_names_a_variable_env_complete_does_not_list(root: Path) -> None:
    (root / "lib" / "lib" / "extra.py").write_text('import os\n\nos.environ.get("LIB_SECRET")\n')
    (root / "lib" / "config").mkdir()
    (root / "lib" / "config" / "run.yaml").write_text("out: ${env:LIB_OUT,./runs}/x\n")
    assert _run("env-test", "lib").exit_code == 0
    result = _pytest(root / "lib")
    assert result.returncode == 1
    assert "LIB_SECRET  (lib/extra.py:3)" in result.stdout
    assert "LIB_OUT  (config/run.yaml:1)" in result.stdout


def test_the_generated_test_reads_no_yaml_comment(root: Path) -> None:
    (root / "lib" / "config").mkdir()
    (root / "lib" / "config" / "run.yaml").write_text(
        "# `$VAR` is expanded when the app loads\nout: ./runs  # $OTHER\n"
    )
    assert _run("env-test", "lib").exit_code == 0
    assert _pytest(root / "lib").returncode == 0


@pytest.mark.parametrize("name", [env_example.COMPLETE, env_example.EXAMPLE])
def test_the_generated_test_refuses_a_line_that_would_set_a_variable(root: Path, name: str) -> None:
    path = root / "lib" / name
    path.write_text(path.read_text() + "LIB_CACHE=/tmp\n")
    line = len(path.read_text().splitlines())
    assert _run("env-test", "lib").exit_code == 0
    result = _pytest(root / "lib")
    assert result.returncode == 1 and f"{name}:{line} sets a variable" in result.stdout


def test_the_generated_test_refuses_a_short_list_variable_the_complete_one_lacks(root: Path) -> None:
    (root / "lib" / env_example.EXAMPLE).write_text(LIB_EXAMPLE + "# LIB_EXTRA=1   only here\n")
    assert _run("env-test", "lib").exit_code == 0
    result = _pytest(root / "lib")
    assert result.returncode == 1 and "env.example lists variables env.complete does not: LIB_EXTRA" in result.stdout


def test_the_generated_test_ignores_tests_and_the_shells_own_names(root: Path) -> None:
    (root / "lib" / "lib" / "shell.py").write_text('import os\n\nos.environ.get("HOME"), os.environ.get("PATH")\n')
    (root / "lib" / "lib" / "done.py").write_text('import os\n\nos.environ.get("COMP_LINE")\n')
    assert _run("env-test", "lib").exit_code == 0
    (root / "lib" / "tests" / "test_x.py").write_text('import os\n\nos.environ.get("ONLY_IN_A_TEST")\n')
    assert _pytest(root / "lib").returncode == 0


def test_the_generated_test_passes_the_projects_formatters_and_linter_unchanged(tmp_path: Path) -> None:
    """The workspace's projects format at 120 columns: a generated file black rewrote would never be up to date."""
    target = tmp_path / "test_env_example.py"
    target.write_text(env_example.render_test("models"))
    for tool in (
        ["black", "--check", "-q", "-l", "120"],
        ["isort", "--check-only", "-q", "--profile", "black", "-l", "120"],
        ["flake8", "--max-line-length", "120"],
    ):
        result = subprocess.run([sys.executable, "-m", *tool, str(target)], capture_output=True, text=True)
        assert result.returncode == 0, f"{tool[0]}: {result.stdout}{result.stderr}"
