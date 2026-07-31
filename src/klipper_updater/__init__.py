"""Klipper/Katapult firmware management.

Layered deliberately so the same logic serves both the interactive CLI and a
long-running Moonraker agent:

  errors/paths/settings/config/devices  - pure, no side effects beyond the FS
  lock/build/flash/service              - do real work, report via a callback
  cli/tui                               - the ONLY place input()/sys.exit()/
                                          print() are allowed to appear

Anything importable by the agent must never block on a terminal.
"""

from __future__ import annotations

__version__ = "0.9.0"

# Bumped only on a breaking change to the agent's JSON-RPC surface. The Mainsail
# panel refuses to render if it sees an API version it doesn't know.
API_VERSION = 1

AGENT_NAME = "klipper_updater"

__all__ = ["__version__", "API_VERSION", "AGENT_NAME"]
