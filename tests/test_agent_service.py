"""The agent's connection lifecycle, driven against a fake Moonraker.

Covers the handshake contract and the reconnect behaviour. Getting the identify
payload wrong means the agent silently never registers, which is invisible until
the panel reports "not installed".
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from klipper_updater import AGENT_NAME, __version__
from klipper_updater.agent.events import RESERVED_EVENTS, EventEmitter
from klipper_updater.agent.rpc import MoonrakerPeer, frame, unframe
from klipper_updater.agent.service import Agent


class FakeMoonraker:
    """Answers every request with a canned result and records what it saw."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = bytearray()
        self.requests: list[dict] = []
        self.notifications: list[dict] = []
        self.responses: list[dict] = []
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            self.buffer.extend(chunk)
            for raw in unframe(self.buffer):
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("id") is not None and "method" in msg:
                    self.requests.append(msg)
                    self._reply(msg)
                elif "method" in msg:
                    self.notifications.append(msg)
                elif msg.get("id") is not None:
                    # The agent answering a request we sent it.
                    self.responses.append(msg)

    def _reply(self, msg: dict) -> None:
        method = msg["method"]
        if method == "server.connection.identify":
            result: object = {"connection_id": 1730367696}
        elif method == "connection.register_remote_method":
            result = "ok"
        elif method == "machine.system_info":
            result = {"system_info": {"service_state": {"klipper": {"active_state": "active"}}}}
        elif method == "printer.objects.query":
            result = {"status": {"print_stats": {"state": "standby"}}}
        else:
            result = {}
        try:
            self.sock.sendall(frame({"jsonrpc": "2.0", "id": msg["id"], "result": result}))
        except OSError:
            pass

    def send(self, payload: dict) -> None:
        self.sock.sendall(frame(payload))

    def methods_called(self, name: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == name]

    def wait_for(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def wired(paths, live_registry_text):
    """An Agent whose peer talks to a FakeMoonraker over a socketpair."""
    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)

    agent_sock, server_sock = socket.socketpair()
    server = FakeMoonraker(server_sock)

    def factory(on_request, on_notify):
        return MoonrakerPeer(
            "unused", on_request=on_request, on_notify=on_notify, transport=agent_sock
        )

    agent = Agent(paths, socket_path="unused", peer_factory=factory)
    # Idle poll only; the watcher isn't the subject of these tests.
    agent.watcher.idle_interval = 3600
    try:
        yield agent, server
    finally:
        agent.stop()
        server.stop()
        for s in (agent_sock, server_sock):
            try:
                s.close()
            except OSError:
                pass


