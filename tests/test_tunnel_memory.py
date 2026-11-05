"""`aisland tunnel` — the workspaces it remembers, and the completion they feed.

Nobody remembers a tunnel name or re-pastes a browser-IDE address, so the first open of an address
teaches aisland the workspace and every later one is a ``<TAB>``. What must hold, and why:

- the address is parsed where it carries the two facts: the ``folder`` query parameter is the folder
  to open, the last path segment names the workspace;
- re-opening a workspace MOVES it to the front rather than adding a second entry — a list that grows
  a duplicate per open is a list nobody can complete from;
- every reader treats a missing, unreadable or malformed file as "nothing remembered". This file
  backs a completion callback: a callback that raises breaks the user's TAB in every shell, which is
  a far worse failure than forgetting a name;
- the same goes for running outside a workspace, where completion is exactly as likely to be invoked.

No test here touches the network or opens a real editor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from aisland import locations
from aisland.cli import main
from aisland.cli import tunnel as tunnel_cli
from aisland.tunnel import host as tunnel
from aisland.tunnel import state as tunnel_state
from aisland.tunnel import store as tunnel_store

#: An address whose path segment IS the tunnel name — nothing to disambiguate.
URL = "https://example.invalid/sandbox-4821/?folder=/mnt/workspace"
#: The same workspace behind an ingress that serves it under an environment suffix.
URL_PROD = "https://example.invalid/sandbox-4821-prod/?folder=/mnt/workspace"
#: What the browser IDE's address carries when a .code-workspace file is open, not a folder.
URL_WORKSPACE_FILE = "https://example.invalid/sandbox-4821/?workspace=/mnt/workspace/repo/dev.code-workspace"


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the tunnel's state at a scratch folder, so no test touches this machine's own."""
    monkeypatch.setenv(tunnel_state.TUNNEL_HOME_ENV, str(tmp_path / "tunnel"))
    return tmp_path / "tunnel"


@pytest.fixture
def editor() -> Any:
    """Stand in for the local ``code`` command, recording what it was asked to open."""
    calls: List[List[str]] = []

    def run(command: List[str], **kwargs: Any) -> Any:
        calls.append(command)
        return None

    with (
        patch.object(tunnel.shutil, "which", return_value="/usr/local/bin/code"),
        patch.object(tunnel.subprocess, "run", run),
    ):
        yield calls


# --- 11, 13. opening ---------------------------------------------------------------------------


def test_an_address_teaches_the_name_and_the_folder(editor: List[List[str]]) -> None:
    payload = tunnel.open_editor(URL)

    assert payload["name"] == "sandbox-4821"
    assert payload["folder"] == "/mnt/workspace"
    assert editor == [["/usr/local/bin/code", "--remote", "tunnel+sandbox-4821", "/mnt/workspace"]]

    remembered = tunnel_store.load_store()
    assert [(entry.name, entry.folder, entry.url) for entry in remembered] == [("sandbox-4821", "/mnt/workspace", URL)]


def test_a_remembered_name_needs_no_address(editor: List[List[str]]) -> None:
    tunnel.open_editor(URL)
    editor.clear()

    payload = tunnel.open_editor("sandbox-4821")

    assert payload["folder"] == "/mnt/workspace"
    assert editor[0][-1] == "/mnt/workspace"


def test_an_address_that_names_no_workspace_is_refused(editor: List[List[str]]) -> None:
    result = CliRunner().invoke(main, ["tunnel", "open", "https://example.invalid/"])
    assert result.exit_code != 0
    assert "names no workspace" in result.output


def test_open_with_no_argument_takes_the_most_recent(editor: List[List[str]]) -> None:
    tunnel_store.remember("older", folder="/one")
    tunnel_store.remember("newer", folder="/two")
    editor.clear()

    payload = tunnel.open_editor()

    assert payload["name"] == "newer"
    assert editor[0][-1] == "/two"


def test_open_with_nothing_remembered_says_how_to_start(editor: List[List[str]]) -> None:
    result = CliRunner().invoke(main, ["tunnel", "open"])
    assert result.exit_code != 0
    assert "No workspace remembered yet" in result.output
    assert editor == []


# --- 12. the store ----------------------------------------------------------------------------


def test_the_store_round_trips_newest_first_and_deduplicates_by_name(state: Path) -> None:
    tunnel_store.remember("one", folder="/one")
    tunnel_store.remember("two", folder="/two")
    tunnel_store.remember("one", folder="/one-again")

    names = [entry.name for entry in tunnel_store.load_store()]
    assert names == ["one", "two"]
    assert tunnel_store.load_store()[0].folder == "/one-again"

    payload = json.loads(tunnel_store.store_path().read_text())
    assert payload["version"] == tunnel_store.STORE_VERSION
    assert [entry["name"] for entry in payload["entries"]] == ["one", "two"]


def test_an_earlier_url_survives_an_open_that_does_not_carry_one(state: Path) -> None:
    tunnel_store.remember("box", url=URL, folder="/mnt/workspace")
    tunnel_store.remember("box")
    assert tunnel_store.load_store()[0].url == URL


