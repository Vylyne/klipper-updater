"""Pushing events out to every connected Moonraker client.

An agent emits with ``connection.send_event``; clients receive it as a
``notify_agent_event`` notification whose params are a *list* containing one
object::

    [{"agent": "mcu_updater", "event": "state", "data": {...}}]

``connected`` and ``disconnected`` are reserved - Moonraker emits those itself,
with the agent's identify payload, and rejects any attempt to send them. That is
deliberately what the panel uses for availability detection, so don't fake them.

Emission is always best-effort. A dropped event must never fail an operation:
during a Moonraker restart the socket is gone, but a build or flash in progress
has to carry on regardless.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from ..devices import BusDevice
from ..paths import Paths

#: Reserved by Moonraker; sending these raises on its side.
RESERVED_EVENTS = ("connected", "disconnected")


class EventEmitter:
    """Fire-and-forget event publisher."""

    def __init__(self, peer_getter: Callable[[], Any], logger: Any = None) -> None:
        # A getter rather than the peer itself: the peer is replaced on every
        # reconnect, and the emitter outlives any single connection.
        self._peer_getter = peer_getter
        self._log = logger

    def emit(self, event: str, data: Optional[dict] = None) -> bool:
        """Publish one event. Returns False if it could not be sent."""
        if event in RESERVED_EVENTS:
            raise ValueError(f"'{event}' is reserved by Moonraker and cannot be emitted")
        peer = self._peer_getter()
        if peer is None or not peer.connected:
            return False
        params: dict[str, Any] = {"event": event}
        if data is not None:
            params["data"] = data
        try:
            peer.notify("connection.send_event", params)
            return True
        except Exception as exc:  # noqa: BLE001 - never let telemetry break work
            if self._log is not None:
                self._log.debug(f"could not emit '{event}': {exc}")
            return False


class LogBatcher:
    """Coalesces job log lines into periodic `log` events.

    A Klipper build emits 400-800 lines in a few seconds. One event per line means
    that many broadcast websocket frames to *every* connected client - the Mainsail
    tab, a phone, KlipperScreen - which is enough to make the UI stutter on a Pi.
    Batching caps it at roughly four frames a second.

    Flushes on whichever comes first: 250 ms elapsed, 40 lines, or 32 KiB. The byte
    trigger keeps a payload bounded even if the compiler emits a wall of errors.
    """

    FLUSH_INTERVAL = 0.25
    MAX_LINES = 40
    MAX_BYTES = 32 * 1024
    #: One pathological line shouldn't consume the whole payload budget.
    MAX_LINE_BYTES = 8 * 1024

    def __init__(self, emitter: EventEmitter, logger: Any = None) -> None:
        self.emitter = emitter
        self._log = logger
        self._lock = threading.Lock()
        self._job_id: Optional[str] = None
        self._pending: list[dict] = []
        self._first_seq: Optional[int] = None
        self._bytes = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick, name="log-batcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        self.flush()

    def add(self, job: Any, line: Any) -> None:
        """Queue one line. Called from the job worker thread."""
        text = line.text
        if len(text) > self.MAX_LINE_BYTES:
            text = text[: self.MAX_LINE_BYTES] + " ...[truncated]"

        with self._lock:
            if self._job_id is not None and self._job_id != job.id:
                # Lines from two different jobs must never share a batch.
                self._flush_locked()
            self._job_id = job.id
            if self._first_seq is None:
                self._first_seq = line.seq
            self._pending.append({"i": line.seq, "s": line.stream, "t": text})
            self._bytes += len(text) + 24  # rough per-line envelope overhead
            if len(self._pending) >= self.MAX_LINES or self._bytes >= self.MAX_BYTES:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._pending or self._job_id is None:
            return
        payload = {
            "job_id": self._job_id,
            # Sequence of the FIRST line in this batch. The client's next expected
            # index is seq + len(lines); anything else is a gap, and it resyncs
            # via fw.job.get. Without this a streaming log silently lies after a
            # dropped frame or a page reload.
            "seq": self._first_seq,
            "lines": self._pending,
        }
        self._pending = []
        self._first_seq = None
        self._bytes = 0
        self.emitter.emit("log", payload)

    def _tick(self) -> None:
        while not self._stop.wait(self.FLUSH_INTERVAL):
            try:
                self.flush()
            except Exception as exc:  # noqa: BLE001 - a flusher must not die
                if self._log is not None:
                    self._log.debug(f"log flush failed: {exc}")


def _fingerprint(devices: list[BusDevice]) -> tuple:
    """A comparable snapshot, so we only emit when the bus actually changes."""
    return tuple(sorted((d.fw.lower(), d.chipset, d.serial) for d in devices))


class BusWatcher:
    """Polls /dev/serial/by-id and emits `bus` when it changes.

    Polling rather than inotify/udev: the entries are udev symlinks, the set is
    tiny, and a poll has no dependencies. The interval is adaptive because during
    a flash a board disappears and reappears within seconds and the UI should
    track it, while idle there is nothing to see.
    """

    def __init__(
        self,
        paths: Paths,
        emitter: EventEmitter,
        serialize: Callable[[list[BusDevice]], Any],
        *,
        idle_interval: float = 15.0,
        busy_interval: float = 2.0,
        logger: Any = None,
        on_change: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.paths = paths
        self.emitter = emitter
        self._serialize = serialize
        #: Run when the set of devices changes, before the event goes out. Used
        #: to adopt a board that has finally turned up from a bootloader install,
        #: so the `bus` event already reflects it rather than showing it as
        #: untracked for one poll and tracked the next.
        self._on_change = on_change
        self.idle_interval = idle_interval
        self.busy_interval = busy_interval
        self._log = logger

        self._busy = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last: Optional[tuple] = None

    def set_busy(self, busy: bool) -> None:
        """Speed up polling while an operation is running."""
        if busy:
            self._busy.set()
        else:
            self._busy.clear()
        self._wake.set()

    def poke(self) -> None:
        """Force an immediate check, e.g. right after a flash."""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bus-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    def reset(self) -> None:
        """Forget the last snapshot, so the next poll re-emits.

        Used after a reconnect: clients that joined while we were disconnected
        have no state, and suppressing the event as "unchanged" would leave them
        with an empty device list.
        """
        self._last = None

    def _loop(self) -> None:
        from .. import devices as devices_mod

        while not self._stop.is_set():
            try:
                found = devices_mod.scan(self.paths)
                fp = _fingerprint(found)
                if fp != self._last:
                    self._last = fp
                    if self._on_change is not None:
                        try:
                            self._on_change()
                        except Exception as exc:  # noqa: BLE001 - never kill the watcher
                            if self._log is not None:
                                self._log.warning(f"bus change handler failed: {exc}")
                    self.emitter.emit("bus", {"devices": self._serialize(found)})
            except Exception as exc:  # noqa: BLE001 - a watcher must not die
                if self._log is not None:
                    self._log.warning(f"bus poll failed: {exc}")

            interval = self.busy_interval if self._busy.is_set() else self.idle_interval
            self._wake.wait(interval)
            self._wake.clear()
