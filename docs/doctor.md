# The environment doctor

`aisland doctor` checks the shared virtual environment, Docker, Java and other tools for known, recurring
skews — two parts of the environment out of step, such as two packages installed at versions that do not work
together, or a setting left behind by a tool since removed — and prints the command that fixes each. It checks the machine and the Python it runs on, so it runs anywhere,
in a workspace or not. A probe that does not finish within two minutes is named, never waited on.

Most checks know the skews of a Python machine-learning stack — OpenCV, `huggingface_hub` beside `transformers`,
the MCP SDK, torch's extension builds — beside Docker, Jenkins' Java, the tunnel and the editor. A check whose
package or tool is not installed passes saying so, so on any other stack the doctor reports only what applies.

[`examples/03_doctor.py`](../examples/03_doctor.py) runs the doctor and checks its report: one line per check and an
exit status of 0 exactly when every check passed.

## Running the checks

The shared `.venv` is built incrementally by every project, so one install can leave a
package pair inconsistent — and those failures surface far from their cause (a `dlopen`
error inside a *pytest collection* step, an `ImportError` three libraries deep in an
unrelated chain). The Docker layer (Colima) has its own equivalents: a client config left
behind by an uninstalled tool, a VM whose trust store does not match the network the host is
on. Homebrew drifts too: its newest Java can be one Jenkins refuses. `aisland doctor` checks for
these known skews and prints the exact command that fixes each one:

```bash
aisland doctor
# Environment doctor — /path/to/.venv/bin/python3
#   ✓ opencv           cv2 4.13.0 imports; only opencv-python-headless=4.13.0.92 installed
#   ✓ huggingface_hub  huggingface_hub 1.12.2 satisfies transformers' import surface
#   ✓ mcp              mcp 2.0.0 exposes MCPServer — MCP servers can start
#   ✓ docker-creds     no credsStore configured — public pulls run anonymously
#   ✓ colima-tls       the VM verifies TLS to https://ghcr.io/v2/ — pulls can download
#   ✓ jenkins-java     Jenkins 2.555.2 runs on Java 25.0.3 (/opt/homebrew/opt/openjdk@25/bin/java)
#   ✓ torch-ext-lock   torch 2.14.1 holds an OS lock while it builds — a killed build leaves no lock that blocks
#   ✓ tunnel-relay     no tunnel configured here (nothing in ~/.aisland/tunnel)
#   ✓ tunnel-connect   `aisland tunnel connect` not used here (nothing in ~/.aisland/tunnel)
#   ✓ pylance-memory   Pylance ran out of memory at 2026-01-12 09:00:00 and runs on its own Node.js since 2026-01-12 09:08:00
#
# All 10 checks passed.
```

It exits non-zero when a check fails, so it works as a CI preflight step. The checks run side by side, so a run
takes about as long as its slowest check, and the whole run has a time limit of two and a half minutes: a check
still running then is listed `not checked: time budget spent`, so the report — and the MCP tool `doctor` — always
answers. Current checks:

| Check | Invariant | Symptom when violated |
|---|---|---|
| `opencv` | Only `opencv-python-headless` is installed (the root `uv.toml` override drops the GUI wheel — both distributions own `cv2/`) | `Library not loaded: @loader_path/libsrt.1.5.3.dylib` during pytest **collection** |
| `huggingface_hub` | `transformers`' import surface (`is_offline_mode`) resolves | `ImportError` while importing `pytorch_lightning` via `torchmetrics` |
| `mcp` | The installed SDK exposes `mcp.server.mcpserver.MCPServer` | An MCP server entry point dies on import (`No module named 'mcp.server.fastmcp'`) while the CLI built on the same package keeps working |
| `docker-creds` | `~/.docker/config.json` names no credential helper that is missing from PATH (Docker Desktop's `credsStore: "desktop"` outlives its uninstall) | `exec: "docker-credential-desktop": executable file not found in $PATH` on **every** `docker pull`, even of public images |
| `colima-tls` | The Colima VM verifies TLS on the host's network (a TLS-intercepting proxy's CA must be in the VM's trust store, not just the macOS keychain) | `tls: failed to verify certificate: x509: certificate signed by unknown authority` during `docker pull`, while the same URL works on the host |
| `jenkins-java` | An installed JDK is one the downloaded Jenkins WAR accepts ([Which Java runs Jenkins](services.md#which-java-runs-jenkins)); passes with a note before Jenkins is downloaded | `aisland jenkins start` stops with `Jenkins 2.555.2 runs on Java 21 or 25, and none is installed (found 26.0.2.1)` |
| `torch-ext-lock` | No stale torch extension build lock blocks an import ([below](#the-torch-ext-lock-check)) | An import that compiles a torch extension sleeps forever with no error; the fix line is the `rm` of the lock file |
| `tunnel-relay` | A machine that has used `aisland tunnel` reaches the relay the tunnel dials out to (probed only there) | The tunnel starts, logs nothing useful and never appears in the editor; the fix line is the `curl` that confirms it |
| `tunnel-connect` | A machine that has used `aisland tunnel connect` has `devtunnel` on PATH, `msgpack` (the `[connect]` extra) and a signed-in `devtunnel` (checked only there; offline, it passes with a note) | `connect` stops with `No 'devtunnel' command found`, `needs msgpack` or `devtunnel is not signed in`; the fix line installs or signs in |
| `pylance-memory` | VS Code's Python language server, Pylance, has not run out of memory in the newest window opened on this workspace — or has started on a Node.js of its own since (read from that window's `Python Language Server.log` under VS Code's `logs` folder; a machine with no such log passes with a note) | No completion, no hover, *Go to Symbol in Workspace* finds nothing; the log repeats `FATAL ERROR: … JavaScript heap out of memory` every half minute or so. The fix line adds `"python.analysis.nodeExecutable": "auto"` to the User settings — never the workspace file: VS Code ignores a machine-scoped setting there — and, when it crashed on its own Node.js too, a larger heap through `"python.analysis.nodeArguments"` |

Every check is cheap and read-only, and reports one specific skew with the command that fixes it.

### The `torch-ext-lock` check

torch compiles some extensions the first time they are imported, and while it compiles it holds a lock file in
the extension's build folder: `<root>/py<this python>_<accelerator>/<extension>/lock`, where the root is
`$TORCH_EXTENSIONS_DIR` or torch's per-user cache.

- **torch 2.14.1 or newer**: the check passes. torch builds under an operating-system lock, which a killed process
  releases; the `lock` file it leaves after every build blocks nothing.
- **An older torch**: torch waits on an existing lock without a time limit, and deletes it only when the process
  that compiles lives through the build. A build killed half way leaves the lock, and every later import that
  compiles the extension then waits forever — about 8 seconds of CPU, then nothing, and no error. The check fails
  on a lock in this Python's folder that is older than two minutes (a build takes seconds), and prints the `rm` that
  removes it. A younger lock is a build in progress, and passes.

## When a command fails

Every command that fails prints one `Error:` line on stderr and exits 1 — a refusal in aisland's own words, or what
the machine refused (a file it may not read, a program that failed). A command that goes on past a failure (`aisland
setup` installing the other projects, `aisland services start` starting the other services) prints that line and
still exits 1 at the end. To see where an error came from, run the command again with `--debug`; it also shows
aisland's debug log on stderr, one `aisland: <module>: <message>` line each (the workspace root it found and why, every
`git` and install command it ran, the files it read):

```bash
aisland --debug docker start
# Traceback (most recent call last):
#   ...
# Error: the Docker engine is not running. Start it: sudo systemctl start docker
```
