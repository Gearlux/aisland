"""`aisland tunnel`: a VS Code Remote Tunnel to the machine aisland runs on.

`host` runs the tunnel on this machine (the CLI it downloads, the process, the workspaces opened through it);
`connect` is `aisland tunnel connect`, a terminal on a remote machine through the same tunnel, whose shell is
`shell` — standard library only, sent to the remote machine as source. Nothing is imported here.
"""
