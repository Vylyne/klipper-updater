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

#: How long to wait before believing a failed lock attempt. A genuine holder
#: keeps the lock for minutes, so one short retry costs nothing - and it closes
#: the microscopic window in which `holder()`'s own probe is the thing holding
#: it, which would otherwise report a spurious "busy" roughly never, i.e. at the
#: worst possible moment.
ACQUIRE_RETRY_DELAY = 0.05


def _probe_and_clear_if_free(path: str) -> bool:
    """Is the lock genuinely unheld? Clears the stale record if so.

    Deliberately a flock probe rather than a liveness check on the recorded pid:
    flock is the authority on whether the lock is held, whereas a pid can be
    reused and `os.kill(pid, 0)` is not portable - on Windows CPython implements
    `os.kill` via TerminateProcess, so signal 0 would *kill* the process rather
    than ask after it.

    Returns False when someone holds it, or when we cannot tell (no file, or no
    flock at all as on Windows) - the conservative answer, since claiming a held
    lock is free is how two flashes end up running at once.
    """
    # Nested inside the platform check rather than an early return, so a type
    # checker running on Windows narrows sys.platform and does not flag the
    # POSIX-only calls - the same reason the import above is written this way.
    if sys.platform != "win32":
        try:
            fh = open(path, "r+", encoding="utf-8")
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False  # someone really has it
            # We got it, so whatever is recorded is gone. Clear it while still
            # holding the lock, so the phantom cannot come back and the panel
            # recovers without needing a restart.
            try:
                fh.seek(0)
                fh.truncate()
                fh.flush()
            except OSError:
                pass
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return True
        finally:
            fh.close()
    return False


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

    def _record(self) -> Optional[dict[str, Any]]:
        """Whatever is written in the file, without judging whether it's current."""
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("pid"):
            return None
        return data

    def holder(self) -> Optional[dict[str, Any]]:
        """Who holds the lock *right now*. None if free or unknown.

        This has to probe the lock, not just read the file. ``release()``
        truncates the record, but a SIGKILLed holder never gets to run it - and
        while the kernel does drop the flock itself, the ``{pid, label, since}``
        payload stays on disk. Reporting that verbatim was exactly the stale
        pidfile failure this module's docstring claims to avoid: killing the
        agent mid-flash left the panel insisting "a firmware operation is
        running on the host" forever, surviving both an agent restart and a
        browser reload, because the phantom lived in a file rather than in
        anyone's memory.

        Found by the kill -9 failure-injection test. The old test only proved a
        *new* acquire succeeded afterwards, which it did - it never asked what
        ``holder()`` said in the meantime.
        """
        data = self._record()
        if data is None:
            return None
        # A successful probe means nobody holds it, so the record is a leftover.
        if _probe_and_clear_if_free(self.path):
            return None
        return data

    def _busy(self) -> BusyError:
        """Build a BusyError naming the incumbent, when we can identify it.

        Reads the raw record rather than going through `holder()`: we only get
        here because the flock was genuinely contended, so there is nothing to
        verify - and if the incumbent released in the moment between our failed
        acquire and this call, `holder()` would return None and we would lose the
        name for no benefit. Naming who wrote the record is best-effort by design.
        """
        held = self._record() or {}
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
            # One retry: a real holder keeps this for minutes, so the only thing
            # a 50ms wait can lose to is holder()'s momentary probe lock.
            for attempt in (0, 1):
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if attempt == 0:
                        time.sleep(ACQUIRE_RETRY_DELAY)
                        continue
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
