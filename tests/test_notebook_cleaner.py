"""Tests for ``aisland notebooks clean`` — the .ipynb git filter.

The cleaner is wired in as a clean filter (filter.ipynb-cleaner.clean) so it
runs on every staged notebook. These tests pin the two non-negotiable
properties of that filter:

1. **Idempotence** — running it twice produces the same bytes as running it
   once. Without this the filter would generate a permanent "modified" state
   on every status check.

2. **Cross-machine portability** — kernel display name and the local Python
   interpreter version are stripped, so the same notebook opened on a
   different machine (different venv name, different Python patch version)
   produces the same on-disk form.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict
from unittest.mock import patch

import pytest

from aisland.notebook_utils import (
    _CANONICAL_KERNELSPEC,
    clean_notebook_outputs,
    clean_notebook_stdin,
)


def _make_nb(
    *,
    is_documentation: bool = False,
    extra_metadata: Dict[str, Any] | None = None,
    extra_cells: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build a minimal notebook fixture with realistic machine-specific metadata."""
    metadata: Dict[str, Any] = {
        "kernelspec": {
            "display_name": ".venv",
            "language": "python",
            "name": "python3",
        },
        "language_info": {
            "codemirror_mode": {"name": "ipython", "version": 3},
            "file_extension": ".py",
            "mimetype": "text/x-python",
            "name": "python",
            "nbconvert_exporter": "python",
            "pygments_lexer": "ipython3",
            "version": "3.14.3",
        },
    }
    if is_documentation:
        metadata["is_documentation"] = True
    if extra_metadata:
        metadata.update(extra_metadata)

    cells: list[Dict[str, Any]] = [
        {
            "cell_type": "code",
            "execution_count": 7,
            "id": "abc12345",
            "metadata": {
                "execution": {
                    "iopub.execute_input": "2026-04-17T07:08:45.581792Z",
                    "iopub.status.busy": "2026-04-17T07:08:45.581648Z",
                    "iopub.status.idle": "2026-04-17T07:08:47.124335Z",
                    "shell.execute_reply": "2026-04-17T07:08:47.123708Z",
                },
            },
            "outputs": [{"output_type": "stream", "name": "stdout", "text": "hello\n"}],
            "source": ["print('hello')"],
        },
        {
            "cell_type": "markdown",
            "id": "def67890",
            "metadata": {"execution": {"keep": "no"}},
            "source": ["# Section header"],
        },
    ]
    if extra_cells:
        cells.extend(extra_cells)

    return {
        "cells": cells,
        "metadata": metadata,
        "nbformat": 4,
        "nbformat_minor": 5,
    }


# --------------------------------------------------------------------------- #
# Metadata normalization — the "cross-machine" guarantee
# --------------------------------------------------------------------------- #


def test_kernelspec_replaced_with_canonical_form() -> None:
    nb = _make_nb()
    cleaned = clean_notebook_outputs(nb)
    assert cleaned["metadata"]["kernelspec"] == _CANONICAL_KERNELSPEC


def test_kernelspec_display_name_no_longer_leaks_venv_name() -> None:
    nb = _make_nb()
    nb["metadata"]["kernelspec"]["display_name"] = "/Users/alice/projects/foo/.venv"
    cleaned = clean_notebook_outputs(nb)
    assert "alice" not in json.dumps(cleaned["metadata"]["kernelspec"])
    assert cleaned["metadata"]["kernelspec"]["display_name"] == "Python 3"


def test_language_info_python_version_stripped() -> None:
    nb = _make_nb()
    cleaned = clean_notebook_outputs(nb)
    assert "version" not in cleaned["metadata"]["language_info"]


def test_language_info_codemirror_version_stripped() -> None:
    nb = _make_nb()
    cleaned = clean_notebook_outputs(nb)
    cm = cleaned["metadata"]["language_info"]["codemirror_mode"]
    assert cm == {"name": "ipython"}


