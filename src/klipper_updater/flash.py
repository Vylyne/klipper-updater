"""Flashing boards.

Two paths:

* **katapult** - the normal case. A board already running Klipper is asked to
  reboot into its bootloader, waited for, then written via katapult's
  ``flashtool.py``.
* **dfu-util** - the first-ever flash of a bare STM32, which has no bootloader
  yet to speak flashtool's protocol.

Cancellation is deliberately *not* plumbed into the write step. Interrupting
``flashtool -f`` part-way through leaves a board with half a firmware image.
Callers cancel between devices, never during one.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional

from .build import Reporter, null_reporter, run_streamed
from .devices import (
    KATAPULT_FW_NAME,
    KLIPPER_FW_NAME,
    BusDevice,
    expected_path,
    find_device,
    wait_for_device,
    wait_for_new_device,
)
from .errors import (
    AmbiguousDfuError,
    DeviceNotFoundError,
    DfuPermissionError,
    FlashError,
    OperationCancelled,
    ToolMissingError,
    UnsupportedChipsetError,
)
from .paths import HUMAN_ACTION_TIMEOUT, REENUMERATE_TIMEOUT, Paths
from .settings import Settings

DFU_VID_PID = "0483:df11"


def flash_katapult(
    paths: Paths,
    settings: Settings,
    mcu_type: str,
    chipset: str,
    serial: str,
    fw_bin: Optional[str] = None,
    *,
    reporter: Reporter = null_reporter,
    timeout: float = REENUMERATE_TIMEOUT,
) -> None:
    """Flash one board through katapult's flashtool.py.

    If the board is currently running Klipper rather than sitting in its
    bootloader, this requests the bootloader first and waits for it to
    re-enumerate - flashtool's documented two-step process for devices it can't
    put into bootloader mode itself.

    Raises on any failure; returns None on success.
    """
    flashtool = paths.flashtool
    if not os.path.exists(flashtool):
        raise ToolMissingError(
            f"flashtool.py not found at {flashtool}. Is katapult installed?",
            tool="flashtool.py",
            path=flashtool,
        )

    if fw_bin is None:
        fw_bin = paths.bin_file(mcu_type, "klipper")
    if not os.path.exists(fw_bin):
        raise FlashError(
            f"firmware binary not found at {fw_bin}. Build it first.",
            type=mcu_type,
            serial=serial,
            path=fw_bin,
        )

    dev = find_device(paths, chipset, serial, fw=KATAPULT_FW_NAME)
    if dev is None:
        running = find_device(paths, chipset, serial, fw=KLIPPER_FW_NAME)
        if running is None:
            raise DeviceNotFoundError(
                f"no device found for {serial} (looked for a katapult or klipper "
                f"device with chipset {chipset}, e.g. "
                f"{expected_path(KATAPULT_FW_NAME, chipset, serial)}). Is it plugged in?",
                type=mcu_type,
                serial=serial,
                chipset=chipset,
            )
        reporter("info", f"{serial} is running Klipper - requesting bootloader...")
        run_streamed(
            [sys.executable, flashtool, "-d", running.path, "-r"],
            cwd=paths.home,
            reporter=reporter,
            dry_run=settings.dry_run,
            fake_delay=0.0,
        )
        if settings.dry_run:
            # Nothing actually rebooted, so there is nothing to wait for. Carry
            # on with the klipper node standing in rather than returning early,
            # so a rehearsal still covers the write step it is meant to rehearse.
            reporter("info", f"[dry-run] would wait for {serial} to re-enumerate as Katapult")
            dev = running
        else:
            reporter("info", f"Waiting for {serial} to re-enumerate as a Katapult device...")
            # settle: udev creating the symlink is not atomic with the device
            # being openable, so flashing the instant it appears can race.
            dev = wait_for_device(
                paths, chipset, serial, KATAPULT_FW_NAME, timeout=timeout, settle=0.5
            )

    reporter("info", f"Flashing {serial} ({mcu_type}) via {dev.path}...")
    rc = run_streamed(
        [sys.executable, flashtool, "-d", dev.path, "-f", fw_bin],
        cwd=paths.home,
        reporter=reporter,
        # No cancel: see module docstring. Never interrupt a write.
        dry_run=settings.dry_run,
        fake_delay=0.0,
    )
    if rc != 0:
        raise FlashError(
            f"flashtool.py failed for {serial} (exit {rc}).",
            type=mcu_type,
            serial=serial,
            returncode=rc,
        )

    # Note which binary this board now holds. A board only ever reports its klipper
    # commit, so without this record two builds from the same commit - a changed
    # .config, an edited makefile-patch source - are indistinguishable, and "flash
    # only the stale ones" would skip exactly the boards a patch change affected.
    if not settings.dry_run:
        from .build import FlashLog, git_head, read_sidecar

        side = read_sidecar(paths, mcu_type, "klipper") or {}
        FlashLog(paths).record(
            serial,
            mcu_type=mcu_type,
            fw="klipper",
            bin_sha256=side.get("bin_sha256"),
            fw_sha=side.get("fw_sha") or git_head(paths.fw_dir("klipper")),
        )

    reporter("info", f"Flashed {serial} successfully.")


# --------------------------------------------------------------------------
# DFU (first-time bootloader install on a bare STM32)
# --------------------------------------------------------------------------


#: `Found DFU: [0483:df11] ver=0200, devnum=51, cfg=1, intf=0, path="6-1.6.6.1.3",
#:  alt=0, name="@Internal Flash   /0x08000000/64*02Kg", serial="3941335F3434"`
#:
#: Matched on the VID:PID rather than the "Found DFU" prefix so a wording change
#: in dfu-util cannot silently reduce us to seeing nothing.
_DFU_LINE_RE = re.compile(
    r"\[(?P<vidpid>[0-9a-fA-F]{4}:[0-9a-fA-F]{4})\]"
    r"(?=.*\bdevnum=(?P<devnum>\d+))?"
    r"(?=.*\bpath=\"(?P<path>[^\"]*)\")?"
    r"(?=.*\bserial=\"(?P<serial>[^\"]*)\")?"
)

#: libusb could see the device but not claim it. Almost always a missing udev
#: rule rather than anything the user did wrong with the boot jumper.
_DFU_DENIED_RE = re.compile(
    r"cannot open dfu device|LIBUSB_ERROR_ACCESS|insufficient permission|access denied",
    re.IGNORECASE,
)


def list_dfu_devices(*, reporter: Reporter = null_reporter) -> list[str]:
    """One entry per DFU *device* from `dfu-util -l`.

    Two things this must get right, both learned the hard way on real hardware:

    **One board is several lines.** dfu-util prints a line per DFU altsetting, so
    a single STM32 appears three times (alt=0/1/2) sharing one devnum, path and
    serial. Counting lines made the ambiguity guard refuse every single-board
    flash with "3 devices are in DFU mode".

    **"Nothing listed" is not the same as "nothing attached."** Without a udev
    rule, dfu-util prints ``Cannot open DFU device ... (LIBUSB_ERROR_ACCESS)``
    and no ``Found DFU`` line at all - so the old code reported "no DFU device
    detected. Hold BOOT0 and replug", sending the user to redo the one step that
    had actually worked. That case raises now.
    """
    try:
        res = subprocess.run(
            ["dfu-util", "-l"], capture_output=True, text=True, timeout=20
        )
    except FileNotFoundError as exc:
        raise ToolMissingError(
            "dfu-util is not installed. Try: sudo apt install dfu-util", tool="dfu-util"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolMissingError(f"could not run dfu-util: {exc}", tool="dfu-util") from exc

    out = (res.stdout or "") + (res.stderr or "")

    # Deduplicate by whatever identifies the physical board, in decreasing order
    # of trustworthiness. dict preserves insertion order, so the first line for
    # each device is the one reported.
    devices: dict[str, str] = {}
    for raw in out.splitlines():
        line = raw.strip()
        match = _DFU_LINE_RE.search(line)
        if match is None:
            continue
        key = (
            match.group("serial")
            or match.group("path")
            or match.group("devnum")
            or line  # nothing to group on: treat the line itself as the device
        )
        devices.setdefault(key, line)

    if not devices and _DFU_DENIED_RE.search(out):
        raise DfuPermissionError(
            "dfu-util can see a board in DFU mode but cannot open it "
            "(LIBUSB_ERROR_ACCESS). The board and the boot jumper are fine - this "
            "is a permissions problem. Install the udev rule (install.sh offers "
            "to) or run the same command under sudo.",
            output=out.strip(),
        )

    return list(devices.values())


#: dfu-util's own statement that the image is on the board.
_DFU_WRITE_OK_RE = re.compile(r"file downloaded successfully|download done", re.IGNORECASE)

#: The failure that follows a successful `:leave`, across dfu-util versions. The
#: device is gone by design, so the request it is complaining about was never
#: going to be answered.
_DFU_LEAVE_NOISE_RE = re.compile(
    r"error during download get_status"
    r"|unable to read dfu status after completion"
    r"|lost device after",
    re.IGNORECASE,
)


def _dfu_left_successfully(transcript: list[str]) -> bool:
    """Did the write succeed and only the post-`leave` status read fail?

    Requires *both* signals. An unrecognised error after a successful download
    still fails: reporting a bricked board as flashed is far worse than the false
    failure this exists to stop, and the caller's re-enumeration wait is what
    ultimately confirms it either way.
    """
    text = "\n".join(transcript)
    return bool(_DFU_WRITE_OK_RE.search(text)) and bool(_DFU_LEAVE_NOISE_RE.search(text))


def wait_for_dfu(
    *,
    reporter: Reporter = null_reporter,
    timeout: float = HUMAN_ACTION_TIMEOUT,
    poll: float = 1.0,
    cancel: Optional[threading.Event] = None,
) -> list[str]:
    """Poll for a DFU device to appear, giving a human time to hold BOOT0."""
    deadline = time.monotonic() + timeout
    reporter("info", "Waiting for a device in DFU mode (hold BOOT0 and replug)...")
    while True:
        found = list_dfu_devices(reporter=reporter)
        if found:
            return found
        if cancel is not None and cancel.is_set():
            raise OperationCancelled("cancelled while waiting for a DFU device")
        if time.monotonic() >= deadline:
            return []
        time.sleep(poll)


def flash_dfu_stm32(
    paths: Paths,
    settings: Settings,
    fw_bin: str,
    *,
    reporter: Reporter = null_reporter,
) -> None:
    """Write a .bin to an STM32 sitting in DFU mode.

    Refuses when more than one DFU device is present. The original targeted
    ``0483:df11`` unconditionally, so with two boards in DFU - or an unrelated
    STM32 dev board plugged in - it would flash whichever answered first.
    """
    if not os.path.exists(fw_bin):
        raise FlashError(f"firmware binary not found at {fw_bin}.", path=fw_bin)

    reporter("info", "Looking for an STM32 device in DFU mode via dfu-util...")
    found = list_dfu_devices(reporter=reporter)
    for line in found:
        reporter("info", f"  {line}")

    if not found:
        raise DeviceNotFoundError(
            "no DFU device detected. Hold BOOT0 (or fit the boot jumper) and replug "
            "the board, then try again."
        )
    if len(found) > 1:
        raise AmbiguousDfuError(
            f"{len(found)} devices are in DFU mode - refusing to guess which one to "
            f"flash. Unplug all but the target board and try again.",
            devices=found,
        )

    reporter("info", "DFU device found. Flashing via dfu-util...")

    # Keep the output as well as the exit code: dfu-util's own words are the only
    # way to tell a real failure from the expected one below.
    transcript: list[str] = []

    def capture(stream: str, line: str) -> None:
        transcript.append(line)
        reporter(stream, line)

    rc = run_streamed(
        [
            "dfu-util",
            "-a",
            "0",
            "-d",
            DFU_VID_PID,
            "-D",
            fw_bin,
            "-s",
            "0x08000000:force:mass-erase:leave",
        ],
        cwd=paths.home,
        reporter=capture,
        dry_run=settings.dry_run,
        fake_delay=0.0,
    )
    if rc != 0:
        if _dfu_left_successfully(transcript):
            # Expected, not a failure. `:leave` asks the STM32 to exit DFU and
            # start the application it just received, so the device detaches
            # before dfu-util can read its status one last time - and dfu-util
            # exits 74 (EX_IOERR) over a request that could not possibly succeed.
            # The write itself already reported "File downloaded successfully".
            #
            # Deliberately not treated as fatal rather than suppressed: the caller
            # then waits for the board to re-enumerate as Katapult, which is the
            # real verdict on whether this worked. Raising here aborted *before*
            # that check, turning a good flash into a reported failure.
            reporter(
                "warn",
                f"dfu-util exited {rc} on its post-'leave' status read. That is "
                f"expected - the board detached to boot the new firmware. The "
                f"download itself succeeded.",
            )
        else:
            raise FlashError(f"dfu-util flashing failed (exit {rc}).", returncode=rc)
    reporter("info", "Flash command sent. Device should reboot into Katapult shortly.")


def flash_initial_bootloader(
    paths: Paths,
    settings: Settings,
    chipset: str,
    fw_bin: str,
    *,
    reporter: Reporter = null_reporter,
) -> None:
    """Dispatch a first-time katapult install by chipset family."""
    if chipset.startswith("stm32"):
        flash_dfu_stm32(paths, settings, fw_bin, reporter=reporter)
        return
    if chipset == "rp2040":
        # Wired up in a later phase: BOOTSEL mass storage only accepts .uf2, so
        # this needs the .uf2 that build() now stages, plus picotool or a mount.
        raise UnsupportedChipsetError(
            "RP2040 BOOTSEL flashing isn't wired up yet - hold BOOTSEL, mount the "
            "RPI-RP2 drive, copy the katapult .uf2 across, then use 'add-serial' "
            "once it enumerates as Katapult.",
            chipset=chipset,
        )
    raise UnsupportedChipsetError(
        f"don't know how to perform a first-time flash for chipset '{chipset}'. "
        f"Flash katapult manually, then use 'add-serial' once it enumerates.",
        chipset=chipset,
    )


def adoptable_devices(
    paths: Paths,
    known_serials: set,
    chipset: str,
    *,
    timeout: float = REENUMERATE_TIMEOUT,
) -> list[BusDevice]:
    """Katapult devices that appeared and aren't tracked yet.

    Replaces the original's fixed `time.sleep(3)` with a real poll.
    """
    return wait_for_new_device(
        paths,
        known_serials,
        fw=KATAPULT_FW_NAME,
        chipset=chipset,
        timeout=timeout,
    )
