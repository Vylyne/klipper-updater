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
from ..jobs import IMMEDIATELY_CANCELLABLE, Job, JobRunner
from ..paths import Paths
from ..settings import Settings, load_settings
from .events import BusWatcher, EventEmitter, LogBatcher
from .methods import Api
from .rpc import MoonrakerPeer, RpcError

PROJECT_URL = "https://github.com/Vylyne/mcu-updater"

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
        self.batcher = LogBatcher(self.emitter, logger=self.log)
        self.runner = JobRunner(
            paths,
            self._settings,
            on_job_change=self._on_job_change,
            on_log_line=self.batcher.add,
            logger=self.log,
        )
        self.api = Api(
            paths,
            call=self._call,
            runner=self.runner,
            logger=self.log,
            # A registry edit from one browser tab has to reach every other client,
            # and the bus poll alone would not do it - the devices on the bus have
            # not changed, only who tracks them.
            on_change=self.emit_state,
        )
        self.watcher = BusWatcher(
            paths,
            self.emitter,
            serialize=lambda devices: self.api.bus(self.api.registry()),
            logger=self.log,
            # A board that took longer to enumerate than add_mcu's wait turns up
            # here instead. Adopting it on the same tick means the bus event the
            # panel receives already shows it under its type.
            on_change=self.api.adopt_paired,
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

    def _settings(self) -> Settings:
        try:
            return load_settings(self.paths.settings_file)
        except Exception:  # noqa: BLE001 - a bad conf must not block a build
            return Settings()

    def _on_job_change(self, job: Job) -> None:
        """A job started, progressed, or finished."""
        # Any pending log lines belong before the state change that follows them,
        # or the UI shows "finished" above the last few output lines.
        self.batcher.flush()
        self.emitter.emit("job", {"job": job.to_dict()})

        # Poll the bus faster while work is happening: during a flash a board
        # disappears and comes back within seconds.
        self.watcher.set_busy(not job.is_terminal)

        if job.is_terminal:
            # Artifacts and staleness changed, so refresh the whole picture.
            self.watcher.poke()
            self.emit_state()

    def _on_notify(self, method: str, params: Any) -> None:
        # Moonraker broadcasts a lot; we only care about klipper's service state
        # changing, which is worth reflecting straight away rather than waiting
        # for the next status poll.
        if method == "notify_service_state_changed":
            self.emit_state()

    # -- lifecycle ---------------------------------------------------------

    def request_stop(self, timeout: float = 300.0) -> None:
        """Shut down, but never in the middle of a write.

        `systemctl restart mcu-updater` during a flash would otherwise kill
        flashtool part-way and leave a board with half an image. A build is safe
        to interrupt, so only the non-interruptible kinds defer. The unit's
        TimeoutStopSec must exceed this.
        """
        job = self.runner.current()
        if job is not None and job.kind not in IMMEDIATELY_CANCELLABLE:
            self.log.warning(
                f"deferring shutdown: {job.kind} job {job.id} is in progress and cannot "
                f"be safely interrupted (waiting up to {timeout:.0f}s)"
            )
            if not self.runner.wait(timeout):
                self.log.error(
                    f"{job.kind} job {job.id} did not finish within {timeout:.0f}s; "
                    f"shutting down anyway"
                )
            else:
                self.log.info(f"{job.kind} job finished; continuing shutdown")
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        self.watcher.stop()
        self.batcher.stop()
        peer = self._peer
        if peer is not None:
            peer.close()

    def reconcile_startup(self) -> None:
        """If a previous run died with klipper stopped, start it back up.

        This is the layer that covers `kill -9` mid-flash, where no finally block
        and no fallback chain ever runs.
        """
        from ..service import make_controller, reconcile

        try:
            reconcile(
                self.paths,
                make_controller(self._settings()),
                reporter=lambda stream, line: self.log.warning(line),
            )
        except Exception as exc:  # noqa: BLE001 - must never block startup
            self.log.warning(f"could not reconcile a previous run: {exc}")

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
        methods = self.api.available_methods()
        for name in sorted(methods):
            peer.call("connection.register_remote_method", {"method_name": name})
        self.log.info(f"registered {len(methods)} methods")

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
        self.batcher.start()
        self.emit_state()

        # A job started before the socket dropped keeps running - it outlives the
        # connection deliberately - so tell the new clients about it.
        current = self.runner.current()
        if current is not None:
            self.emitter.emit("job", {"job": current.to_dict()})

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
