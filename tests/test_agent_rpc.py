"""The JSON-RPC peer, driven over a real socketpair.

socket.socketpair() works on Windows in CPython, so these run everywhere and
exercise the actual reader thread and framing rather than a mock.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from klipper_updater.agent.rpc import (
    ERR_INVALID_PARAMS,
    ERR_METHOD_NOT_FOUND,
    ETX,
    MethodNotFound,
    MoonrakerPeer,
    RpcError,
    frame,
    unframe,
)
from klipper_updater.errors import UnknownTypeError

# --------------------------------------------------------------------------
# framing, in isolation
# --------------------------------------------------------------------------


def test_frame_terminates_with_etx_not_a_newline():
    """Moonraker's socket is ETX-delimited. A newline-framed client sees nothing."""
    data = frame({"jsonrpc": "2.0", "method": "x"})
    assert data.endswith(ETX)
    assert not data.endswith(b"\n")
    assert data.count(ETX) == 1


def test_unframe_splits_multiple_messages_in_one_read():
    buf = bytearray(b'{"a":1}' + ETX + b'{"b":2}' + ETX)
    assert unframe(buf) == [b'{"a":1}', b'{"b":2}']
    assert buf == bytearray()


def test_unframe_keeps_a_partial_tail_for_the_next_read():
    buf = bytearray(b'{"a":1}' + ETX + b'{"par')
    assert unframe(buf) == [b'{"a":1}']
    assert bytes(buf) == b'{"par'
    buf.extend(b'tial":2}' + ETX)
    assert unframe(buf) == [b'{"partial":2}']


def test_unframe_returns_nothing_when_no_terminator_yet():
    buf = bytearray(b'{"incomplete":')
    assert unframe(buf) == []
    assert bytes(buf) == b'{"incomplete":'


def test_unframe_drops_empty_segments():
    """A stray or doubled ETX must not look like a parse error."""
    buf = bytearray(ETX + b'{"a":1}' + ETX + ETX)
    assert unframe(buf) == [b'{"a":1}']


# --------------------------------------------------------------------------
# peer over a socketpair
# --------------------------------------------------------------------------


class FakeMoonraker:
    """The far end of a socketpair, speaking the same framing."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buffer = bytearray()
        # Parsed but not yet handed out. unframe() drains *every* complete
        # message from the buffer, so anything beyond the one we return has to be
        # kept - dropping it made the next read block on data that had already
        # arrived, which is precisely the coalescing case these tests cover.
        self.parsed: list[dict] = []

    def read_message(self, timeout: float = 5.0) -> dict:
        self.sock.settimeout(timeout)
        while True:
            if self.parsed:
                return self.parsed.pop(0)
            for raw in unframe(self.buffer):
                self.parsed.append(json.loads(raw))
            if self.parsed:
                continue
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("agent closed the socket")
            self.buffer.extend(chunk)

    def send(self, payload: dict) -> None:
        self.sock.sendall(frame(payload))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)


def test_the_harness_itself_does_not_drop_coalesced_messages():
    """Regression test for the harness, not the peer.

    read_message() used to return unframe()[0] and discard the rest, so two
    replies arriving in one recv() lost the second - a flake that only showed up
    when the kernel happened to coalesce the writes. Driven by pre-filling the
    buffer so it fails deterministically rather than by luck.
    """
    class _StubSocket:
        """Only settimeout is reached; recv would mean the queue logic failed."""

        def settimeout(self, _timeout):
            pass

        def recv(self, _size):
            raise AssertionError("read_message hit the socket instead of the queue")

    harness = FakeMoonraker.__new__(FakeMoonraker)
    harness.sock = _StubSocket()
    harness.buffer = bytearray(
        frame({"jsonrpc": "2.0", "id": 1, "result": "first"})
        + frame({"jsonrpc": "2.0", "id": 2, "result": "second"})
    )
    harness.parsed = []

    assert harness.read_message()["id"] == 1
    assert harness.read_message()["id"] == 2


@pytest.fixture
def pair():
    a, b = socket.socketpair()
    try:
        yield a, b
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


@pytest.fixture
def peer_and_server(pair):
    agent_sock, server_sock = pair
    handled: list[tuple[str, object]] = []

    def on_request(method, params):
        handled.append((method, params))
        if method == "boom":
            raise RuntimeError("handler exploded")
        if method == "unknown":
            raise MethodNotFound(method)
        if method == "typed":
            raise UnknownTypeError("no such type", type="nope", known=["a"])
        return {"echo": method, "params": params}

    notified: list[tuple[str, object]] = []
    peer = MoonrakerPeer(
        "unused",
        on_request=on_request,
        on_notify=lambda m, p: notified.append((m, p)),
        transport=agent_sock,
    )
    peer.connect()
    server = FakeMoonraker(server_sock)
    try:
        yield peer, server, handled, notified
    finally:
        peer.close()


def test_outbound_call_gets_its_response(peer_and_server):
    peer, server, _, _ = peer_and_server
    result: list = []

    t = threading.Thread(target=lambda: result.append(peer.call("server.info")))
    t.start()

    req = server.read_message()
    assert req["jsonrpc"] == "2.0"
    assert req["method"] == "server.info"
    assert isinstance(req["id"], int)
    server.send({"jsonrpc": "2.0", "id": req["id"], "result": {"ok": True}})

    t.join(timeout=5)
    assert result == [{"ok": True}]


def test_outbound_call_surfaces_an_error_response(peer_and_server):
    peer, server, _, _ = peer_and_server
    box: list = []

    def go():
        try:
            peer.call("nope")
        except RpcError as exc:
            box.append(exc)

    t = threading.Thread(target=go)
    t.start()
    req = server.read_message()
    server.send(
        {
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32601, "message": "Method not found"},
        }
    )
    t.join(timeout=5)
    assert len(box) == 1
    assert box[0].code == -32601


def test_notify_sends_no_id(peer_and_server):
    peer, server, _, _ = peer_and_server
    peer.notify("connection.send_event", {"event": "state", "data": {"x": 1}})
    msg = server.read_message()
    assert msg["method"] == "connection.send_event"
    assert "id" not in msg


def test_inbound_request_is_served(peer_and_server):
    peer, server, handled, _ = peer_and_server
    server.send({"jsonrpc": "2.0", "method": "fw.ping", "params": {"a": 1}, "id": 7})
    resp = server.read_message()
    assert resp["id"] == 7
    assert resp["result"] == {"echo": "fw.ping", "params": {"a": 1}}
    assert handled == [("fw.ping", {"a": 1})]


def test_a_raising_handler_still_produces_exactly_one_response(peer_and_server):
    """The single most important property in this module.

    Moonraker awaits our reply with no timeout, so a missing response wedges the
    calling client's HTTP request permanently.
    """
    peer, server, _, _ = peer_and_server
    server.send({"jsonrpc": "2.0", "method": "boom", "id": 11})
    resp = server.read_message()
    assert resp["id"] == 11
    assert "error" in resp
    assert "exploded" in resp["error"]["message"]

    # And the connection is still usable afterwards.
    server.send({"jsonrpc": "2.0", "method": "fine", "id": 12})
    assert server.read_message()["id"] == 12


def test_unknown_method_reports_method_not_found(peer_and_server):
    peer, server, _, _ = peer_and_server
    server.send({"jsonrpc": "2.0", "method": "unknown", "id": 3})
    resp = server.read_message()
    assert resp["error"]["code"] == ERR_METHOD_NOT_FOUND


def test_a_typed_updater_error_carries_its_stable_code(peer_and_server):
    """The panel switches on error.data.code, not on English prose."""
    peer, server, _, _ = peer_and_server
    server.send({"jsonrpc": "2.0", "method": "typed", "id": 4})
    resp = server.read_message()
    assert resp["error"]["data"]["code"] == "unknown_type"
    assert resp["error"]["data"]["data"]["known"] == ["a"]


def test_notifications_reach_the_notify_handler(peer_and_server):
    peer, server, _, notified = peer_and_server
    server.send({"jsonrpc": "2.0", "method": "notify_service_state_changed", "params": [{}]})
    deadline = time.monotonic() + 5
    while not notified and time.monotonic() < deadline:
        time.sleep(0.01)
    assert notified and notified[0][0] == "notify_service_state_changed"


def test_a_message_split_across_two_writes_is_reassembled(peer_and_server):
    """TCP/unix streams don't preserve write boundaries."""
    peer, server, handled, _ = peer_and_server
    whole = frame({"jsonrpc": "2.0", "method": "fw.ping", "id": 21})
    server.send_raw(whole[:9])
    time.sleep(0.05)
    server.send_raw(whole[9:])
    assert server.read_message()["id"] == 21


