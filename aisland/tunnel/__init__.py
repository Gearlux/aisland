"""`aisland tunnel`: a VS Code Remote Tunnel to the machine aisland runs on.

`host` runs the tunnel on this machine (the process, its sign-in, opening an editor on a remote one); `binary` is
the VS Code CLI it downloads, `store` the workspaces opened through it, and `state` the per-machine folder and the
tunnel-name rule all of them share. `connect` is `aisland tunnel connect`, a terminal on a remote machine through the
same tunnel, whose shell is `shell` — standard library only, sent to the remote machine as source. Nothing is
imported here.
"""
