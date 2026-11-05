"""Setting a machine up for the workspace: `manager` runs `aisland setup`'s steps in order, each step a module here —
`provision` and `installers` (the system tools), `uv_config` (the root `uv.toml`), `editor_files` (the editor's
workspace file and `pyrightconfig.json`), `mcp_config` (the AI clients' server lists), `env_example` (`env.complete` and
`env.example`), `completion` (the shell's), `playwright_browser` and `git_settings`. Nothing is imported here.
"""
