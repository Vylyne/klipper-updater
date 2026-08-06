"""Remembering which type a freshly-bootloadered board was meant to become.

`add_mcu.start` waits a short while for the board to come back as Katapult and
then reports it. When it does come back in time, this file is not needed - the
job already knows what it is. This exists for every other case:

* the board takes longer than the wait (a marginal port, a chain of hubs);
* it is unplugged after flashing and brought back later;
* the agent restarted in between.

In all of those the board arrives as an anonymous untracked device and the
intent - "this is a bttebb36" - is lost, even though the user stated it clearly
a minute earlier.

**Keyed on the DFU serial**, because that is the only identifier that exists on
both sides of the transition. A board in DFU has no ``/dev/serial/by-id`` name at
all, and the name it gets afterwards is not knowable in advance - but the reverse
*is* computable (`devices.dfu_serial_for`), so a board appearing later can be
matched back to what we wrote to it.

Deliberately expiring: a pairing that could still act a month later would be a
surprise, and surprise is the one thing an automatic registry edit must not be.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from .paths import Paths

#: How long a pairing stays actionable. Long enough to cover "flash it now, plug
#: it into the toolhead this evening", short enough that a board found in a
#: drawer next month is treated as the stranger it has become.
PAIRING_TTL = 24 * 3600.0


class Pairings:
    """DFU serial -> the type its bootloader was installed for."""

    def __init__(self, paths: Paths, ttl: float = PAIRING_TTL) -> None:
        self.paths = paths
        self.ttl = ttl

    # -- storage -----------------------------------------------------------

    def all(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.paths.pairings_file, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self.paths.pairings_file), exist_ok=True)
        tmp = self.paths.pairings_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.paths.pairings_file)

    # -- use ---------------------------------------------------------------

    def record(self, dfu_serial: str, mcu_type: str) -> None:
        """Note that `dfu_serial` was just given `mcu_type`'s bootloader.

        Called immediately after the write and *before* the re-enumeration wait,
        which is the whole point: the cases this exists for are exactly the ones
        where that wait does not succeed.
        """
        if not dfu_serial:
            return
        data = self.all()
        data[dfu_serial] = {"type": mcu_type, "at": time.time()}
        self._write(data)

    def type_for(self, dfu_serial: str) -> Optional[str]:
        """The type this board was bootloadered as, if recent enough."""
        entry = self.all().get(dfu_serial)
        if not entry:
            return None
        at = entry.get("at")
        if not isinstance(at, (int, float)) or (time.time() - at) > self.ttl:
            return None
        mcu_type = entry.get("type")
        return mcu_type if isinstance(mcu_type, str) and mcu_type else None

    def forget(self, dfu_serial: str) -> None:
        """Drop a pairing once it has been acted on, so it cannot act twice."""
        data = self.all()
        if data.pop(dfu_serial, None) is not None:
            self._write(data)

    def prune(self) -> int:
        """Drop everything past its TTL. Returns how many went."""
        data = self.all()
        now = time.time()
        keep = {
            key: entry
            for key, entry in data.items()
            if isinstance(entry.get("at"), (int, float)) and (now - entry["at"]) <= self.ttl
        }
        if len(keep) != len(data):
            self._write(keep)
        return len(data) - len(keep)
