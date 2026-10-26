# Notebooks

How aisland keeps Jupyter notebooks small in Git by clearing their outputs, and how to keep the outputs of a documentation notebook.

[`examples/05_notebooks_clean.py`](../examples/05_notebooks_clean.py) cleans a notebook and a documentation notebook and
checks which outputs are kept.

## Jupyter Notebook Management

To keep the repository size small and diffs clean, all Jupyter notebooks (`*.ipynb`) are automatically processed by a Git filter (`aisland notebooks clean`, which `aisland setup` configures together with a `*.ipynb filter=ipynb-cleaner` line in `.gitattributes`) that clears their code cell outputs before staging. It also strips machine-specific metadata (the local Python version, the kernel's display name, per-cell execution timestamps), even from documentation notebooks.

### Marking as Documentation
If you have a notebook that serves as documentation and you **want** to preserve its outputs (e.g., visualizations, benchmark results), you must mark it as such in its metadata:

1. Open the notebook in VS Code or Jupyter.
2. Edit the notebook metadata (in VS Code: `Notebook Information` -> `Open Metadata`).
3. Add `"is_documentation": true` to the `metadata` object:
```json
{
  "metadata": {
    "is_documentation": true,
    ...
  },
  ...
}
```

### Manual Cleaning
You can also manually clean a notebook using the CLI:
```bash
cat my_notebook.ipynb | aisland notebooks clean > cleaned_notebook.ipynb
```
