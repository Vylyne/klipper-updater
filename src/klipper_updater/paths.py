"""Every filesystem location the tool touches, in one overridable place.

This is the testability seam. The original script hardcoded
``os.path.expanduser("~/mcus")`` at import time, which made the whole thing
untestable off a printer. Route everything through a ``Paths`` instance and the
entire core runs against a tmp_path on Windows with no mocks and no hardware.

Env overrides (all honoured by :meth:`Paths.from_env`):

  KLIPPER_UPDATER_HOME          pretend this is ~
  KLIPPER_UPDATER_SETTINGS      relocate ~/mcus on its own
  KLIPPER_UPDATER_FAKE_BUS      replace /dev/serial/by-id (touch/rm files in it
                                to simulate a board re-enumerating)
  KLIPPER_UPDATER_PRINTER_DATA  relocate ~/printer_data
"""

from __future__ import annotations

import dataclasses
import os

#: The two firmware trees this tool builds. Order matters for display only.
FW_TARGETS = ("klipper", "katapult")

#: Waiting for a board to come back after katapult's `-r` bootloader request.
#: USB re-enumeration is fast; if it hasn't happened in 15s it isn't going to.
REENUMERATE_TIMEOUT = 15

#: Waiting for a human to find the board and hold BOOT0/BOOTSEL. Deliberately
#: much longer than REENUMERATE_TIMEOUT - the original code used one 15s
#: constant for both, which is far too short for a physical task.
HUMAN_ACTION_TIMEOUT = 120

DEFAULT_SERIAL_BY_ID = "/dev/serial/by-id"


@dataclasses.dataclass(frozen=True)
class Paths:
    home: str
    settings_dir: str
    serial_by_id: str
    printer_data: str

    # --- files derived from settings_dir ---

    @property
    def mcus_json(self) -> str:
        return os.path.join(self.settings_dir, "mcus.json")

    @property
    def settings_file(self) -> str:
        return os.path.join(self.settings_dir, "updater.conf")

    @property
    def lock_file(self) -> str:
        return os.path.join(self.settings_dir, ".updater.lock")

    @property
    def journal_file(self) -> str:
        """Records "klipper was stopped by us" so a crashed run can be reconciled."""
        return os.path.join(self.settings_dir, ".updater.state")

    # --- external tools / trees ---

    @property
    def flashtool(self) -> str:
        return os.path.join(self.home, "katapult", "scripts", "flashtool.py")

    @property
    def moonraker_sock(self) -> str:
        return os.path.join(self.printer_data, "comms", "moonraker.sock")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.printer_data, "logs")

    def fw_dir(self, fw: str) -> str:
        """Source tree for a firmware target, e.g. ~/klipper."""
        return os.path.join(self.home, fw)

    def kconfiglib(self, fw: str) -> str:
        return os.path.join(self.fw_dir(fw), "lib", "kconfiglib", "kconfiglib.py")

    def kconfig_root(self, fw: str) -> str:
        """Path to the top-level Kconfig, relative to fw_dir (as make invokes it)."""
        return os.path.join("src", "Kconfig")

    def built_artifact(self, fw: str, ext: str = "bin") -> str:
        """Where the source tree drops its output, e.g. ~/klipper/out/klipper.bin."""
        return os.path.join(self.fw_dir(fw), "out", f"{fw}.{ext}")

    # --- per-type saved state ---

    def type_dir(self, mcu_type: str) -> str:
        return os.path.join(self.settings_dir, mcu_type)

    def config_file(self, mcu_type: str, fw: str) -> str:
        return os.path.join(self.type_dir(mcu_type), f"{fw}.config")

    def bin_file(self, mcu_type: str, fw: str) -> str:
        return os.path.join(self.type_dir(mcu_type), f"{fw}.bin")

    def uf2_file(self, mcu_type: str, fw: str) -> str:
        """RP2040 BOOTSEL mass storage only accepts .uf2; a .bin is silently ignored."""
        return os.path.join(self.type_dir(mcu_type), f"{fw}.uf2")

    def sidecar_file(self, mcu_type: str, fw: str) -> str:
        """Build provenance: {fw_sha, config_sha256, duration, timestamp}."""
        return os.path.join(self.type_dir(mcu_type), f"{fw}.build.json")

    # --- construction ---

    @classmethod
    def from_env(cls, home: str | None = None, env: dict[str, str] | None = None) -> Paths:
        e = os.environ if env is None else env

        resolved_home = home or e.get("KLIPPER_UPDATER_HOME") or os.path.expanduser("~")
        resolved_home = os.path.abspath(resolved_home)

        settings = e.get("KLIPPER_UPDATER_SETTINGS") or os.path.join(resolved_home, "mcus")
        bus = e.get("KLIPPER_UPDATER_FAKE_BUS") or DEFAULT_SERIAL_BY_ID
        pdata = e.get("KLIPPER_UPDATER_PRINTER_DATA") or os.path.join(resolved_home, "printer_data")

        return cls(
            home=resolved_home,
            settings_dir=os.path.abspath(settings),
            serial_by_id=bus,
            printer_data=os.path.abspath(pdata),
        )