def test_two_messages_in_one_write_are_both_served(peer_and_server):
    peer, server, _, _ = peer_and_server
    server.send_raw(
        frame({"jsonrpc": "2.0", "method": "a", "id": 31})
        + frame({"jsonrpc": "2.0", "method": "b", "id": 32})
    )
    ids = {server.read_message()["id"], server.read_message()["id"]}
    assert ids == {31, 32}


def test_unparseable_input_is_discarded_without_killing_the_reader(peer_and_server):
    peer, server, _, _ = peer_and_server
    server.send_raw(b"this is not json" + ETX)
    server.send({"jsonrpc": "2.0", "method": "still-alive", "id": 41})
    assert server.read_message()["id"] == 41


def test_positional_params_are_rejected_clearly(peer_and_server):
    """Moonraker relays whatever `arguments` the caller passed, including a list."""
    from klipper_updater.agent.methods import Api
    from klipper_updater.paths import Paths

    api = Api(Paths.from_env(env={"KLIPPER_UPDATER_HOME": "/tmp/x"}))
    with pytest.raises(RpcError) as exc:
        api.dispatch("fw.ping", ["positional"])
    assert exc.value.code == ERR_INVALID_PARAMS


def test_pending_calls_fail_when_the_socket_drops(pair):
    """Otherwise a caller blocks forever on a reply that can never arrive."""
    agent_sock, server_sock = pair
    peer = MoonrakerPeer("unused", on_request=lambda m, p: None, transport=agent_sock)
    peer.connect()

    box: list = []

    def go():
        try:
            peer.call("server.info", timeout=10)
        except RpcError as exc:
            box.append(exc)

    t = threading.Thread(target=go)
    t.start()
    time.sleep(0.2)
    server_sock.close()
    t.join(timeout=5)
    assert len(box) == 1


def test_wait_closed_returns_when_the_far_end_hangs_up(pair):
    agent_sock, server_sock = pair
    peer = MoonrakerPeer("unused", on_request=lambda m, p: None, transport=agent_sock)
    peer.connect()
    assert peer.connected
    server_sock.close()
    assert peer.wait_closed(timeout=5) is True
    assert not peer.connected
