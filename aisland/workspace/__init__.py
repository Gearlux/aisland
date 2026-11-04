"""The workspace itself: `init` makes one (`aisland workspace init`), `file` is `workspace.toml`, `check` names the
keys in it no part of aisland reads, `local` is a root no one else shares (`init --local`). Nothing is imported here,
so a module that reads `workspace.toml` imports only `file`.
"""
