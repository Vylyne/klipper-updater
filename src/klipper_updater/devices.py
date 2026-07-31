"""Discovering boards under ``/dev/serial/by-id``.

Entries look like ``usb-<fw>_<chipset>_<serial>``, e.g.::

    usb-Klipper_stm32g0b1xx_290055001850304158373620-if00
    usb-katapult_stm32f072xb_4C0033000957465331323720-if00

The firmware component's capitalisation is **not** dependable - it has been
observed as both ``Klipper`` and ``klipper`` depending on the board and klipper
version. The original code compared it case-insensitively when *discovering*
devices but rebuilt the path with an exact-case f-string when *flashing*, so a
board enumerating as lowercase was found and then declared missing.

Everything here therefore matches case-insensitively and returns the real
on-disk path rather than a reconstructed one.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from collections.abc import Iterable
from typing import Optional

from .errors import BootloaderTimeoutError, OperationCancelled
from .paths import REENUMERATE_TIMEOUT, Paths

_PREFIX = "usb-"

#: Katapult was called CanBoot before it was renamed; older bootloaders still
#: enumerate under the old name.
KATAPULT_NAMES = ("katapult", "canboot")
KLIPPER_NAMES = ("klipper",)

#: Display-only. Never use these to build a path you then test for existence.
KLIPPER_FW_NAME = "Klipper"
KATAPULT_FW_NAME = "katapult"

STATE_KLIPPER = "klipper"
STATE_KATAPULT = "katapult"
STATE_OFFLINE = "offline"


@dataclasses.dataclass(frozen=True)
class BusDevice:
    fw: str
    chipset: str
    serial: str
    path: str

    @property
    def is_klipper(self) -> bool:
        return self.fw.lower() in KLIPPER_NAMES

    @property
    def is_katapult(self) -> bool:
        return self.fw.lower() in KATAPULT_NAMES

    @property
    def state(self) -> str:
        if self.is_klipper:
            return STATE_KLIPPER
        if self.is_katapult:
            return STATE_KATAPULT
        return self.fw.lower()


def parse_entry(name: str, directory: str) -> Optional[BusDevice]:
    """Parse one by-id filename. Returns None if it isn't a recognisable device."""
    if not name.startswith(_PREFIX):
        return None
    parts = name[len(_PREFIX) :].split("_", 2)
    if len(parts) < 2:
        return None
    if len(parts) == 3:
        fw, chipset, serial = parts
    else:
        # Two-part name: no chipset component.
        fw, chipset, serial = parts[0], "", parts[1]
    if not serial:
        return None
    return BusDevice(fw=fw, chipset=chipset, serial=serial, path=os.path.join(directory, name))


def scan(paths: Paths) -> list[BusDevice]:
    """Every parseable device currently on the bus. Empty list if there's no bus."""
    directory = paths.serial_by_id
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        dev = parse_entry(name, directory)
        if dev is not None:
            out.append(dev)
    return out


def find_device(
    paths: Paths,
    chipset: str,
    serial: str,
    fw: Optional[str] = None,
) -> Optional[BusDevice]:
    """Locate one board by chipset+serial, optionally constrained to a firmware.

    `fw` may be a single name or one of the KLIPPER_NAMES/KATAPULT_NAMES
    groups; matching is case-insensitive. Returns the device with its actual
    path, or None.
    """
    wanted: Optional[tuple[str, ...]]
    if fw is None:
        wanted = None
    elif fw.lower() in KATAPULT_NAMES:
        wanted = KATAPULT_NAMES
    elif fw.lower() in KLIPPER_NAMES:
        wanted = KLIPPER_NAMES
    else:
        wanted = (fw.lower(),)

    for dev in scan(paths):
        if dev.serial != serial:
            continue
        if chipset and dev.chipset != chipset:
            continue
        if wanted is not None and dev.fw.lower() not in wanted:
            continue
        return dev
    return None


def device_state(paths: Paths, chipset: str, serial: str) -> tuple[str, Optional[str]]:
    """(state, path) for a tracked serial: klipper, katapult, or offline."""
    dev = find_device(paths, chipset, serial)
    if dev is None:
        return STATE_OFFLINE, None
    return dev.state, dev.path


def find_untracked(
    paths: Paths,
    known_serials: Iterable[str],
    *,
    fw: Optional[str] = None,
    chipset: Optional[str] = None,
) -> list[BusDevice]:
    """Devices on the bus whose serial isn't tracked under any MCU type."""
    known = set(known_serials)
    wanted_group: Optional[tuple[str, ...]] = None
    if fw is not None:
        if fw.lower() in KATAPULT_NAMES:
            wanted_group = KATAPULT_NAMES
        elif fw.lower() in KLIPPER_NAMES:
            wanted_group = KLIPPER_NAMES
        else:
            wanted_group = (fw.lower(),)

    out = []
    for dev in scan(paths):
        if dev.serial in known:
            continue
        if wanted_group is not None and dev.fw.lower() not in wanted_group:
            continue
        if chipset and dev.chipset != chipset:
            continue
        out.append(dev)
    return out


def expected_path(fw_name: str, chipset: str, serial: str) -> str:
    """Reconstructed path, for error messages only.

    Never test this for existence - the firmware name's case isn't reliable.
    Use find_device() instead.
    """
    return f"/dev/serial/by-id/{_PREFIX}{fw_name}_{chipset}_{serial}"


def _sleep_checked(seconds: float, cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.wait(seconds):
        raise OperationCancelled("cancelled while waiting for a device")
    elif cancel is None:
        time.sleep(seconds)


def wait_for_device(
    paths: Paths,
    chipset: str,
    serial: str,
    fw: str,
    *,
    timeout: float = REENUMERATE_TIMEOUT,
    poll: float = 0.5,
    settle: float = 0.0,
    cancel: Optional[threading.Event] = None,
) -> BusDevice:
    """Poll until a specific board shows up under `fw`, or raise.

    `settle` adds a pause after first sighting: udev creating the symlink is not
    atomic with respect to the device being openable, so flashing immediately
    can race.
    """
    deadline = time.monotonic() + timeout
    while True:
        dev = find_device(paths, chipset, serial, fw=fw)
        if dev is not None:
            if settle:
                _sleep_checked(settle, cancel)
            return dev
        if time.monotonic() >= deadline:
            raise BootloaderTimeoutError(
                f"{serial} never appeared as a {fw} device within {timeout:.0f}s "
                f"(expected something like {expected_path(fw, chipset, serial)}).",
                serial=serial,
                chipset=chipset,
                fw=fw,
                timeout=timeout,
            )
        _sleep_checked(poll, cancel)


def wait_for_new_device(
    paths: Paths,
    baseline: Iterable[str],
    *,
    fw: Optional[str] = None,
    chipset: Optional[str] = None,
    timeout: float = REENUMERATE_TIMEOUT,
    poll: float = 0.5,
    settle: float = 1.0,
    cancel: Optional[threading.Event] = None,
) -> list[BusDevice]:
    """Poll for any device not in `baseline` to appear.

    Replaces the original's fixed `time.sleep(3)` after a DFU flash. Returns the
    new devices, or an empty list on timeout - appearing is the interesting
    event, so a caller that gets nothing back reports it rather than erroring.
    """
    known = set(baseline)
    deadline = time.monotonic() + timeout
    while True:
        found = find_untracked(paths, known, fw=fw, chipset=chipset)
        if found:
            if settle:
                _sleep_checked(settle, cancel)
            return find_untracked(paths, known, fw=fw, chipset=chipset) or found
        if time.monotonic() >= deadline:
            return []
        _sleep_checked(poll, cancel)