def test_the_store_is_capped_at_its_limit(state: Path) -> None:
    for index in range(tunnel_store.STORE_LIMIT + 5):
        tunnel_store.remember(f"box-{index:02d}")

    remembered = tunnel_store.load_store()
    assert len(remembered) == tunnel_store.STORE_LIMIT
    assert remembered[0].name == f"box-{tunnel_store.STORE_LIMIT + 4:02d}"


# --- 14, 15, 16. completion -------------------------------------------------------------------


def test_completion_offers_every_name_and_filters_by_prefix(state: Path) -> None:
    tunnel_store.remember("sandbox-48", folder="/mnt/workspace")
    tunnel_store.remember("rfuav-train", folder="/data")

    everything = tunnel_cli.complete_workspace(None, None, "")
    assert [item.value for item in everything] == ["rfuav-train", "sandbox-48"]

    filtered = tunnel_cli.complete_workspace(None, None, "san")
    assert [item.value for item in filtered] == ["sandbox-48"]
    assert "/mnt/workspace" in str(filtered[0].help)
    assert "just now" in str(filtered[0].help)


def test_completion_answers_nothing_rather_than_raising(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    assert tunnel_cli.complete_workspace(None, None, "") == []  # no file at all

    tunnel_store.save_store([])
    assert tunnel_cli.complete_workspace(None, None, "") == []  # an empty store

    monkeypatch.setattr(locations, "workspace_found", lambda: False)
    assert tunnel_cli.complete_workspace(None, None, "") == []  # outside any workspace


def test_a_malformed_store_reads_as_empty_everywhere(state: Path, editor: List[List[str]]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    tunnel_store.store_path().write_text("{not json at all")

    assert tunnel_store.load_store() == []
    assert tunnel_cli.complete_workspace(None, None, "") == []

    listing = CliRunner().invoke(main, ["tunnel", "list"])
    assert listing.exit_code == 0
    assert "No workspace remembered yet" in listing.output

    opening = CliRunner().invoke(main, ["tunnel", "open"])
    assert opening.exit_code != 0
    assert "No workspace remembered yet" in opening.output


# --- 17, 18. clearing -------------------------------------------------------------------------


def test_clear_removes_exactly_the_named_workspace(state: Path) -> None:
    tunnel_store.remember("keep", folder="/keep")
    tunnel_store.remember("drop", folder="/drop")

    result = CliRunner().invoke(main, ["tunnel", "clear", "drop"])

    assert result.exit_code == 0
    assert "Cleared 'drop'" in result.output
    assert [entry.name for entry in tunnel_store.load_store()] == ["keep"]


def test_clear_accepts_the_address_instead_of_the_name(state: Path) -> None:
    tunnel_store.remember("sandbox-4821", url=URL, folder="/mnt/workspace")

    cleared, unknown = tunnel_store.forget([URL])

    assert (cleared, unknown) == ([URL], [])
    assert tunnel_store.load_store() == []


def test_clearing_something_unremembered_is_reported(state: Path) -> None:
    tunnel_store.remember("keep")

    result = CliRunner().invoke(main, ["tunnel", "clear", "ghost"])

    assert result.exit_code != 0
    assert "Not remembered: 'ghost'" in result.output
    assert [entry.name for entry in tunnel_store.load_store()] == ["keep"]


def test_clear_without_a_target_says_what_to_pass(state: Path) -> None:
    result = CliRunner().invoke(main, ["tunnel", "clear"])
    assert result.exit_code != 0
    assert "--all" in result.output


def test_clear_all_asks_first_and_keeps_everything_on_no(state: Path) -> None:
    tunnel_store.remember("one")
    tunnel_store.remember("two")

    result = CliRunner().invoke(main, ["tunnel", "clear", "--all"], input="n\n")

    assert result.exit_code == 0
    assert "Clear all 2 remembered workspaces?" in result.output
    assert "Kept." in result.output
    assert len(tunnel_store.load_store()) == 2


def test_clear_all_with_yes_forgets_everything(state: Path) -> None:
    tunnel_store.remember("one")
    tunnel_store.remember("two")

    result = CliRunner().invoke(main, ["tunnel", "clear", "--all", "--yes"])

    assert result.exit_code == 0
    assert "Cleared 2 workspaces." in result.output
    assert tunnel_store.load_store() == []

    listing = CliRunner().invoke(main, ["tunnel", "list", "--json"])
    assert json.loads(listing.output) == []


def test_clear_all_with_nothing_remembered_says_so(state: Path) -> None:
    result = CliRunner().invoke(main, ["tunnel", "clear", "--all"])
    assert result.exit_code == 0
    assert "No workspace remembered yet" in result.output


# --- the rendered table -----------------------------------------------------------------------


def test_the_listing_shows_folder_age_and_address(state: Path) -> None:
    tunnel_store.remember("sandbox-48", url=URL, folder="/mnt/workspace")

    result = CliRunner().invoke(main, ["tunnel", "list"])

    assert result.exit_code == 0
    assert "NAME" in result.output and "LAST USED" in result.output
    assert "sandbox-48" in result.output
    assert "/mnt/workspace" in result.output
    assert URL in result.output


# --- a .code-workspace file in the address ----------------------------------------------------


def test_the_address_of_an_open_workspace_file_opens_that_file(editor: List[List[str]]) -> None:
    payload = tunnel.open_editor(URL_WORKSPACE_FILE)

    assert payload["folder"] == "/mnt/workspace/repo/dev.code-workspace"
    assert editor[0][-1] == "/mnt/workspace/repo/dev.code-workspace"
    assert tunnel_store.load_store()[0].folder == "/mnt/workspace/repo/dev.code-workspace"


def test_a_workspace_file_wins_over_a_folder_in_the_same_address(editor: List[List[str]]) -> None:
    both = (
        "https://example.invalid/sandbox-4821/?folder=/mnt/workspace&workspace=/mnt/workspace/repo/dev.code-workspace"
    )

    assert tunnel_store.parse_workspace_url(both).folder == "/mnt/workspace/repo/dev.code-workspace"


def test_the_listing_calls_the_column_what_it_opens(state: Path) -> None:
    tunnel_store.remember("sandbox-4821", url=URL_WORKSPACE_FILE, folder="/mnt/workspace/repo/dev.code-workspace")

    result = CliRunner().invoke(main, ["tunnel", "list"])

    assert "OPENS" in result.output
    assert "dev.code-workspace" in result.output


# --- the name an ingress suffix makes ambiguous -----------------------------------------------


def test_an_explicit_name_wins_over_the_one_in_the_address(editor: List[List[str]]) -> None:
    payload = tunnel.open_editor(URL_PROD, name="sandbox-4821")

    assert payload["name"] == "sandbox-4821"
    assert editor[0][2] == "tunnel+sandbox-4821"
    assert tunnel_store.load_store()[0].url == URL_PROD


def test_a_remembered_stripped_name_is_used_without_asking(editor: List[List[str]]) -> None:
    tunnel_store.remember("sandbox-4821", folder="/mnt/workspace")
    editor.clear()

    result = CliRunner().invoke(main, ["tunnel", "open", URL_PROD])

    assert result.exit_code == 0, result.output
    assert "Tunnel name" not in result.output
    assert editor[0][2] == "tunnel+sandbox-4821"


def test_a_remembered_suffixed_name_stays_as_the_address_spells_it(editor: List[List[str]]) -> None:
    tunnel_store.remember("sandbox-4821-prod", folder="/mnt/workspace")
    editor.clear()

    result = CliRunner().invoke(main, ["tunnel", "open", URL_PROD])

    assert result.exit_code == 0, result.output
    assert "Tunnel name" not in result.output
    assert editor[0][2] == "tunnel+sandbox-4821-prod"


def test_an_ambiguous_address_asks_and_offers_the_machines_own_form(editor: List[List[str]]) -> None:
    result = CliRunner().invoke(main, ["tunnel", "open", URL_PROD], input="\n")

    assert result.exit_code == 0, result.output
    assert "The address names 'sandbox-4821-prod'" in result.output
    assert "sandbox-4821" in result.output
    assert editor[0][2] == "tunnel+sandbox-4821"
    assert [entry.name for entry in tunnel_store.load_store()] == ["sandbox-4821"]


def test_the_answer_is_remembered_so_it_is_asked_once(editor: List[List[str]]) -> None:
    CliRunner().invoke(main, ["tunnel", "open", URL_PROD], input="\n")
    editor.clear()

    again = CliRunner().invoke(main, ["tunnel", "open", URL_PROD])

    assert again.exit_code == 0, again.output
    assert "Tunnel name" not in again.output
    assert editor[0][2] == "tunnel+sandbox-4821"


def test_machine_readable_output_never_asks(editor: List[List[str]]) -> None:
    result = CliRunner().invoke(main, ["tunnel", "open", URL_PROD, "--json"])

    assert result.exit_code == 0, result.output
    assert "Tunnel name" not in result.output
    assert json.loads(result.output)["name"] == "sandbox-4821"


@pytest.mark.parametrize(
    "name, stripped",
    [
        ("sandbox-4821-prod", "sandbox-4821"),
        ("sandbox-4821-dev", "sandbox-4821"),
        ("sandbox-4821-staging", "sandbox-4821"),
        ("sandbox-4821", "sandbox-4821"),
        ("prod", "prod"),
    ],
)
def test_only_a_trailing_environment_suffix_is_stripped(name: str, stripped: str) -> None:
    assert tunnel_store.strip_env_suffix(name) == stripped


# --- completion after the first open ----------------------------------------------------------


def test_a_prefix_completes_the_workspace_after_the_first_open(editor: List[List[str]]) -> None:
    tunnel.open_editor(URL)

    offered = tunnel_cli.complete_workspace(None, None, "sand")

    assert [item.value for item in offered] == ["sandbox-4821"]
    assert "/mnt/workspace" in str(offered[0].help)
