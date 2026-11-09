# Notebooks

How aisland keeps Jupyter notebooks small in Git by clearing their outputs, and how to keep the outputs of a documentation notebook.

[`examples/05_notebooks_clean.py`](../examples/05_notebooks_clean.py) cleans a notebook and a documentation notebook and
checks which outputs are kept.

## Jupyter notebook management

To keep the repository size small and diffs clean, Jupyter notebooks (`*.ipynb`) are processed by a Git filter (`aisland notebooks clean`) that clears their code cell outputs before staging. It also strips machine-specific metadata (the local Python version, the kernel's display name, per-cell execution timestamps), even from documentation notebooks.

`aisland setup` registers the filter in the workspace's repository and in every active project's, and adds the line that applies it to the root's `.gitattributes`. git reads a project's own `.gitattributes` inside the project, never the root's, so a project whose notebooks should be cleaned carries the line in its own `.gitattributes` and commits it:

```text
*.ipynb filter=ipynb-cleaner
```

`git check-attr filter my_notebook.ipynb`, run in the project, says `filter: ipynb-cleaner` once the line is there.

The filter itself reads standard input and needs no workspace, so it also runs in a repository of its own.

### Marking a notebook as documentation

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

### Cleaning a notebook by hand

You can also manually clean a notebook using the CLI:
```bash
cat my_notebook.ipynb | aisland notebooks clean > cleaned_notebook.ipynb
```
