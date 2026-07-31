"""Cross-process exclusion for build/flash operations.

A file lock rather than an in-process mutex, because the CLI and the agent are
separate processes and both can build and flash. ``flock`` is released by the
kernel when the holding process dies, so there are no stale locks to clean up
after a crash - which is exactly the failure mode a pidfile gets wrong.

The lock file carries ``{pid, label, since}`` so a caller who loses the race can
say *who* is holding it rather than just "busy".
"""

from __future__ import annotations

import json
import os
import sys
import time
from types import TracebackType
from typing import Any, Optional

from .errors import BusyError
from .paths import Paths

# Written as a direct sys.platform comparison rather than via a helper flag so
# that type checkers narrow it and don't flag the POSIX-only calls below.
if sys.platform != "win32":
    import fcntl


class ExclusiveLock:
    """Non-blocking exclusive lock. Raises BusyError rather than waiting.

    Waiting would be worse than failing here: the operations being guarded take
    minutes and stop the printer's firmware, so a queued second one is a
    surprise, not a convenience.
    """

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.path = paths.lock_file
        self._fh: Optional[Any] = None
        self.label: Optional[str] = None

    def holder(self) -> Optional[dict[str, Any]]:
        """Best-effort read of who currently holds it. None if unknown/free."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("pid"):
            return None
        return data

    def _busy(self) -> BusyError:
        """Build a BusyError naming the incumbent, when we can identify it."""
        held = self.holder() or {}
        parts = ["another firmware operation is already running"]
        label = held.get("label")
        if label:
            parts.append(f" ({label})")
        since = held.get("since")
        if isinstance(since, (int, float)):
            parts.append(f", started {time.time() - since:.0f}s ago")
        parts.append(". Wait for it to finish.")
        return BusyError("".join(parts), holder=held)

    def acquire(self, label: str) -> ExclusiveLock:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Opened r+ when possible so a failed lock attempt doesn't truncate the
        # incumbent's holder record.
        fh = open(self.path, "a+", encoding="utf-8")
        if sys.platform != "win32":
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                raise self._busy() from None

        fh.seek(0)
        fh.truncate()
        json.dump({"pid": os.getpid(), "label": label, "since": time.time()}, fh)
        fh.flush()
        os.fsync(fh.fileno())
        self._fh = fh
        self.label = label
        return self

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            fh.truncate()
            fh.flush()
            if sys.platform != "win32":
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def __enter__(self) -> ExclusiveLock:
        if self._fh is None:
            raise RuntimeError("call acquire(label) before entering the lock")
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()


def exclusive(paths: Paths, label: str) -> ExclusiveLock:
    """``with exclusive(paths, "build klipper/bttebb36"):``"""
    return ExclusiveLock(paths).acquire(label)
