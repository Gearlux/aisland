import json
import sys
from typing import Any, Dict


def clean_notebook_outputs(nb_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Clears the outputs of all code cells in a Jupyter notebook,
    unless the notebook is marked as documentation in its metadata.
    """
    # Check for documentation marker in metadata
    metadata = nb_data.get("metadata", {})
    if metadata.get("is_documentation") is True:
        return nb_data

    # Clear outputs for all code cells
    for cell in nb_data.get("cells", []):
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None

    return nb_data


def clean_notebook_stdin() -> None:
    """Reads a notebook from stdin, cleans it, and writes to stdout."""
    try:
        data = sys.stdin.read()
        if not data.strip():
            return
        nb = json.loads(data)
        cleaned_nb = clean_notebook_outputs(nb)
        json.dump(cleaned_nb, sys.stdout, indent=1, ensure_ascii=False)
        sys.stdout.write("\n")
    except (json.JSONDecodeError, Exception) as e:
        # If parsing fails, just output the original data to avoid data loss
        sys.stdout.write(data)
        sys.stderr.write(f"Warning: Failed to clean notebook: {e}\n")
