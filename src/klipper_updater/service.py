"""Stopping and starting Klipper around a flash, and surviving a crash.

Leaving klipper stopped is the one genuinely bad outcome this tool can produce:
the printer is dead until someone notices and SSHes in. Defence is layered.

1. ``klipper_stopped()`` restores state in a ``finally``.
2. ``MoonrakerService.start()`` falls back to systemd if Moonraker has gone away
   between the stop and the start.
3. A **journal** file records "we stopped it" before stopping, so a process that
   dies outright can be reconciled on next startup.
4. The systemd unit carries an ``ExecStopPost`` net for the case where even that
   doesn't run.

This module owns 1-3.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from collections.abc import Iterator
from typing import Any, Callable, Optional

from .build import Reporter, null_reporter
from .errors import PrintInProgressError, ServiceControlError
from .paths import Paths
from .settings import Settings


class ServiceController:
    """Interface for the three backends below."""

    name: str = "klipper"

    def stop(self, reporter: Reporter = null_reporter) -> None:
        raise NotImplementedError

    def start(self, reporter: Reporter = null_reporter) -> None:
        raise NotImplementedError

    def is_active(self) -> bool:
        raise NotImplementedError


class SystemdService(ServiceController):
    """`sudo systemctl <action> <unit>`, as the original did.

    Needs passwordless sudo for this one unit (install.sh offers to set that up).
    Used by the CLI, which has no Moonraker connection, and as the agent's
    last-resort fallback.
    """

    def __init__(self, name: str = "klipper") -> None:
        self.name = name

    def _run(self, action: str) -> int:
        try:
            res = subprocess.run(
                ["sudo", "systemctl", action, self.name],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return 1
        return res.returncode

    def stop(self, reporter: Reporter = null_reporter) -> None:
        reporter("info", f"Stopping {self.name} service...")
        if self._run("stop") != 0:
            reporter("warn", f"systemctl stop {self.name} did not report success")

    def start(self, reporter: Reporter = null_reporter) -> None:
        reporter("info", f"Starting {self.name} service...")
        if self._run("start") != 0:
            reporter("warn", f"systemctl start {self.name} did not report success")

    def is_active(self) -> bool:
        try:
            res = subprocess.run(
                ["systemctl", "is-active", "--quiet", self.name], timeout=20
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return res.returncode == 0


class MoonrakerService(ServiceController):
    """Service control via Moonraker's `machine.services.*` API.

    Preferred from the agent: no sudo needed (Moonraker is already privileged and
    gates this on moonraker.asvc, where klipper is allowed by default), and
    Moonraker emits notify_service_state_changed so Mainsail honestly shows
    "klipper: stopped" instead of dumping the user into a "lost connection"
    error state for the minutes a flash takes.

    ``call`` is injected - it's the agent's JSON-RPC peer - so this module stays
    free of transport concerns and testable.
    """

    def __init__(
        self,
        call: Callable[[str, dict], Any],
        name: str = "klipper",
        fallback: Optional[ServiceController] = None,
    ) -> None:
        self._call = call
        self.name = name
        self.fallback = fallback if fallback is not None else SystemdService(name)

    def stop(self, reporter: Reporter = null_reporter) -> None:
        reporter("info", f"Stopping {self.name} via Moonraker...")
        try:
            self._call("machine.services.stop", {"service": self.name})
            return
        except Exception as exc:  # noqa: BLE001 - any failure means try systemd
            reporter("warn", f"Moonraker stop failed ({exc}); falling back to systemctl")
        self.fallback.stop(reporter)

    def start(self, reporter: Reporter = null_reporter) -> None:
        """Belt and braces: this must succeed or the printer stays dead.

        If Moonraker died between our stop and this start, the API is
        unreachable, so every failure falls through to systemd.
        """
        reporter("info", f"Starting {self.name} via Moonraker...")
        try:
            self._call("machine.services.start", {"service": self.name})
            return
        except Exception as exc:  # noqa: BLE001
            reporter("warn", f"Moonraker start failed ({exc}); falling back to systemctl")
        self.fallback.start(reporter)

    def is_active(self) -> bool:
        return self.fallback.is_active()


class NullService(ServiceController):
    """Narrates instead of acting. Used by dry-run and tests.

    Tracks its own state rather than always claiming to be active, because
    klipper_stopped() verifies that a stop actually took effect - a NullService
    that lied about being up would make every dry run fail that check.
    """

    def __init__(self, name: str = "klipper") -> None:
        self.name = name
        self.actions: list[str] = []
        self._active = True

    def stop(self, reporter: Reporter = null_reporter) -> None:
        self.actions.append("stop")
        self._active = False
        reporter("info", f"[dry-run] would stop {self.name}")

    def start(self, reporter: Reporter = null_reporter) -> None:
        self.actions.append("start")
        self._active = True
        reporter("info", f"[dry-run] would start {self.name}")

    def is_active(self) -> bool:
        return self._active


def make_controller(
    settings: Settings,
    *,
    call: Optional[Callable[[str, dict], Any]] = None,
) -> ServiceController:
    """Pick a backend. `call` is only available inside the agent."""
    if settings.dry_run or settings.service_backend == "null":
        return NullService(settings.service)
    if settings.service_backend == "moonraker" and call is not None:
        return MoonrakerService(call, settings.service)
    return SystemdService(settings.service)


# --------------------------------------------------------------------------
# crash journal
# --------------------------------------------------------------------------


class Journal:
    """Records that we stopped a service, so a crash can be reconciled.

    Written before the stop and cleared after the start. If the process is
    SIGKILLed in between, the next startup finds the entry and restarts klipper.
    """

    def __init__(self, paths: Paths) -> None:
        self.path = paths.journal_file

    def record_stop(self, service: str, label: str) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {"service": service, "label": label, "at": time.time(), "pid": os.getpid()}, fh
            )
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def pending(self) -> Optional[dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("service") else None


def reconcile(
    paths: Paths,
    svc: ServiceController,
    *,
    reporter: Reporter = null_reporter,
) -> bool:
    """On startup: if a previous run died with klipper stopped, start it.

    Returns True if it took action.
    """
    journal = Journal(paths)
    entry = journal.pending()
    if entry is None:
        return False

    age = time.time() - float(entry.get("at") or 0)
    reporter(
        "warn",
        f"found an unfinished operation from {age:.0f}s ago "
        f"({entry.get('label', 'unknown')}) that stopped '{entry.get('service')}'. "
        f"Making sure it's running again.",
    )
    if not svc.is_active():
        svc.start(reporter)
    journal.clear()
    return True


#: How long to wait for a stop to actually take effect before giving up.
STOP_VERIFY_TIMEOUT = 20.0


@contextlib.contextmanager
def klipper_stopped(
    paths: Paths,
    svc: ServiceController,
    label: str,
    *,
    reporter: Reporter = null_reporter,
    verify: bool = True,
    verify_timeout: float = STOP_VERIFY_TIMEOUT,
) -> Iterator[None]:
    """Stop the service for the duration of the block, then put it back.

    Idempotent: if klipper was already stopped on entry, it is left stopped on
    exit rather than being helpfully started - the user stopped it for a reason.

    **The stop is verified.** If klipper is still running - no passwordless sudo,
    Moonraker unreachable, a wedged unit - this raises instead of continuing.
    Flashing with klipper up means klipper is holding the serial port, so the
    write would either fail outright or fight for the device. Refusing is the
    only safe answer.
    """
    was_active = svc.is_active()
    journal = Journal(paths)

    if not was_active:
        reporter("info", f"{svc.name} is already stopped - leaving it that way.")
        yield
        return

    journal.record_stop(svc.name, label)
    svc.stop(reporter)

    if verify:
        deadline = time.monotonic() + verify_timeout
        while svc.is_active():
            if time.monotonic() >= deadline:
                # Put it back the way we found it before bailing out: we asked it
                # to stop and it may yet comply, so don't leave it ambiguous.
                try:
                    svc.start(reporter)
                finally:
                    journal.clear()
                raise ServiceControlError(
                    f"could not stop '{svc.name}' within {verify_timeout:.0f}s - refusing to "
                    f"continue, because flashing while klipper holds the serial port is unsafe. "
                    f"Check passwordless sudo for systemctl, or that klipper is in "
                    f"~/printer_data/moonraker.asvc.",
                    service=svc.name,
                )
            time.sleep(0.5)
        reporter("info", f"{svc.name} confirmed stopped.")

    try:
        yield
    finally:
        # Belt and braces: this is the single most important line in the project.
        # Whatever happened above, klipper has to come back.
        try:
            svc.start(reporter)
        finally:
            journal.clear()


def assert_not_printing(
    settings: Settings,
    *,
    print_state: Optional[Callable[[], Optional[str]]] = None,
    force: bool = False,
    reporter: Reporter = null_reporter,
) -> None:
    """Refuse to flash during a print.

    Nothing prevented this before, so a cron'd `update-all -y` could destroy a
    running print. `print_state` returns klipper's print_stats.state; it is only
    available where there's a Moonraker connection, so the CLI passes None and
    this becomes a no-op there.
    """
    if force or settings.allow_flash_while_printing or print_state is None:
        return
    try:
        state = print_state()
    except Exception as exc:  # noqa: BLE001 - never let the check itself break a flash
        reporter("warn", f"could not determine print state ({exc}); continuing")
        return
    if state in ("printing", "paused"):
        raise PrintInProgressError(
            f"a print is currently {state} - refusing to flash. Cancel the print "
            f"first, or pass --force if you are certain.",
            state=state,
        )