def test_language_info_stable_keys_preserved() -> None:
    nb = _make_nb()
    cleaned = clean_notebook_outputs(nb)
    info = cleaned["metadata"]["language_info"]
    assert info["file_extension"] == ".py"
    assert info["mimetype"] == "text/x-python"
    assert info["name"] == "python"
    assert info["nbconvert_exporter"] == "python"
    assert info["pygments_lexer"] == "ipython3"


def test_codemirror_mode_as_string_preserved() -> None:
    """Older notebooks store codemirror_mode as a plain string rather than a dict."""
    nb = _make_nb()
    nb["metadata"]["language_info"]["codemirror_mode"] = "ipython"
    cleaned = clean_notebook_outputs(nb)
    assert cleaned["metadata"]["language_info"]["codemirror_mode"] == "ipython"


def test_missing_kernelspec_left_alone() -> None:
    """Don't synthesize kernelspec where the notebook had none."""
    nb = _make_nb()
    del nb["metadata"]["kernelspec"]
    cleaned = clean_notebook_outputs(nb)
    assert "kernelspec" not in cleaned["metadata"]


def test_missing_language_info_left_alone() -> None:
    nb = _make_nb()
    del nb["metadata"]["language_info"]
    cleaned = clean_notebook_outputs(nb)
    assert "language_info" not in cleaned["metadata"]


def test_missing_metadata_block_does_not_crash() -> None:
    nb: Dict[str, Any] = {"cells": [], "nbformat": 4, "nbformat_minor": 5}
    cleaned = clean_notebook_outputs(nb)
    assert cleaned == {"cells": [], "nbformat": 4, "nbformat_minor": 5}


# --------------------------------------------------------------------------- #
# Per-cell execution metadata stripping
# --------------------------------------------------------------------------- #


def test_cell_execution_timestamps_stripped() -> None:
    nb = _make_nb()
    cleaned = clean_notebook_outputs(nb)
    for cell in cleaned["cells"]:
        assert "execution" not in cell["metadata"]


def test_cell_execution_stripped_for_documentation_notebook_too() -> None:
    """Per-cell timestamps vary on every run regardless of is_documentation."""
    nb = _make_nb(is_documentation=True)
    cleaned = clean_notebook_outputs(nb)
    for cell in cleaned["cells"]:
        assert "execution" not in cell["metadata"]


def test_other_cell_metadata_preserved() -> None:
    nb = _make_nb()
    nb["cells"][0]["metadata"]["tags"] = ["important"]
    cleaned = clean_notebook_outputs(nb)
    assert cleaned["cells"][0]["metadata"]["tags"] == ["important"]


# --------------------------------------------------------------------------- #
# Output stripping
# --------------------------------------------------------------------------- #


def test_code_cell_outputs_cleared_for_regular_notebook() -> None:
    nb = _make_nb()
    cleaned = clean_notebook_outputs(nb)
    code_cell = cleaned["cells"][0]
    assert code_cell["outputs"] == []
    assert code_cell["execution_count"] is None


def test_code_cell_outputs_preserved_for_documentation_notebook() -> None:
    nb = _make_nb(is_documentation=True)
    cleaned = clean_notebook_outputs(nb)
    code_cell = cleaned["cells"][0]
    assert code_cell["outputs"] != []
    assert code_cell["execution_count"] == 7


def test_markdown_cells_unaffected() -> None:
    nb = _make_nb()
    cleaned = clean_notebook_outputs(nb)
    md_cell = cleaned["cells"][1]
    assert md_cell["cell_type"] == "markdown"
    assert md_cell["source"] == ["# Section header"]


# --------------------------------------------------------------------------- #
# Idempotence — the property that prevents phantom "modified" state
# --------------------------------------------------------------------------- #


def test_clean_is_idempotent_regular_notebook() -> None:
    nb = _make_nb()
    first = json.dumps(clean_notebook_outputs(json.loads(json.dumps(nb))), indent=1)
    second_input = json.loads(first)
    second = json.dumps(clean_notebook_outputs(second_input), indent=1)
    assert first == second


def test_clean_is_idempotent_documentation_notebook() -> None:
    nb = _make_nb(is_documentation=True)
    first = json.dumps(clean_notebook_outputs(json.loads(json.dumps(nb))), indent=1)
    second_input = json.loads(first)
    second = json.dumps(clean_notebook_outputs(second_input), indent=1)
    assert first == second


