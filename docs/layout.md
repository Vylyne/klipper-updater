# Where everything lives

Files are split by *what they are*, following the `printer_data` conventions.

```
~/printer_data/config/klipper-updater/     # hand-edited. backed up. editable in Mainsail.
    mcus.cfg                               #   the MCU registry
    updater.conf                           #   tool settings
    bttebb36/klipper.config                #   saved menuconfig answers, per type
    flylllplusbuffer/klipper.config

~/printer_data/klipper-updater/            # generated. not backed up.
    bttebb36/klipper.bin                   #   built firmware
    bttebb36/klipper.build.json            #   build provenance, for staleness
    flylllplusbuffer/klipper.uf2
    .updater.lock                          #   runtime state
    .updater.state
```

## Why the split

**Config goes under `config/` because that directory is special.** Moonraker's
file manager serves it, so those files are editable in Mainsail's own editor —
which means you can adjust a saved `.config` from a browser today, without
waiting for a dedicated Kconfig UI. It's also what every backup scheme picks up,
and the saved menuconfig answers are the one thing here you genuinely cannot
regenerate: lose them and you're redoing menuconfig for every board.

**Firmware binaries deliberately do *not* go in `config/`.** Backup tools like
klipper-backup git-commit everything under that directory, so a `.bin` there
means a binary churn commit after every single build. They're also regenerable
from source plus the saved config, and Mainsail's editor would list files it
can't open. The same reasoning puts the lock and journal in the data tree —
they're runtime state, not configuration.

`~/printer_data/klipper-updater/` follows the pattern other add-ons use, e.g.
`moonraker-timelapse` writing to `~/printer_data/timelapse/`.

## The registry: `mcus.cfg`

Klipper-style, because it lives next to `printer.cfg` and gets hand-edited:

```ini
# Toolhead boards. The buffer patch is specific to this batch.
[mcu flylllplusbuffer]
chipset: stm32f072xb
serials:
    4C0033000957465331323720-if00
    3F0037000957465331323720-if00
klipper_makefile_patches:
    src/Makefile -> src-y += buffer.c
```

| Key | Meaning |
| --- | --- |
| `chipset` | Required. Matches the chipset segment of the `/dev/serial/by-id` name. |
| `serials` | One tracked board per line. |
| `katapult_installed` | Only written when `false`; a board with no bootloader is the exception. |
| `<fw>_extra_args` | Appended to the `make` command line. |
| `<fw>_makefile_patches` | `<file> -> <line>`, appended to that Makefile for one build then reverted. |

`<fw>` is `klipper` or `katapult`.

**Your comments survive.** The panel writes this file structurally when you add a
serial or edit a type, and `configparser` would throw every comment away doing
that — so writes go through a purpose-built round-tripper that preserves
comments, ordering, blank lines, and any keys it doesn't recognise. A note
explaining *why* a board needs a particular patch is exactly the kind of thing
that must not vanish because you tapped a button on your phone.

`makefile_patches` exists because Klipper's build system offers no way to add
`src-y +=` lines from the command line, and a permanent edit would leak into
every other type sharing that chipset and conflict on the next `git pull`.

## Overrides

Every path derives from one `Paths` object, so nothing is hardcoded elsewhere:

| Variable | Replaces |
| --- | --- |
| `KLIPPER_UPDATER_HOME` | `~` |
| `KLIPPER_UPDATER_PRINTER_DATA` | `~/printer_data` |
| `KLIPPER_UPDATER_CONFIG_DIR` | `…/config/klipper-updater` |
| `KLIPPER_UPDATER_DATA_DIR` | `…/klipper-updater` |
| `KLIPPER_UPDATER_FAKE_BUS` | `/dev/serial/by-id` |

## Coming from the old layout

Before this, everything lived in `~/mcus` with the registry as `mcus.json`.
There is no automatic migration — it's a one-time move and the conversion isn't
worth shipping code for. If the tool finds `~/mcus/mcus.json` and no `mcus.cfg`,
it **refuses to start** rather than reporting an empty registry, because that
would let the next `add-type` write a fresh file while your real one sat
untouched.

To move across by hand:

```bash
NEW=~/printer_data/config/klipper-updater
mkdir -p "$NEW" ~/printer_data/klipper-updater

# saved menuconfig answers - the part worth keeping
cp -r ~/mcus/*/                "$NEW"/
find "$NEW" -name '*.bin' -o -name '*.uf2' -o -name '*.build.json' -delete

# write $NEW/mcus.cfg by hand (see mcus.cfg in this repo for a worked example),
# or recreate it with add-type / add-serial
rm -rf ~/mcus     # only once you have checked the new location works
```

Firmware binaries are not worth copying; rebuild them.
