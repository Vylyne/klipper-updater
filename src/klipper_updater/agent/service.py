"""The long-running agent process.

Connect to Moonraker's unix socket, identify as an agent, register our methods,
then serve requests until the socket drops - and reconnect forever when it does.

Two properties matter more than anything else here:

* **Reconnect is unconditional.** Moonraker restarts (its own update manager does
  it), and the agent must come back without help.
* **Work outlives the connection.** In later phases a build or flash will be in
  flight when Moonraker restarts. Losing the socket must not abort it, which is
  why the connection is a replaceable field rather than the thing that owns
  state.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from .. import AGENT_NAME, __version__
from ..paths import Paths
from .events import BusWatcher, EventEmitter
from .methods import Api
from .rpc import MoonrakerPeer, RpcError

PROJECT_URL = "https://github.com/Vylyne/klipper-updater"

#: Reconnect backoff, in seconds. Caps rather than growing forever - if
#: Moonraker is down for an hour we still want to be back within 30s of it
#: returning.
BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)

log = logging.getLogger("klipper_updater.agent")


class Agent:
    def __init__(
        self,
        paths: Paths,
        *,
        socket_path: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        peer_factory: Optional[Any] = None,
    ) -> None:
        self.paths = paths
        self.socket_path = socket_path or paths.moonraker_sock
        self.log = logger or log

        # Injectable so the handshake can be tested over a socketpair without a
        # Moonraker. Signature: (on_request, on_notify) -> MoonrakerPeer.
        self._peer_factory = peer_factory

        self._peer: Optional[MoonrakerPeer] = None
        self._stop = threading.Event()

        self.emitter = EventEmitter(lambda: self._peer, logger=self.log)
        self.api = Api(paths, call=self._call, logger=self.log)
        self.watcher = BusWatcher(
            paths,
            self.emitter,
            serialize=lambda devices: self.api.bus(self.api.registry()),
            logger=self.log,
        )

    # -- outbound calls used by the Api for enrichment ---------------------

    def _call(self, method: str, params: Any = None, timeout: float = 1.5) -> Any:
        peer = self._peer
        if peer is None or not peer.connected:
            raise RpcError("not connected to moonraker")
        return peer.call(method, params, timeout=timeout)

    # -- inbound -----------------------------------------------------------

    def _on_request(self, method: str, params: Any) -> Any:
        self.log.debug(f"-> {method} {params!r}")
        return self.api.dispatch(method, params)

    def _on_notify(self, method: str, params: Any) -> None:
        # Moonraker broadcasts a lot; we only care about klipper's service state
        # changing, which is worth reflecting straight away rather than waiting
        # for the next status poll.
        if method == "notify_service_state_changed":
            self.emit_state()

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        self.watcher.stop()
        peer = self._peer
        if peer is not None:
            peer.close()

    def emit_state(self) -> None:
        try:
            self.emitter.emit("state", self.api.status({}))
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"could not emit state: {exc}")

    def _handshake(self, peer: MoonrakerPeer) -> None:
        res = peer.call(
            "server.connection.identify",
            {
                "client_name": AGENT_NAME,
                "version": __version__,
                "type": "agent",
                "url": PROJECT_URL,
            },
        )
        conn_id = res.get("connection_id") if isinstance(res, dict) else None
        self.log.info(f"identified with moonraker as '{AGENT_NAME}' (connection {conn_id})")

        # Registrations are per-connection and vanish on disconnect, so this runs
        # on every reconnect, not just at startup.
        for name in sorted(self.api.METHODS):
            peer.call("connection.register_remote_method", {"method_name": name})
        self.log.info(f"registered {len(self.api.METHODS)} methods")

    def run_once(self) -> None:
        """One connection lifetime: connect, serve, return when it drops."""
        if self._peer_factory is not None:
            peer = self._peer_factory(self._on_request, self._on_notify)
        else:
            peer = MoonrakerPeer(
                self.socket_path,
                on_request=self._on_request,
                on_notify=self._on_notify,
                logger=self.log,
            )
        peer.connect()
        self._peer = peer
        try:
            self._handshake(peer)
        except Exception:
            self._peer = None
            peer.close()
            raise

        # Clients that connected while we were away have no state, so re-emit
        # rather than letting the watcher suppress it as "unchanged".
        self.watcher.reset()
        self.watcher.start()
        self.emit_state()

        peer.wait_closed()
        self.log.info("moonraker connection closed")
        self._peer = None
        peer.close()

    def run_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self.run_once()
                attempt = 0  # a successful session resets the backoff
            except FileNotFoundError:
                self.log.warning(
                    f"{self.socket_path} does not exist yet - is moonraker running?"
                )
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                self.log.warning(f"connection failed: {exc}")

            if self._stop.is_set():
                break
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            self.log.debug(f"reconnecting in {delay:.0f}s")
            self._stop.wait(delay)

        self.watcher.stop()
        self.log.info("agent stopped")


def wait_for_socket(path: str, timeout: float = 0.0) -> bool:
    """Optionally wait for Moonraker's socket to appear before first connecting.

    Used by the systemd unit's startup path: `After=moonraker.service` only
    orders process start, not readiness, so on a cold boot the socket may not
    exist for a few seconds.
    """
    import os

    if timeout <= 0:
        return os.path.exists(path)
    deadline = time.monotonic() + timeout
    while True:
        if os.path.exists(path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)
