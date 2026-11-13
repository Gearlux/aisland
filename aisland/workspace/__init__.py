"""The workspace and what it is made of. `init` makes one (`aisland workspace init`), `file` is `workspace.toml`,
`check` names the keys in it no part of aisland reads, `local` is a root no one else shares (`init --local`).
`projects` lists the projects and their dependency order, `gitmodules` reads `.gitmodules`, `discovery` what the
projects declare to aisland, `clones` adopts a clone the workspace does not list. What a project or a machine chooses
is a plugin of `configure` (`aisland workspace configure`): `sources`, `mirrors`, `activation`, `selection`; beside them
`tracking` (whether the root records a project's commits) and `recording` (whether its test runs are recorded).
Nothing is imported here, so a module that reads `workspace.toml` imports only `file`.
"""
