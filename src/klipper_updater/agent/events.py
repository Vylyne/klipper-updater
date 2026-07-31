"""Pushing events out to every connected Moonraker client.

An agent emits with ``connection.send_event``; clients receive it as a
``notify_agent_event`` notification whose params are a *list* containing one
object::

    [{"agent": "klipper_updater", "event": "state", "data": {...}}]

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
    ) -> None:
        self.paths = paths
        self.emitter = emitter
        self._serialize = serialize
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
                    self.emitter.emit("bus", {"devices": self._serialize(found)})
            except Exception as exc:  # noqa: BLE001 - a watcher must not die
                if self._log is not None:
                    self._log.warning(f"bus poll failed: {exc}")

            interval = self.busy_interval if self._busy.is_set() else self.idle_interval
            self._wake.wait(interval)
            self._wake.clear()