def _run(agent: Agent) -> threading.Thread:
    t = threading.Thread(target=agent.run_once, daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------
# handshake
# --------------------------------------------------------------------------


def test_identify_sends_exactly_the_four_required_fields(wired):
    agent, server = wired
    _run(agent)
    assert server.wait_for(lambda: server.methods_called("server.connection.identify"))

    params = server.methods_called("server.connection.identify")[0]["params"]
    assert params == {
        "client_name": AGENT_NAME,
        "version": __version__,
        "type": "agent",
        "url": "https://github.com/Vylyne/klipper-updater",
    }


def test_every_method_is_registered(wired):
    agent, server = wired
    _run(agent)
    expected = set(agent.api.available_methods())
    assert server.wait_for(
        lambda: len(server.methods_called("connection.register_remote_method")) >= len(expected)
    )
    registered = {
        r["params"]["method_name"]
        for r in server.methods_called("connection.register_remote_method")
    }
    assert registered == expected


def test_identify_happens_before_any_registration(wired):
    """Moonraker only treats us as an agent once identified."""
    agent, server = wired
    _run(agent)
    assert server.wait_for(
        lambda: len(server.methods_called("connection.register_remote_method")) > 0
    )
    order = [r["method"] for r in server.requests]
    assert order.index("server.connection.identify") < order.index(
        "connection.register_remote_method"
    )


def _events(server: FakeMoonraker, name: str) -> list[dict]:
    return [
        n["params"]
        for n in server.notifications
        if n["method"] == "connection.send_event" and n["params"].get("event") == name
    ]


def test_state_is_emitted_once_connected(wired):
    agent, server = wired
    _run(agent)
    # Wait for the state event specifically: the bus watcher starts first and its
    # event can arrive ahead of this one.
    assert server.wait_for(lambda: _events(server, "state"))

    state = _events(server, "state")[0]
    assert len(state["data"]["types"]) == 4
    assert state["data"]["klipper_service"] == "active"


def test_the_bus_is_emitted_on_connect_so_late_clients_are_not_left_empty(wired):
    agent, server = wired
    _run(agent)
    assert server.wait_for(lambda: _events(server, "bus"))
    assert "devices" in _events(server, "bus")[0]["data"]


def test_inbound_method_calls_are_served_over_the_wire(wired):
    """The full path a panel click takes: Moonraker relays a request, we answer."""
    agent, server = wired
    _run(agent)
    assert server.wait_for(lambda: server.methods_called("server.connection.identify"))

    server.send({"jsonrpc": "2.0", "method": "fw.status", "id": 999})
    assert server.wait_for(lambda: any(r["id"] == 999 for r in server.responses))

    reply = next(r for r in server.responses if r["id"] == 999)
    assert "error" not in reply
    assert len(reply["result"]["types"]) == 4


def test_an_unknown_inbound_method_gets_an_error_not_silence(wired):
    """Silence would hang the caller's HTTP request forever."""
    agent, server = wired
    _run(agent)
    assert server.wait_for(lambda: server.methods_called("server.connection.identify"))

    server.send({"jsonrpc": "2.0", "method": "fw.does.not.exist", "id": 1001})
    assert server.wait_for(lambda: any(r["id"] == 1001 for r in server.responses))
    assert next(r for r in server.responses if r["id"] == 1001)["error"]["code"] == -32601


def test_service_state_change_triggers_a_fresh_state_event(wired):
    agent, server = wired
    _run(agent)
    assert server.wait_for(lambda: server.methods_called("server.connection.identify"))
    before = len(server.notifications)

    server.send({"jsonrpc": "2.0", "method": "notify_service_state_changed", "params": [{}]})
    assert server.wait_for(lambda: len(server.notifications) > before)


def test_run_once_returns_when_moonraker_disconnects(wired):
    agent, server = wired
    t = _run(agent)
    assert server.wait_for(lambda: server.methods_called("server.connection.identify"))
    server.stop()
    t.join(timeout=5)
    assert not t.is_alive(), "run_once should return so run_forever can reconnect"


# --------------------------------------------------------------------------
# reconnect
# --------------------------------------------------------------------------


def test_run_forever_survives_a_missing_socket(paths):
    """A cold boot reaches the agent before moonraker is listening."""
    agent = Agent(paths, socket_path=str(paths.home) + "/definitely-not-a-socket")
    from klipper_updater.agent import service as service_mod

    # Collapse the backoff so the test is quick.
    original, service_mod.BACKOFF = service_mod.BACKOFF, (0.05,)
    try:
        t = threading.Thread(target=agent.run_forever, daemon=True)
        t.start()
        time.sleep(0.4)
        assert t.is_alive(), "the reconnect loop must not die on a missing socket"
        agent.stop()
        t.join(timeout=5)
        assert not t.is_alive()
    finally:
        service_mod.BACKOFF = original


def test_registration_repeats_on_every_reconnect(paths, live_registry_text):
    """Moonraker drops remote-method registrations when the connection closes."""
    with open(paths.mcus_json, "w", encoding="utf-8") as fh:
        fh.write(live_registry_text)

    servers: list[FakeMoonraker] = []
    socks: list[socket.socket] = []

    def factory(on_request, on_notify):
        a, b = socket.socketpair()
        socks.extend([a, b])
        servers.append(FakeMoonraker(b))
        return MoonrakerPeer("unused", on_request=on_request, on_notify=on_notify, transport=a)

    agent = Agent(paths, socket_path="unused", peer_factory=factory)
    agent.watcher.idle_interval = 3600
    expected = set(agent.api.available_methods())

    def registered_on(server: FakeMoonraker) -> set:
        return {
            r["params"]["method_name"]
            for r in server.methods_called("connection.register_remote_method")
        }

    def session(index: int) -> threading.Thread:
        """Start run_once and wait until its connection has fully registered.

        The peer factory runs *inside* run_once, so the server object doesn't
        exist until after the thread starts.
        """
        t = threading.Thread(target=agent.run_once, daemon=True)
        t.start()
        deadline = time.monotonic() + 5
        while len(servers) <= index and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(servers) > index, f"peer factory was not called for session {index}"
        assert servers[index].wait_for(lambda: registered_on(servers[index]) == expected), (
            "registrations are per-connection and must be redone on every reconnect"
        )
        return t

    try:
        # Ending a session by killing the server is exactly what a
        # `systemctl restart moonraker` looks like from the agent's side.
        t1 = session(0)
        servers[0].stop()
        t1.join(timeout=5)
        assert not t1.is_alive(), "run_once must return so run_forever can retry"

        t2 = session(1)
        servers[1].stop()
        t2.join(timeout=5)
    finally:
        agent.stop()
        for s in servers:
            s.stop()
        for s in socks:
            try:
                s.close()
            except OSError:
                pass

    assert len(servers) == 2


# --------------------------------------------------------------------------
# a build, end to end over the socket
# --------------------------------------------------------------------------


def test_a_build_driven_over_the_wire_streams_job_and_log_events(wired, paths):
    """The whole Phase 2 path: Moonraker relays fw.build, the agent runs it, and
    job + batched log events come back on the same socket."""
    import os

    agent, server = wired
    agent.batcher.FLUSH_INTERVAL = 0.05

    # dry_run so no toolchain is needed; the fake build log is still real output
    # through the real reporter, batcher and emitter.
    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write("[updater]\ndry_run = true\nservice_backend = null\nclean_before_build = false\n")
    os.makedirs(paths.type_dir("bttebb36"), exist_ok=True)
    with open(paths.config_file("bttebb36", "klipper"), "w", encoding="utf-8") as fh:
        fh.write("CONFIG_MACH_STM32=y\n")

    _run(agent)
    assert server.wait_for(lambda: server.methods_called("server.connection.identify"))

    server.send(
        {
            "jsonrpc": "2.0",
            "id": 500,
            "method": "fw.build",
            "params": {"name": "bttebb36", "fw": "klipper"},
        }
    )
    assert server.wait_for(lambda: any(r["id"] == 500 for r in server.responses))
    reply = next(r for r in server.responses if r["id"] == 500)
    assert "error" not in reply, reply
    job_id = reply["result"]["job_id"]

    # The job finishes, and we saw it both start and end.
    assert agent.runner.wait(timeout=60)
    agent.batcher.flush()
    assert server.wait_for(
        lambda: any(
            e["data"]["job"]["state"] in ("succeeded", "failed", "cancelled")
            for e in _events(server, "job")
        ),
        timeout=15,
    )

    jobs = [e["data"]["job"] for e in _events(server, "job")]
    assert jobs[0]["id"] == job_id
    assert jobs[-1]["state"] == "succeeded", jobs[-1].get("error")

    # Log arrived batched, not one frame per line.
    logs = _events(server, "log")
    assert logs, "no log events were emitted"
    total_lines = sum(len(e["data"]["lines"]) for e in logs)
    assert total_lines > 50, f"only {total_lines} lines streamed"
    assert len(logs) < total_lines, "batching should mean fewer frames than lines"

    # Sequence numbers are contiguous across batches - this is what lets the
    # panel distinguish an in-order append from a gap.
    seqs = [line["i"] for e in logs for line in e["data"]["lines"]]
    assert seqs == list(range(len(seqs))), "log sequence must be gapless and ordered"
    for event in logs:
        assert event["data"]["seq"] == event["data"]["lines"][0]["i"]


def test_a_build_can_be_cancelled_over_the_wire(wired, paths, monkeypatch):
    import os

    agent, server = wired
    # The autouse fixture makes dry-run builds instant, which means the job can
    # finish before the cancel arrives. Slow it down so this tests cancellation
    # rather than scheduling luck.
    monkeypatch.setattr("klipper_updater.build.FAKE_BUILD_DELAY", 0.03)

    with open(paths.settings_file, "w", encoding="utf-8") as fh:
        fh.write("[updater]\ndry_run = true\nservice_backend = null\n")
    os.makedirs(paths.type_dir("bttebb36"), exist_ok=True)
    with open(paths.config_file("bttebb36", "klipper"), "w", encoding="utf-8") as fh:
        fh.write("CONFIG_MACH_STM32=y\n")

    _run(agent)
    assert server.wait_for(lambda: server.methods_called("server.connection.identify"))

    server.send(
        {
            "jsonrpc": "2.0",
            "id": 600,
            "method": "fw.build",
            "params": {"name": "bttebb36", "fw": "klipper"},
        }
    )
    assert server.wait_for(lambda: any(r["id"] == 600 for r in server.responses))

    job_id = reply_job_id(server, 600)
    server.send(
        {"jsonrpc": "2.0", "id": 601, "method": "fw.job.cancel", "params": {"job_id": job_id}}
    )
    assert server.wait_for(lambda: any(r["id"] == 601 for r in server.responses))
    cancel_reply = next(r for r in server.responses if r["id"] == 601)
    assert "error" not in cancel_reply, cancel_reply
    assert cancel_reply["result"]["cancelling"] is True
    assert cancel_reply["result"]["immediate"] is True, "a build stops straight away"

    assert agent.runner.wait(timeout=60)
    assert agent.runner.get(job_id).state == "cancelled"


def reply_job_id(server: FakeMoonraker, request_id: int) -> str:
    return next(r for r in server.responses if r["id"] == request_id)["result"]["job_id"]


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reserved", RESERVED_EVENTS)
def test_reserved_event_names_are_refused_locally(reserved):
    """Moonraker rejects these; emitting one would be a silent no-op plus an
    error on its side. The panel relies on Moonraker's own versions."""
    emitter = EventEmitter(lambda: None)
    with pytest.raises(ValueError):
        emitter.emit(reserved)


def test_emitting_while_disconnected_is_a_no_op_not_an_error():
    """A build in progress must not fail because moonraker restarted."""
    assert EventEmitter(lambda: None).emit("state", {"x": 1}) is False


def test_emitting_survives_a_broken_peer():
    class Broken:
        connected = True

        def notify(self, *a, **kw):
            raise OSError("socket closed under us")

    assert EventEmitter(lambda: Broken()).emit("state", {}) is False
