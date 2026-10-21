"""A stand-in for Microsoft's ``devtunnel`` command, for ``tests/test_tunnel_connect.py``.

It answers the three commands ``aisland tunnel connect`` runs, from files in ``$FAKE_DT_STATE``:

- ``user show`` — ``Logged in as … using Microsoft.`` when ``account`` exists, else ``Not logged in.``;
- ``list -j`` — the contents of ``tunnels.json`` (a banner goes to stderr, as the real one prints);
- ``connect <id>`` — records its arguments (``connect.args``) and pid (``connect.pid``), prints
  ``connect.fail`` and exits 1 when that file exists, else serves a fake VS Code tunnel control port
  on ``$FAKE_DT_PORT`` until it is terminated.

The control port speaks the real tunnel's messages (MessagePack, measured on a remote box): a
``version`` notice on connect, ``spawn`` answered by ``streams_started`` with stdin/stdout/stderr
stream ids, ``stream_data`` / ``stream_ended`` both ways, and the spawn's result
``{"message", "exit_code"}``. ``spawn`` runs the program HERE, with ``HOME`` set to
``$FAKE_DT_HOME`` so a login shell reads no start-up file of the person running the tests. A
``spawn.error`` file makes it refuse the spawn with that text, as the real one does with an error.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List

import msgpack

STATE = Path(os.environ["FAKE_DT_STATE"])


def _serve_connection(conn: socket.socket) -> None:
    lock = threading.Lock()

    def send(message: Dict[str, Any]) -> None:
        with lock:
            conn.sendall(msgpack.packb(message, use_bin_type=True))

    send({"id": None, "method": "version", "params": {"version": "fake", "protocol_version": 6}})
    unpacker = msgpack.Unpacker(raw=False)
    process: Any = None
    while True:
        data = conn.recv(65536)
        if not data:
            break
        unpacker.feed(data)
        for message in unpacker:
            method = message.get("method")
            if method == "spawn":
                request_id = message["id"]
                refusal = STATE / "spawn.error"
                if refusal.exists():
                    send({"id": request_id, "error": {"code": -1, "message": refusal.read_text().strip()}})
                    continue
                params = message["params"]
                env = {**os.environ, **params.get("env", {}), "HOME": os.environ["FAKE_DT_HOME"]}
                process = subprocess.Popen(
                    [params["command"], *params["args"]],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=params.get("cwd"),
                )
                send(
                    {
                        "id": None,
                        "method": "streams_started",
                        "params": {"for_request_id": request_id, "stream_ids": [0, 1, 2]},
                    }
                )
                readers: List[threading.Thread] = []
                for stream_id, pipe in ((1, process.stdout), (2, process.stderr)):

                    def pump(stream_id: int = stream_id, pipe: Any = pipe) -> None:
                        for chunk in iter(lambda: pipe.read1(4096), b""):
                            send(
                                {"id": None, "method": "stream_data", "params": {"stream": stream_id, "segment": chunk}}
                            )
                        send({"id": None, "method": "stream_ended", "params": {"stream": stream_id}})

                    reader = threading.Thread(target=pump, daemon=True)
                    reader.start()
                    readers.append(reader)

                def finish(request_id: int = request_id, readers: List[threading.Thread] = readers) -> None:
                    code = process.wait()
                    for reader in readers:
                        reader.join()
                    send({"id": request_id, "result": {"message": f"exit status: {code}", "exit_code": code}})

                threading.Thread(target=finish, daemon=True).start()
            elif method == "stream_data" and process is not None:
                process.stdin.write(message["params"]["segment"])
                process.stdin.flush()
            elif method == "stream_ended" and process is not None:
                process.stdin.close()


def _connect(tunnel_id: str) -> int:
    (STATE / "connect.args").write_text(tunnel_id)
    (STATE / "connect.pid").write_text(str(os.getpid()))
    failure = STATE / "connect.fail"
    if failure.exists():
        print(failure.read_text().strip())
        return 1
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", int(os.environ["FAKE_DT_PORT"])))
    server.listen()
    print(f"Connected to tunnel: {tunnel_id}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    while True:
        conn, _ = server.accept()
        threading.Thread(target=_serve_connection, args=(conn,), daemon=True).start()


def main(argv: List[str]) -> int:
    if argv[:2] == ["user", "show"]:
        account = STATE / "account"
        print(
            "Logged in as " + account.read_text().strip() + " using Microsoft."
            if account.exists()
            else "Not logged in."
        )
        return 0
    if argv[:1] == ["list"]:
        print("Welcome to dev tunnels!", file=sys.stderr)
        print((STATE / "tunnels.json").read_text())
        return 0
    if argv[:1] == ["connect"]:
        return _connect(argv[1])
    print(f"fake devtunnel: unexpected arguments {json.dumps(argv)}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