def test_clean_produces_same_output_across_python_versions() -> None:
    """Two notebooks differing only in interpreter metadata clean to the same bytes."""
    nb_local = _make_nb()
    nb_other = _make_nb()
    nb_other["metadata"]["language_info"]["version"] = "3.11.5"
    nb_other["metadata"]["language_info"]["codemirror_mode"]["version"] = 2
    nb_other["metadata"]["kernelspec"]["display_name"] = "Python 3 (ipykernel)"
    cleaned_local = json.dumps(clean_notebook_outputs(nb_local), indent=1)
    cleaned_other = json.dumps(clean_notebook_outputs(nb_other), indent=1)
    assert cleaned_local == cleaned_other


# --------------------------------------------------------------------------- #
# clean_notebook_stdin — the stdin/stdout wrapper used by the git filter
# --------------------------------------------------------------------------- #


def _run_stdin_clean(stdin_text: str) -> tuple[str, str]:
    stdin = io.StringIO(stdin_text)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("sys.stdin", stdin), patch("sys.stdout", stdout), patch(
        "sys.stderr", stderr
    ):
        clean_notebook_stdin()
    return stdout.getvalue(), stderr.getvalue()


def test_stdin_clean_round_trips_valid_notebook() -> None:
    nb = _make_nb()
    out, err = _run_stdin_clean(json.dumps(nb))
    assert err == ""
    parsed = json.loads(out)
    assert parsed["metadata"]["kernelspec"] == _CANONICAL_KERNELSPEC
    assert parsed["cells"][0]["outputs"] == []


def test_stdin_clean_appends_trailing_newline() -> None:
    nb = _make_nb()
    out, _err = _run_stdin_clean(json.dumps(nb))
    assert out.endswith("\n")


def test_stdin_clean_passthrough_on_invalid_json() -> None:
    garbage = "this is not json {{{"
    out, err = _run_stdin_clean(garbage)
    assert out == garbage
    assert "Failed to parse notebook JSON" in err


def test_stdin_clean_empty_input_is_passthrough() -> None:
    """git can hand the filter an empty file; don't crash, don't synthesize JSON."""
    out, err = _run_stdin_clean("")
    assert out == ""
    assert err == ""


def test_stdin_clean_is_idempotent_via_pipe() -> None:
    """Round-tripping through clean_notebook_stdin twice produces identical bytes."""
    nb = _make_nb()
    first, _ = _run_stdin_clean(json.dumps(nb))
    second, _ = _run_stdin_clean(first)
    assert first == second


# --------------------------------------------------------------------------- #
# Non-mapping defensive cases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad_value", [None, 42, "string"])
def test_metadata_block_non_dict_is_ignored(bad_value: Any) -> None:
    nb = {"cells": [], "metadata": bad_value, "nbformat": 4, "nbformat_minor": 5}
    cleaned = clean_notebook_outputs(nb)
    assert cleaned["metadata"] == bad_value


def test_non_dict_cells_skipped() -> None:
    nb = _make_nb()
    nb["cells"].append("not a dict")  # type: ignore[arg-type]
    cleaned = clean_notebook_outputs(nb)
    assert cleaned["cells"][-1] == "not a dict"


# --------------------------------------------------------------------------- #
# Editor-/runtime-specific blocks — VSCode, Colab, papermill, widgets
# --------------------------------------------------------------------------- #


def test_vscode_interpreter_hash_stripped_from_notebook_metadata() -> None:
    """VSCode writes sha256(local-venv-path) into metadata.vscode.interpreter.hash."""
    nb = _make_nb()
    nb["metadata"]["vscode"] = {
        "interpreter": {
            "hash": "d4e0a3b5f2c8e7a1b9d2c4f6e8a0b2d4f6e8a0b2d4f6e8a0b2d4f6e8a0b2d4f6"
        },
    }
    cleaned = clean_notebook_outputs(nb)
    assert "vscode" not in cleaned["metadata"]


