"""ESP32 displays: PlatformIO builds, esptool uploads.

Different enough from an MCU to live apart. There is no Kconfig, no Katapult, no
chipset to reason about - a PlatformIO env already names the board, the partition
table and the build flags, so the env *is* the type. Adding the second display
is another `[display <env>]` section and nothing structural.

The device list is not here either: `[knomi_serial T0_knomi]` in Klipper's config
already names the port it uses, so a second copy would only be something to
disagree with.

**Nothing here ever lets PlatformIO choose a port.** Its auto-detect picks one
device arbitrarily when several match, and every display on this printer is an
indistinguishable CH340 - so an upload without an explicit port writes firmware
to whichever one answered first. See `upload()`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import threading
import time
from typing import Optional

from .build import Reporter, null_reporter, run_streamed
from .cfgdoc import CfgDocument
from .errors import BuildError, ConfigError, FlashError, SourceTreeMissingError, ToolMissingError
from .paths import Paths
from .settings import Settings

SECTION_PREFIX = "display"

#: Where PlatformIO puts itself. `pio` on PATH first, because that is what a
#: user's own symlinks give; the venv path is the fallback for a service whose
#: PATH is systemd's rather than a login shell's.
PIO_CANDIDATES = (
    "pio",
    os.path.expanduser("~/.platformio/penv/bin/pio"),
    "/usr/local/bin/pio",
)

#: From esptool's own banner, e.g. `MAC: cc:ba:97:19:aa:38`. The one piece of
#: durable identity a display has: it is in efuse, so it survives reflashing, and
#: the CH340 in front of it has no serial of its own to offer.
_MAC_RE = re.compile(r"^MAC:\s*((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})\s*$", re.MULTILINE)

#: `Chip is ESP32-S3 (QFN56) (revision v0.2)`
_CHIP_RE = re.compile(r"^Chip is (.+?)\s*$", re.MULTILINE)

#: PlatformIO giving up inside WaitForNewSerialPort. The board manifest for a
#: native-USB ESP32-S3 tells it to reset the board and then adopt whatever *new*
#: serial port appears. A display wired through a CH340 keeps the same port -
#: the CH340 is a separate always-powered chip and never leaves the bus - so no
#: new port ever appears and it times out on a perfectly healthy screen.
#:
#: Matched so the failure can explain itself. It cannot be fixed from here:
#: board_upload.* is settable only in platformio.ini, and `pio run` has no
#: option to override it.
_WAITING_FOR_PORT_RE = re.compile(r"Couldn't find a board on the selected port", re.I)


@dataclasses.dataclass
class DisplayType:
    """One PlatformIO env, and where its devices are declared in printer.cfg."""

    name: str
    env: str = ""
    source: str = ""
    #: The Klipper section prefix whose entries are displays of this type.
    #: `[knomi_serial T0_knomi]` -> `knomi_serial`. A second display with its own
    #: klippy module would set this differently; one sharing the module leaves it.
    klipper_section: str = "knomi_serial"

    def __post_init__(self) -> None:
        # The env is the type, so the section name is the env unless overridden.
        if not self.env:
            self.env = self.name

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "env": self.env,
            "source": self.source,
            "klipper_section": self.klipper_section,
        }


def load(paths: Paths, default_source: str = "") -> dict[str, DisplayType]:
    """Read `[display <name>]` sections from the shared config file."""
    try:
        with open(paths.main_config, encoding="utf-8") as fh:
            doc = CfgDocument(fh.read())
    except OSError:
        return {}

    out: dict[str, DisplayType] = {}
    for section in doc.section_names(SECTION_PREFIX):
        name = section[len(SECTION_PREFIX) :].strip()
        if not name:
            continue
        out[name] = DisplayType(
            name=name,
            env=(doc.get(section, "env") or "").strip(),
            source=(doc.get(section, "source") or default_source).strip(),
            klipper_section=(doc.get(section, "klipper_section") or "knomi_serial").strip(),
        )
    return out


def find_pio(settings: Settings) -> str:
    """The PlatformIO launcher, or a clear error naming what to install."""
    configured = settings.platformio_bin
    candidates = ([configured] if configured else []) + list(PIO_CANDIDATES)
    for candidate in candidates:
        found = shutil.which(candidate) if os.path.basename(candidate) == candidate else candidate
        if found and os.path.exists(found):
            return found
    raise ToolMissingError(
        "PlatformIO not found. Install it, or symlink its launcher onto PATH: "
        "~/.platformio/penv/bin/pio",
        tool="pio",
    )


def _source_dir(display: DisplayType) -> str:
    path = os.path.expanduser(display.source)
    if not path:
        raise ConfigError(
            f"display '{display.name}' has no source tree configured. Set 'source:' "
            f"in its [display] section, or 'display_source' in [updater].",
            type=display.name,
        )
    if not os.path.isdir(path):
        raise SourceTreeMissingError(
            f"source directory {path} not found for display '{display.name}'.",
            fw=display.env,
            path=path,
        )
    return path


def resolve_port(port: str) -> str:
    """Follow a udev symlink to the device PlatformIO can actually see.

    `pio device list` enumerates through pyserial, which reports real devices -
    `/dev/ttyUSB0` - and never the `/dev/knomi_t0` symlink pointing at one. Hand
    PlatformIO the symlink and it looks for a board on a port that is not in its
    list, which is why an upload to a perfectly healthy display failed with
    "Couldn't find a board on the selected port".

    Resolved here, at the moment of the write, rather than in the config: the
    stable name is the whole reason the udev rule exists, and `/dev/ttyUSB0`
    depends on plug order. Klipper is stopped by the time this runs, so nothing
    is re-enumerating in the gap between resolving and writing.

    A broken or absent symlink resolves to itself; PlatformIO then reports a
    missing port, which is a better message than anything invented here.
    """
    try:
        return os.path.realpath(port)
    except OSError:
        return port


def firmware_bin(display: DisplayType) -> str:
    """Where PlatformIO leaves the image for this env."""
    return os.path.join(
        os.path.expanduser(display.source), ".pio", "build", display.env, "firmware.bin"
    )


def build(
    paths: Paths,
    settings: Settings,
    display: DisplayType,
    *,
    reporter: Reporter = null_reporter,
    cancel: Optional[threading.Event] = None,
) -> str:
    """Compile one env. Returns the path to the image it produced."""
    source = _source_dir(display)
    pio = find_pio(settings)

    reporter("info", f"Building {display.env} in {source}...")
    rc = run_streamed(
        [pio, "run", "-e", display.env],
        cwd=source,
        reporter=reporter,
        cancel=cancel,
        dry_run=settings.dry_run,
    )
    if rc != 0:
        raise BuildError(
            f"PlatformIO build failed for display '{display.name}': pio exited {rc}.",
            type=display.name,
            fw=display.env,
            returncode=rc,
        )
    return firmware_bin(display)


def upload(
    paths: Paths,
    settings: Settings,
    display: DisplayType,
    port: str,
    *,
    reporter: Reporter = null_reporter,
    cancel: Optional[threading.Event] = None,
) -> dict[str, Optional[str]]:
    """Write this env's firmware to the display at `port`.

    **`port` is required and is never inferred.** PlatformIO auto-detects an
    upload port when none is given, and with several identical CH340s attached it
    picks whichever it finds first - observed doing exactly that on this printer,
    choosing between two displays with no way for the user to know which. An
    upload that guesses its target writes firmware to the wrong screen.

    esptool's ROM handshake is what verifies the target: it refuses to write to
    anything that is not an ESP32, so the check is inherent rather than a step
    that could be skipped. Its banner also carries the MAC, which is the only
    durable identity a display has - returned here so a caller can record it.
    """
    if not port:
        raise FlashError(
            "refusing to upload without an explicit port: PlatformIO would pick a "
            "device on its own, and every display here is an identical CH340.",
            type=display.name,
        )

    source = _source_dir(display)
    pio = find_pio(settings)

    transcript: list[str] = []

    def capture(stream: str, line: str) -> None:
        transcript.append(line)
        reporter(stream, line)

    target = resolve_port(port)
    reporter("info", f"Uploading {display.env} to {port}...")
    if target != port:
        # Say which real device is about to be written. The stable name is what
        # the config and the MAC record use; this is the only place the two are
        # visibly tied together.
        reporter("info", f"{port} -> {target}")

    rc = run_streamed(
        [
            pio,
            "run",
            "-e",
            display.env,
            "-t",
            "upload",
            "--upload-port",
            target,
            # Nothing else goes here. `pio run` takes no --project-option - that
            # belongs to `pio ci` and `pio project init` - and an invalid flag
            # makes pio exit before it touches the board, wasting a whole
            # Klipper stop/start cycle. board_upload.* can only be set in
            # platformio.ini; see _WAITING_FOR_PORT_RE for the failure that
            # causes and the message that explains it.
        ],
        cwd=source,
        reporter=capture,
        cancel=cancel,
        dry_run=settings.dry_run,
    )

    text = "\n".join(transcript)
    mac = _MAC_RE.search(text)
    chip = _CHIP_RE.search(text)

    if rc != 0:
        if _WAITING_FOR_PORT_RE.search(text):
            raise FlashError(
                f"upload failed for display '{display.name}' on {port}: PlatformIO reset "
                f"the board and then waited for a *new* serial port to appear. One never "
                f"will - this display talks through a CH340, which stays on the bus and "
                f"keeps the same port.\n"
                f"Add this to the [env:{display.env}] section of "
                f"{os.path.join(source, 'platformio.ini')}:\n"
                f"    board_upload.wait_for_upload_port = no\n"
                f"It has to go there: board_upload.* is a platformio.ini setting and "
                f"'pio run' has no command-line option for it.",
                type=display.name,
                port=port,
                returncode=rc,
                remedy="board_upload.wait_for_upload_port = no",
            )
        raise FlashError(
            f"upload failed for display '{display.name}' on {port}: pio exited {rc}.",
            type=display.name,
            port=port,
            returncode=rc,
        )
    return {
        "port": port,
        "mac": mac.group(1).lower() if mac else None,
        "chip": chip.group(1) if chip else None,
    }


# --------------------------------------------------------------------------
# remembering which display sat on which port
# --------------------------------------------------------------------------


def read_macs(paths: Paths) -> dict[str, dict]:
    """port -> {mac, env, at}. Empty when the file is missing or unreadable."""
    try:
        with open(paths.display_macs_file, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record_mac(paths: Paths, port: str, mac: Optional[str], env: str) -> Optional[str]:
    """Note the display seen on `port`, and report a MAC that has changed.

    Returns the *previous* MAC when it differs - which is the swap signal. A
    tophat board plugged into the other socket moves every display on it at once,
    and this is the only thing that would notice.

    Absent MAC writes nothing: esptool did not report one, and overwriting a good
    record with a blank would destroy the very history this exists for.
    """
    if not mac:
        return None
    data = read_macs(paths)
    previous = (data.get(port) or {}).get("mac")
    data[port] = {"mac": mac, "env": env, "at": time.time()}

    os.makedirs(os.path.dirname(paths.display_macs_file), exist_ok=True)
    tmp = paths.display_macs_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, paths.display_macs_file)

    return previous if previous and previous != mac else None
