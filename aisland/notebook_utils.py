"""Notebook normalization for the ``aisland notebooks clean`` git filter.

Wired in as a ``filter.ipynb-cleaner.clean`` smudge/clean filter (see each
project's ``.gitattributes``) so every ``.ipynb`` checked into the workspace
is normalized on its way to the index. Two classes of noise are stripped:

1. **Cell outputs** — only for non-documentation notebooks (``is_documentation:
   true`` in top-level metadata preserves outputs).

2. **Machine-specific metadata** — always stripped, even for documentation
   notebooks. Kernel display name, local Python version, and per-cell
   wall-clock execution timestamps would otherwise produce noisy diffs every
   time the same notebook is opened on a different machine or a different
   Python interpreter.
"""

import json
import sys
from typing import Any, Dict

# Canonical kernelspec written into every cleaned notebook. The original
# `display_name` is whatever the local Jupyter installation chose for the
# venv (".venv", "Python 3 (ipykernel)", etc.) — wildly machine-dependent
# and not load-bearing for nbconvert, which keys off `name` + `language`.
_CANONICAL_KERNELSPEC: Dict[str, str] = {
    "display_name": "Python 3",
    "language": "python",
    "name": "python3",
}

# language_info keys whose values are static across Python versions. The
# version-bearing fields (`version`, `codemirror_mode.version`) hardcode the
# local interpreter and are intentionally excluded — they're the main source
# of cross-machine diff churn.
_STABLE_LANGUAGE_INFO_KEYS = (
    "file_extension",
    "mimetype",
    "name",
    "nbconvert_exporter",
    "pygments_lexer",
)

# Top-level metadata blocks that different editors / runtimes attach with
# machine-specific contents. VSCode writes ``vscode.interpreter.hash`` (a
# sha256 of the local venv path); Colab/papermill write timestamps and
# session UUIDs; widget state is a per-execution UUID map. None of it is
# load-bearing for our pipelines and all of it produces churn when the same
# notebook is opened from a different host.
_STRIPPED_NOTEBOOK_METADATA_KEYS = (
    "vscode",
    "colab",
    "papermill",
    "widgets",
    "varInspector",
)

# Per-cell metadata blocks with the same story as the top-level set, plus the
# ``execution`` block (wall-clock timestamps Jupyter writes every time a cell
# runs). ``cell.metadata.vscode`` is stripped wholesale rather than scrubbing
# just the interpreter hash — its other content (``languageId``) is redundant
# with the notebook-level ``language_info.name``.
_STRIPPED_CELL_METADATA_KEYS = (
    "execution",
    "vscode",
    "colab",
    "papermill",
    "executionInfo",
)


def _normalize_notebook_metadata(nb_data: Dict[str, Any]) -> None:
    """Rewrite top-level metadata in place, stripping machine-specific blocks.

    Three classes of churn are addressed:
      * ``kernelspec`` is replaced with the canonical form (the original
        display name is typically the local venv name).
      * ``language_info`` is rebuilt from a fixed-key set, dropping the
        Python patch version and the codemirror grammar version.
      * VSCode / Colab / papermill / widget / varInspector blocks are
        removed entirely — see ``_STRIPPED_NOTEBOOK_METADATA_KEYS``.
    """
    metadata = nb_data.get("metadata")
    if not isinstance(metadata, dict):
        return

    if "kernelspec" in metadata:
        metadata["kernelspec"] = dict(_CANONICAL_KERNELSPEC)

    language_info = metadata.get("language_info")
    if isinstance(language_info, dict):
        normalized: Dict[str, Any] = {k: language_info[k] for k in _STABLE_LANGUAGE_INFO_KEYS if k in language_info}
        codemirror = language_info.get("codemirror_mode")
        if isinstance(codemirror, dict) and "name" in codemirror:
            normalized["codemirror_mode"] = {"name": codemirror["name"]}
        elif isinstance(codemirror, str):
            normalized["codemirror_mode"] = codemirror
        metadata["language_info"] = normalized

    for key in _STRIPPED_NOTEBOOK_METADATA_KEYS:
        metadata.pop(key, None)


def _strip_cell_metadata(cell: Dict[str, Any]) -> None:
    """Drop machine-specific per-cell metadata blocks.

    See ``_STRIPPED_CELL_METADATA_KEYS`` — covers wall-clock execution
    timestamps plus VSCode / Colab / papermill blocks that carry runtime
    or interpreter-hash information.
    """
    cell_meta = cell.get("metadata")
    if not isinstance(cell_meta, dict):
        return
    for key in _STRIPPED_CELL_METADATA_KEYS:
        cell_meta.pop(key, None)


def clean_notebook_outputs(nb_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a parsed notebook so it diffs cleanly across machines.

    Always:
      * Replace ``metadata.kernelspec`` with the canonical form.
      * Strip the Python interpreter version from ``metadata.language_info``.
      * Drop ``metadata.execution`` (timestamps) from every cell.

    Only when the top-level metadata does NOT set ``is_documentation: true``:
      * Clear ``outputs`` and ``execution_count`` on every code cell.

    Returns the same object (also mutated in place) for convenience.
    """
    _normalize_notebook_metadata(nb_data)

    is_documentation = isinstance(nb_data.get("metadata"), dict) and nb_data["metadata"].get("is_documentation") is True

    for cell in nb_data.get("cells", []):
        if not isinstance(cell, dict):
            continue
        _strip_cell_metadata(cell)
        if cell.get("cell_type") == "code" and not is_documentation:
            cell["outputs"] = []
            cell["execution_count"] = None

    return nb_data


def clean_notebook_stdin() -> None:
    """Read a notebook JSON from stdin, normalize it, write it to stdout.

    Wired in as ``filter.ipynb-cleaner.clean`` so every staged ``.ipynb`` is
    normalized on its way to the git index. On parse failure the original
    bytes are echoed back unchanged so a malformed notebook never gets
    silently emptied — a warning is emitted on stderr so the operator can
    investigate.
    """
    data = sys.stdin.read()
    if not data.strip():
        sys.stdout.write(data)
        return
    try:
        nb = json.loads(data)
    except json.JSONDecodeError as e:
        sys.stdout.write(data)
        sys.stderr.write(f"Warning: Failed to parse notebook JSON, passing through unchanged: {e}\n")
        return
    cleaned_nb = clean_notebook_outputs(nb)
    json.dump(cleaned_nb, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
