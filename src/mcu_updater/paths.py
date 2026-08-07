"""Every filesystem location the tool touches, in one overridable place.

This is the testability seam. The original script hardcoded
``os.path.expanduser("~/mcus")`` at import time, which made the whole thing
untestable off a printer. Route everything through a ``Paths`` instance and the
entire core runs against a tmp_path on Windows with no mocks and no hardware.

Env overrides (all honoured by :meth:`Paths.from_env`):

  MCU_UPDATER_HOME          pretend this is ~
  MCU_UPDATER_CONFIG_DIR    relocate the hand-edited config dir
  MCU_UPDATER_DATA_DIR      relocate the artifact/state dir
  MCU_UPDATER_FAKE_BUS      replace /dev/serial/by-id (touch/rm files in it
                                to simulate a board re-enumerating)
  MCU_UPDATER_PRINTER_DATA  relocate ~/printer_data
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

#: Per-type config folders are gathered under this subdirectory of `config_dir`.
#: They used to sit directly in it, so a printer with six board types showed six
#: folders in Mainsail's file browser before the one file anyone edits.
#: :func:`mcu_updater.layout.migrate_type_dirs` moves an old install across.
TYPE_SUBDIR = "types"


@dataclasses.dataclass(frozen=True)
class Paths:
    """Where everything lives.

    Split by *what the thing is*, following the printer_data conventions:

    ``config_dir`` (``~/printer_data/config/mcu-updater``)
        Hand-edited, effectively irreplaceable, wants backing up - the registry
        and the saved menuconfig answers. Being under the config root means
        Moonraker serves it, so these are editable in Mainsail's own editor.

    ``data_dir`` (``~/printer_data/mcu-updater``)
        Build artifacts and runtime state. Deliberately *not* in config/: .bin
        files are regenerable, and git-based backup tools commit everything under
        config/, so putting them there means a binary churn commit after every
        build. Same pattern moonraker-timelapse uses for printer_data/timelapse.
    """

    home: str
    config_dir: str
    data_dir: str
    serial_by_id: str
    printer_data: str

    # --- hand-edited config ---

    @property
    def main_config(self) -> str:
        """One file for everything hand-edited.

        The registry ([mcu <name>] sections) and the tool settings ([updater])
        live together. They were separate while the registry was JSON - a
        `_settings` key in a dict keyed by board name would have been ugly and
        collision-prone - but .cfg sections namespace cleanly, so one file is
        simply less to find and less to edit.
        """
        return os.path.join(self.config_dir, "mcu-updater.cfg")

    @property
    def registry_file(self) -> str:
        """The [mcu ...] sections. Same file as `main_config`."""
        return self.main_config

    @property
    def settings_file(self) -> str:
        """The [updater] section. Same file as `main_config`."""
        return self.main_config

    @property
    def legacy_settings_file(self) -> str:
        """Settings used to live here. Only used to warn, never read."""
        return os.path.join(self.config_dir, "updater.conf")

    @property
    def legacy_locations(self) -> list[str]:
        """Registry paths we no longer look at. Used only to refuse helpfully.

        Both are dead ends rather than things to migrate from, but finding one
        while the current file is absent means the user has data somewhere we are
        about to ignore - and silently reporting an empty registry is how the next
        add-type overwrites it.
        """
        return [
            os.path.join(self.home, "mcus", "mcus.json"),
            # Short-lived: the directory was renamed with the project.
            os.path.join(self.printer_data, "config", "klipper-updater", "mcus.cfg"),
            # Short-lived: registry and settings were merged into one file.
            os.path.join(self.config_dir, "mcus.cfg"),
        ]

    # --- runtime state ---

    @property
    def lock_file(self) -> str:
        return os.path.join(self.data_dir, ".updater.lock")

    @property
    def registry_lock_file(self) -> str:
        """Serialises registry writes. Deliberately *not* `lock_file`.

        A registry edit is a sub-millisecond load-modify-write; a build or flash
        holds `lock_file` for minutes. Sharing one lock would mean "you cannot
        track a board while a build is running", which is a pointless refusal -
        they touch different things.
        """
        return os.path.join(self.data_dir, ".registry.lock")

    @property
    def flashlog_file(self) -> str:
        """Which binary was last written to each board.

        In the data tree rather than config: it is a record of what happened, not
        something anyone hand-edits, and it is regenerable in the sense that losing
        it degrades answers to "unknown" rather than breaking anything.
        """
        return os.path.join(self.data_dir, ".flashed.json")

    @property
    def pairings_file(self) -> str:
        """Which type each freshly-bootloadered board was meant to become.

        A board in DFU has no identity to record, so this is keyed on its *DFU*
        serial - which is derivable from the running serial it will have once
        Katapult is on it, and is therefore the one thing that survives the
        transition. Written before the re-enumeration wait, so a board that takes
        longer than the wait, or is unplugged and brought back tomorrow, still
        arrives with its intent attached instead of as an anonymous stranger.
        """
        return os.path.join(self.data_dir, ".dfu-pairings.json")

    def display_sidecar(self, env: str) -> str:
        """Build provenance for one display env: which commit the image is from.

        In our data tree even though the image itself lives in the source repo's
        `.pio/build/<env>/`. That directory is PlatformIO's, and writing our
        bookkeeping into it would put it in the path of `pio run -t clean` and
        into the user's git status.
        """
        return os.path.join(self.data_dir, "displays", f"{env}.build.json")

    @property
    def display_macs_file(self) -> str:
        """Which display (by MAC) was last flashed on which port.

        A display's MAC is in efuse, so it survives reflashing - and the CH340 in
        front of it has no serial to offer, so this is the only durable identity
        one has. Recorded on every upload, which is the only moment it is
        obtainable: esptool reads it over the ROM handshake, and that needs the
        port free, which means Klipper stopped.

        Its use is noticing a swap. Two tophat boards plugged into each other's
        sockets moves every display on them at once, and nothing else would say
        so.
        """
        return os.path.join(self.data_dir, ".display-macs.json")

    @property
    def journal_file(self) -> str:
        """Records "klipper was stopped by us" so a crashed run can be reconciled."""
        return os.path.join(self.data_dir, ".updater.state")

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

    @property
    def type_root(self) -> str:
        """Parent of every per-type config folder.

        Exists so `mcu-updater.cfg` is the only thing in `config_dir` - it is the
        file people open, and it was previously buried under one folder per board
        type in Mainsail's editor.
        """
        return os.path.join(self.config_dir, TYPE_SUBDIR)

    def type_dir(self, mcu_type: str) -> str:
        """Saved menuconfig answers for one type. Backed up, editable in Mainsail."""
        return os.path.join(self.type_root, mcu_type)

    def legacy_type_dir(self, mcu_type: str) -> str:
        """Where a type's config lived before it was gathered under `types/`.

        Only for migrating; nothing reads a config from here.
        """
        return os.path.join(self.config_dir, mcu_type)

    def artifact_dir(self, mcu_type: str) -> str:
        """Built firmware for one type. Regenerable, so kept out of backups."""
        return os.path.join(self.data_dir, mcu_type)

    def config_file(self, mcu_type: str, fw: str) -> str:
        return os.path.join(self.type_dir(mcu_type), f"{fw}.config")

    def bin_file(self, mcu_type: str, fw: str) -> str:
        return os.path.join(self.artifact_dir(mcu_type), f"{fw}.bin")

    def uf2_file(self, mcu_type: str, fw: str) -> str:
        """RP2040 BOOTSEL mass storage only accepts .uf2; a .bin is silently ignored."""
        return os.path.join(self.artifact_dir(mcu_type), f"{fw}.uf2")

    def sidecar_file(self, mcu_type: str, fw: str) -> str:
        """Build provenance: {fw_sha, config_sha256, duration, timestamp}."""
        return os.path.join(self.artifact_dir(mcu_type), f"{fw}.build.json")

    # --- construction ---

    @classmethod
    def from_env(cls, home: str | None = None, env: dict[str, str] | None = None) -> Paths:
        e = os.environ if env is None else env

        resolved_home = home or e.get("MCU_UPDATER_HOME") or os.path.expanduser("~")
        resolved_home = os.path.abspath(resolved_home)

        pdata = e.get("MCU_UPDATER_PRINTER_DATA") or os.path.join(resolved_home, "printer_data")
        pdata = os.path.abspath(pdata)

        config = e.get("MCU_UPDATER_CONFIG_DIR") or os.path.join(
            pdata, "config", "mcu-updater"
        )
        data = e.get("MCU_UPDATER_DATA_DIR") or os.path.join(pdata, "mcu-updater")
        bus = e.get("MCU_UPDATER_FAKE_BUS") or DEFAULT_SERIAL_BY_ID

        return cls(
            home=resolved_home,
            config_dir=os.path.abspath(config),
            data_dir=os.path.abspath(data),
            serial_by_id=bus,
            printer_data=pdata,
        )