def test_colab_block_stripped_from_notebook_metadata() -> None:
    nb = _make_nb()
    nb["metadata"]["colab"] = {"provenance": [], "toc_visible": True}
    cleaned = clean_notebook_outputs(nb)
    assert "colab" not in cleaned["metadata"]


def test_papermill_block_stripped_from_notebook_metadata() -> None:
    nb = _make_nb()
    nb["metadata"]["papermill"] = {
        "start_time": "2026-04-17T07:08:45.581792",
        "end_time": "2026-04-17T07:08:47.124335",
        "duration": 1.54,
    }
    cleaned = clean_notebook_outputs(nb)
    assert "papermill" not in cleaned["metadata"]


def test_widgets_block_stripped_from_notebook_metadata() -> None:
    """Widget state carries per-session UUIDs that change on every kernel restart."""
    nb = _make_nb()
    nb["metadata"]["widgets"] = {
        "application/vnd.jupyter.widget-state+json": {
            "state": {"abc123-uuid": {"model_module": "@jupyter-widgets/base"}},
            "version_major": 2,
            "version_minor": 0,
        },
    }
    cleaned = clean_notebook_outputs(nb)
    assert "widgets" not in cleaned["metadata"]


def test_var_inspector_block_stripped_from_notebook_metadata() -> None:
    nb = _make_nb()
    nb["metadata"]["varInspector"] = {"library": "var_list.py", "cols": {}}
    cleaned = clean_notebook_outputs(nb)
    assert "varInspector" not in cleaned["metadata"]


def test_vscode_block_stripped_from_cell_metadata() -> None:
    nb = _make_nb()
    nb["cells"][0]["metadata"]["vscode"] = {"languageId": "python"}
    cleaned = clean_notebook_outputs(nb)
    assert "vscode" not in cleaned["cells"][0]["metadata"]


def test_colab_block_stripped_from_cell_metadata() -> None:
    nb = _make_nb()
    nb["cells"][0]["metadata"]["colab"] = {"base_uri": "https://localhost:8080/"}
    cleaned = clean_notebook_outputs(nb)
    assert "colab" not in cleaned["cells"][0]["metadata"]


def test_papermill_block_stripped_from_cell_metadata() -> None:
    nb = _make_nb()
    nb["cells"][0]["metadata"]["papermill"] = {"duration": 0.123, "status": "completed"}
    cleaned = clean_notebook_outputs(nb)
    assert "papermill" not in cleaned["cells"][0]["metadata"]


def test_colab_execution_info_stripped_from_cell_metadata() -> None:
    """Colab writes per-cell timestamps + user info into executionInfo."""
    nb = _make_nb()
    nb["cells"][0]["metadata"]["executionInfo"] = {
        "elapsed": 1234,
        "status": "ok",
        "timestamp": 1700000000000,
        "user": {"displayName": "Some User", "userId": "01234567890"},
    }
    cleaned = clean_notebook_outputs(nb)
    assert "executionInfo" not in cleaned["cells"][0]["metadata"]


def test_strips_all_editor_blocks_in_one_pass() -> None:
    """All editor/runtime blocks gone simultaneously — no interference between them."""
    nb = _make_nb()
    nb["metadata"]["vscode"] = {"interpreter": {"hash": "abc"}}
    nb["metadata"]["colab"] = {"provenance": []}
    nb["metadata"]["papermill"] = {"duration": 1.0}
    nb["metadata"]["widgets"] = {"state": {}}
    nb["cells"][0]["metadata"]["vscode"] = {"languageId": "python"}
    nb["cells"][0]["metadata"]["colab"] = {"base_uri": "x"}
    nb["cells"][0]["metadata"]["papermill"] = {"duration": 0.1}
    nb["cells"][0]["metadata"]["executionInfo"] = {"timestamp": 1700000000000}
    cleaned = clean_notebook_outputs(nb)
    for key in ("vscode", "colab", "papermill", "widgets"):
        assert key not in cleaned["metadata"]
    for key in ("vscode", "colab", "papermill", "executionInfo", "execution"):
        assert key not in cleaned["cells"][0]["metadata"]
