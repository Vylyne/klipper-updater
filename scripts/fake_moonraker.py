#!/usr/bin/env python3
"""A stand-in Moonraker, for testing the agent without a printer.

Unix sockets aren't available to Python on Windows, so the agent's socket path
can't be exercised on the dev box. This harness closes that gap: run it on any
Linux box (a Pi, a VM, WSL) and it speaks enough of Moonraker's protocol to drive
the real agent process end to end.

    # terminal 1
    ./scripts/fake_moonraker.py /tmp/fake-moonraker.sock

    # terminal 2
    PYTHONPATH=src python3 -m klipper_updater.agent \\
        --socket /tmp/fake-moonraker.sock -v

Then type method names at the harness prompt:

    > fw.ping
    > fw.status
    > fw.bus.scan {"only_untracked": true}

It prints the agent's replies, and every event the agent pushes. Combine with
KLIPPER_UPDATER_FAKE_BUS to simulate boards appearing and disappearing.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading

ETX = b"\x03"


class Harness:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: socket.socket | None = None
        self.buffer = bytearray()
        self.next_id = 1000
        self.registered: list[str] = []
        self.lock = threading.Lock()

    # -- wire --------------------------------------------------------------

    def send(self, payload: dict) -> None:
        conn = self.conn
        if conn is None:
            print("!! agent not connected")
            return
        with self.lock:
            conn.sendall(json.dumps(payload).encode() + ETX)

    def reply(self, req: dict, result: object) -> None:
        self.send({"jsonrpc": "2.0", "id": req["id"], "result": result})

    # -- inbound -----------------------------------------------------------

    def handle(self, msg: dict) -> None:
        method = msg.get("method")

        if method and msg.get("id") is not None:
            # A request from the agent.
            if method == "server.connection.identify":
                p = msg.get("params", {})
                print(f"\n== agent identified ==\n{json.dumps(p, indent=2)}")
                missing = {"client_name", "version", "type", "url"} - set(p)
                if missing:
                    print(f"!! MISSING required identify fields: {sorted(missing)}")
                if p.get("type") != "agent":
                    print(f"!! type must be 'agent', got {p.get('type')!r}")
                self.reply(msg, {"connection_id": 1730367696})
            elif method == "connection.register_remote_method":
                name = msg["params"]["method_name"]
                self.registered.append(name)
                print(f"   registered: {name}")
                self.reply(msg, "ok")
            elif method == "machine.system_info":
                self.reply(
                    msg,
                    {"system_info": {"service_state": {"klipper": {"active_state": "active"}}}},
                )
            elif method == "printer.objects.query":
                self.reply(msg, {"status": {"print_stats": {"state": "standby"}}})
            else:
                print(f"   (agent called {method}, replying {{}})")
                self.reply(msg, {})

        elif method == "connection.send_event":
            p = msg.get("params", {})
            name = p.get("event")
            data = p.get("data")
            if name in ("connected", "disconnected"):
                print(f"!! agent tried to emit reserved event '{name}'")
            summary = ""
            if isinstance(data, dict):
                if "types" in data:
                    summary = f"{len(data['types'])} type(s), {len(data.get('bus', []))} device(s)"
                elif "devices" in data:
                    summary = f"{len(data['devices'])} device(s)"
            print(f"<< event '{name}' {summary}")

        elif msg.get("id") is not None:
            # A response to something we asked for.
            if "error" in msg:
                print(f"<< ERROR {json.dumps(msg['error'], indent=2)}")
            else:
                print(f"<< {json.dumps(msg.get('result'), indent=2)}")

    def read_loop(self, conn: socket.socket) -> None:
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            self.buffer.extend(chunk)
            while True:
                idx = self.buffer.find(ETX)
                if idx < 0:
                    break
                raw = bytes(self.buffer[:idx])
                del self.buffer[: idx + 1]
                if not raw.strip():
                    continue
                try:
                    self.handle(json.loads(raw))
                except Exception as exc:  # noqa: BLE001
                    print(f"!! bad message ({exc}): {raw[:200]!r}")
        print("\n== agent disconnected ==")
        self.conn = None

    # -- run ---------------------------------------------------------------

    def serve(self) -> None:
        if os.path.exists(self.path):
            os.unlink(self.path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.listen(1)
        print(f"listening on {self.path}\nwaiting for the agent...")

        def accept_loop() -> None:
            while True:
                conn, _ = srv.accept()
                self.conn = conn
                self.buffer.clear()
                self.registered.clear()
                print("\n== agent connected ==")
                self.read_loop(conn)

        threading.Thread(target=accept_loop, daemon=True).start()
        self.prompt()

    def prompt(self) -> None:
        print("\nType a method name to call the agent, e.g. 'fw.status'.")
        print("Optionally follow it with a JSON object of arguments. Ctrl-D to quit.\n")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line:
                continue
            if line in ("methods", "?"):
                print(f"registered: {self.registered}")
                continue
            parts = line.split(None, 1)
            method = parts[0]
            params: object = {}
            if len(parts) > 1:
                try:
                    params = json.loads(parts[1])
                except ValueError as exc:
                    print(f"!! bad JSON arguments: {exc}")
                    continue
            self.next_id += 1
            self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})


def main() -> int:
    if sys.platform == "win32":
        print("Unix sockets aren't available here - run this on Linux (a Pi, VM, or WSL).")
        return 1
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake-moonraker.sock"
    try:
        Harness(path).serve()
    finally:
        if os.path.exists(path):
            os.unlink(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
