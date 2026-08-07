"""``python -m mcu_updater.agent`` - the systemd entry point."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
from typing import Optional

from .. import AGENT_NAME, __version__
from ..errors import UpdaterError
from ..layout import migrate_type_dirs
from ..paths import Paths
from .service import Agent, wait_for_socket


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcu-updater",
        description="Moonraker agent exposing mcu-updater to Mainsail",
    )
    p.add_argument("--socket", default=None, help="Path to moonraker.sock")
    p.add_argument("--log", default=None, help="Log file (default: stderr only)")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    p.add_argument(
        "--wait-for-socket",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="On startup, wait this long for the socket to appear (0 to skip)",
    )
    p.add_argument(
        "--shutdown-grace",
        type=float,
        default=280.0,
        metavar="SECONDS",
        help="On SIGTERM, wait this long for an in-progress flash to finish before "
        "exiting. Must be less than the unit's TimeoutStopSec.",
    )
    # AGENT_NAME is the Moonraker protocol identity, not the product name -
    # report the latter here so it matches the CLI and the systemd unit.
    p.add_argument("--version", action="version", version=f"mcu-updater {__version__}")
    return p


def setup_logging(path: Optional[str], verbose: bool) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("mcu_updater")
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(fmt)
    root.addHandler(stderr)

    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Rotating, because this runs forever on a machine with an SD card.
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:
            root.warning(f"could not open log file {path}: {exc}")
    return root


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    log = setup_logging(args.log, args.verbose)

    paths = Paths.from_env()
    sock = args.socket or paths.moonraker_sock

    log.info(f"{AGENT_NAME} {__version__} starting (socket={sock})")

    # Logged and survived rather than fatal: the only way this refuses is a
    # name collision a human has to resolve, and exiting would put us in a
    # systemd restart loop that takes the whole panel - including the status
    # that would explain the problem - offline. A build then fails loudly with
    # ConfigNotFoundError naming the path it looked in.
    try:
        moved = migrate_type_dirs(paths)
    except UpdaterError as exc:
        log.error(f"could not gather per-type config under {paths.type_root}: {exc}")
    else:
        if moved:
            log.info(f"moved per-type config into {paths.type_root}: {', '.join(moved)}")

    if args.wait_for_socket and not wait_for_socket(sock, args.wait_for_socket):
        # Not fatal: run_forever retries anyway. This just avoids a burst of
        # noisy failures on a cold boot, where `After=moonraker.service` orders
        # process start but not readiness.
        log.warning(f"socket {sock} still absent after {args.wait_for_socket:.0f}s; retrying")

    agent = Agent(paths, socket_path=sock, logger=log)

    # Covers `kill -9` on a previous run: if it died with klipper stopped, the
    # journal says so and klipper gets started before we do anything else.
    agent.reconcile_startup()

    def _shutdown(signum: int, _frame: object) -> None:
        log.info(f"received signal {signum}, shutting down")
        # Off the handler thread: request_stop may block for minutes waiting for a
        # flash to finish, and a signal handler must return promptly.
        threading.Thread(
            target=agent.request_stop,
            args=(args.shutdown_grace,),
            name="shutdown",
            daemon=True,
        ).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (OSError, ValueError):  # pragma: no cover - not all platforms
            pass

    try:
        agent.run_forever()
    except KeyboardInterrupt:  # pragma: no cover
        agent.request_stop(args.shutdown_grace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
