"""`aisland wheels`: the wheels a project needs where the workspace is not.

`build` is the run (`aisland wheels build`) and drives the others: `projects` (the workspace's projects and what
the target needs), `published` (what PyPI has of them), `export` (what a wheel is built from), `report` (what it
says), `files` (the folder it writes), `check` (the empty venv the wheels are tried in), `bundle`, `extra`, `scrub`
(a target off GitHub), `prune` with `filter_repo` (history rewriting), and `install` / `unzip`, standard library
only, copied into the folder and the zip because they run where aisland is not. Nothing is imported here: a module
is imported by its own name.
"""
