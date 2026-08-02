# Where everything lives

Files are split by *what they are*, following the `printer_data` conventions.

```
~/printer_data/config/mcu-updater/     # hand-edited. backed up. editable in Mainsail.
    mcu-updater.cfg                        #   [updater] settings + one [mcu ...] per board
    bttebb36/klipper.config                #   saved menuconfig answers, per type
    flylllplusbuffer/klipper.config

~/printer_data/mcu-updater/            # generated. not backed up.
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

`~/printer_data/mcu-updater/` follows the pattern other add-ons use, e.g.
`moonraker-timelapse` writing to `~/printer_data/timelapse/`.

## One config file: `mcu-updater.cfg`

Klipper-style, because it lives next to `printer.cfg` and gets hand-edited.
Settings and the registry share it — `.cfg` sections namespace cleanly, so there
is one file to find, one file to back up, and one file to open in Mainsail:

```ini
# Tool settings. Every value has a default, so this section is optional.
[updater]
enable_flashing: true

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

The `[updater]` section holds `make_jobs`, `clean_before_build`, `service`,
`service_backend`, `dry_run`, `enable_flashing`, `allow_flash_while_printing`
and `log_ring_size`. All optional.

**Edit the existing `[updater]` section rather than appending a second one.** A
duplicate section is refused outright: first-wins would mean
`enable_flashing: true` in the later copy silently doing nothing, which is a
miserable thing to debug.

**Your comments survive.** The panel writes this file structurally when you add a
serial or edit a type, and `configparser` would throw every comment away doing
that — so writes go through a purpose-built round-tripper that preserves
comments, ordering, blank lines, and any keys it doesn't recognise. A note
explaining *why* a board needs a particular patch is exactly the kind of thing
that must not vanish because you tapped a button on your phone.

`makefile_patches` exists because Klipper's build system offers no way to add
`src-y +=` lines from the command line, and a permanent edit would leak into
every other type sharing that chipset and conflict on the next `git pull`.

## The systemd unit is called `mcu-updater`

Not `klipper-updater`, and that is load-bearing. KIAUH discovers instances with
`^<component>(-[0-9a-zA-Z]+)?\.service$`, so `klipper-updater.service` matches
the *Klipper* pattern: KIAUH treats it as a Klipper instance called "updater",
opens it to read `EnvironmentFile=`, and its whole menu crashes if the unit is
not world-readable.

`klipper_updater` and `klipper-mcu-updater` happen to slip past that exact regex
too, but only via quirks - an underscore is not a hyphen, and the character class
forbids a second hyphen. A name that starts with no component name at all is safe
by construction instead.

The unit must also equal the `[update_manager <name>]` section, because Moonraker
only accepts a `managed_services` value matching that, `klipper`, or `moonraker`.
Both constraints point at the same answer.

The unit is installed mode 0644 with `install`, not `cp`. `mktemp` creates 0600
and `cp` carries that mode across, which is how the KIAUH crash was triggered in
the first place.

## Overrides

Every path derives from one `Paths` object, so nothing is hardcoded elsewhere:

| Variable | Replaces |
| --- | --- |
| `KLIPPER_UPDATER_HOME` | `~` |
| `KLIPPER_UPDATER_PRINTER_DATA` | `~/printer_data` |
| `KLIPPER_UPDATER_CONFIG_DIR` | `…/config/mcu-updater` |
| `KLIPPER_UPDATER_DATA_DIR` | `…/mcu-updater` |
| `KLIPPER_UPDATER_FAKE_BUS` | `/dev/serial/by-id` |

## Coming from the old layout

Two moves have happened, and neither has automatic migration — each is a
one-time job and the conversion isn't worth shipping code for. In both cases the
tool **refuses to start** rather than reporting an empty registry, because that
would let the next `add-type` write a fresh file while your real one sat
untouched:

- `~/mcus/mcus.json` → `~/printer_data/config/mcu-updater/mcu-updater.cfg`
  (JSON to `.cfg`, and out of the home directory)
- `mcus.cfg` + `updater.conf` → the single `mcu-updater.cfg`. Rename the
  registry, then paste your old `[updater]` section into the top of it. A
  leftover `updater.conf` is warned about, not read.

To move across by hand:

```bash
NEW=~/printer_data/config/mcu-updater
mkdir -p "$NEW" ~/printer_data/mcu-updater

# saved menuconfig answers - the part worth keeping
cp -r ~/mcus/*/                "$NEW"/
find "$NEW" -name '*.bin' -o -name '*.uf2' -o -name '*.build.json' -delete

# write $NEW/mcu-updater.cfg by hand (mcu-updater.cfg in this repo is a worked
# example), or recreate it with add-type / add-serial
rm -rf ~/mcus     # only once you have checked the new location works
```

Firmware binaries are not worth copying; rebuild them.
