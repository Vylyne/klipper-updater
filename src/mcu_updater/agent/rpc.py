"""Bidirectional JSON-RPC 2.0 over Moonraker's unix socket.

Two things about this transport are easy to get wrong and expensive to debug.

**Framing is ETX, not newlines.** Moonraker's docs: *"Each JSON-RPC request must
be terminated with an ETX character (0x03)."* A single ``recv()`` may return
several messages, and one message may span several ``recv()`` calls, so the
reader buffers and splits rather than assuming one read is one message.

**An unanswered request hangs the caller forever.** Moonraker's
``Connection.call_method_with_response()`` creates a future and awaits it with no
deadline. If this agent fails to reply to an inbound request, the front end's HTTP
request never completes, and they accumulate. Every inbound request therefore
gets exactly one response, guaranteed by a ``BaseException`` handler - a wrong
answer is far better than no answer.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional

ETX = b"\x03"

# JSON-RPC 2.0 reserved codes, plus the -32000 application band.
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_APPLICATION = -32000

RequestHandler = Callable[[str, Any], Any]
NotifyHandler = Callable[[str, Any], None]


class RpcError(Exception):
    """An error returned by the peer, or a transport failure."""

    def __init__(self, message: str, code: int = ERR_APPLICATION, data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


class MethodNotFound(RpcError):
    def __init__(self, method: str) -> None:
        super().__init__(f"unknown method '{method}'", ERR_METHOD_NOT_FOUND)


def frame(payload: dict) -> bytes:
    """Encode one message for the wire."""
    return json.dumps(payload).encode("utf-8") + ETX


def unframe(buffer: bytearray) -> list[bytes]:
    """Pull every complete message out of `buffer`, leaving any partial tail.

    Mutates `buffer` in place. Empty segments (a stray ETX, or a keepalive) are
    dropped rather than treated as a parse error.
    """
    out: list[bytes] = []
    while True:
        idx = buffer.find(ETX)
        if idx < 0:
            return out
        chunk = bytes(buffer[:idx])
        del buffer[: idx + 1]
        if chunk.strip():
            out.append(chunk)


class MoonrakerPeer:
    """A connection to Moonraker that can both call and be called.

    `on_request` handles inbound requests: it receives ``(method, params)`` and
    either returns a JSON-serialisable result or raises. Raising anything is
    safe - it becomes a JSON-RPC error response.
    """

    def __init__(
        self,
        sock_path: str,
        on_request: RequestHandler,
        on_notify: Optional[NotifyHandler] = None,
        *,
        logger: Any = None,
        transport: Optional[Any] = None,
        max_workers: int = 4,
    ) -> None:
        self.sock_path = sock_path
        self._on_request = on_request
        self._on_notify = on_notify
        self._log = logger
        self._sock: Optional[Any] = transport
        self._owns_socket = transport is None

        self._next_id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, Future] = {}
        self._pending_lock = threading.Lock()

        self._reader: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._max_workers = max_workers

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed.is_set()

    def connect(self, timeout: float = 10.0) -> None:
        if self._sock is None:
            # Written as a direct sys.platform comparison so type checkers narrow
            # AF_UNIX, which does not exist on Windows. The agent only ever runs
            # on the Linux printer host; tests inject a socketpair instead.
            if sys.platform == "win32":
                raise RpcError(
                    "unix sockets are unavailable on Windows - pass transport= to "
                    "MoonrakerPeer instead of connecting"
                )
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self.sock_path)
            self._sock = sock
        # Blocking reads: the reader thread has nothing else to do, and a
        # timeout here would just turn into a busy loop.
        try:
            self._sock.settimeout(None)
        except OSError:
            pass

        self._stop.clear()
        self._closed.clear()
        self._pool = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="rpc-dispatch"
        )
        self._reader = threading.Thread(target=self._read_loop, name="rpc-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None and self._owns_socket:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self._closed.set()
        self._fail_pending("connection closed")
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False)

    def wait_closed(self, timeout: Optional[float] = None) -> bool:
        """Block until the connection drops. Returns True if it did."""
        return self._closed.wait(timeout)

    # -- outbound ----------------------------------------------------------

    def _allocate_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _send(self, payload: dict) -> None:
        sock = self._sock
        if sock is None:
            raise RpcError("not connected")
        data = frame(payload)
        with self._write_lock:
            try:
                sock.sendall(data)
            except OSError as exc:
                raise RpcError(f"write failed: {exc}") from exc

    def call(self, method: str, params: Any = None, timeout: float = 10.0) -> Any:
        """Send a request and wait for its response."""
        req_id = self._allocate_id()
        fut: Future = Future()
        with self._pending_lock:
            self._pending[req_id] = fut

        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": req_id}
        if params is not None:
            payload["params"] = params

        try:
            self._send(payload)
        except Exception:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise

        try:
            return fut.result(timeout=timeout)
        except TimeoutError as exc:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise RpcError(f"timed out after {timeout}s calling {method}") from exc

    def notify(self, method: str, params: Any = None) -> None:
        """Send a notification - no id, no reply expected."""
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    # -- inbound -----------------------------------------------------------

    def _read_loop(self) -> None:
        buffer = bytearray()
        try:
            while not self._stop.is_set():
                sock = self._sock
                if sock is None:
                    break
                chunk = sock.recv(65536)
                if not chunk:
                    self._debug("moonraker closed the socket")
                    break
                buffer.extend(chunk)
                for raw in unframe(buffer):
                    self._handle_raw(raw)
        except OSError as exc:
            if not self._stop.is_set():
                self._debug(f"socket error: {exc}")
        finally:
            self._closed.set()
            self._fail_pending("connection lost")

    def _handle_raw(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._debug(f"discarding unparseable message: {exc}")
            return
        if not isinstance(msg, dict):
            self._debug("discarding non-object message")
            return

        has_method = "method" in msg
        has_id = msg.get("id") is not None

        if has_method and has_id:
            # Inbound request. Dispatched off the reader thread so a slow handler
            # cannot stall the socket.
            pool = self._pool
            if pool is None:
                self._respond_error(msg["id"], ERR_INTERNAL, "agent is shutting down")
                return
            pool.submit(self._serve_request, msg)
        elif has_method:
            if self._on_notify is not None:
                try:
                    self._on_notify(msg["method"], msg.get("params"))
                except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the reader
                    self._warn(f"notification handler for {msg['method']} raised: {exc}")
        elif has_id:
            self._resolve(msg)
        else:
            self._debug("discarding message with neither method nor id")

    def _serve_request(self, msg: dict) -> None:
        """Run a handler and *always* write exactly one response.

        The blanket BaseException catch is deliberate. Moonraker awaits our reply
        with no timeout, so a missing response wedges the calling client's HTTP
        request permanently. Any answer beats none.
        """
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params")
        try:
            result = self._on_request(method, params)
        except RpcError as exc:
            self._respond_error(req_id, exc.code, exc.message, exc.data)
        except BaseException as exc:  # noqa: BLE001
            self._warn(f"handler for {method} raised {type(exc).__name__}: {exc}", exc_info=True)
            code = ERR_APPLICATION
            data = getattr(exc, "to_dict", None)
            self._respond_error(
                req_id,
                code,
                str(exc) or type(exc).__name__,
                data() if callable(data) else None,
            )
        else:
            self._respond_result(req_id, result)

    def _respond_result(self, req_id: Any, result: Any) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:  # noqa: BLE001
            self._warn(f"could not send response for id {req_id}: {exc}")

    def _respond_error(self, req_id: Any, code: int, message: str, data: Any = None) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "error": err})
        except Exception as exc:  # noqa: BLE001
            self._warn(f"could not send error for id {req_id}: {exc}")

    def _resolve(self, msg: dict) -> None:
        req_id = msg.get("id")
        if not isinstance(req_id, int):
            return
        with self._pending_lock:
            fut = self._pending.pop(req_id, None)
        if fut is None:
            self._debug(f"response for unknown id {req_id}")
            return
        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            if isinstance(err, dict):
                fut.set_exception(
                    RpcError(
                        str(err.get("message", "unknown error")),
                        int(err.get("code", ERR_APPLICATION)),
                        err.get("data"),
                    )
                )
            else:
                fut.set_exception(RpcError(str(err)))
        else:
            fut.set_result(msg.get("result"))

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(RpcError(reason))

    # -- logging -----------------------------------------------------------

    def _debug(self, message: str) -> None:
        if self._log is not None:
            self._log.debug(message)

    def _warn(self, message: str, exc_info: bool = False) -> None:
        if self._log is not None:
            self._log.warning(message, exc_info=exc_info)
