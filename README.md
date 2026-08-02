# mcu-updater

Firmware management for a Klipper printer with more than one MCU. It keeps a
registry of your board types and the USB serials of the physical boards of each
type, remembers each type's `menuconfig` answers, builds Klipper and Katapult,
and flashes every board — so "Klipper updated, now reflash six toolheads" is one
command instead of an afternoon.

Linux only (it needs `/dev/serial/by-id`, systemd and `sudo`). Python 3.9+,
standard library only — no pip dependencies, no virtualenv.

## Requirements

- Klipper checked out at `~/klipper`
- [Katapult](https://github.com/Arksine/katapult) at `~/katapult` (for the
  `flashtool.py` used to flash over USB/CAN)
- An ARM toolchain and `make`, i.e. whatever already builds Klipper for you
- `dfu-util`, only for installing Katapult onto a brand-new STM32 board
- Passwordless `sudo` for `systemctl {start,stop} klipper`

## Usage

```bash
~/mcu-updater/src/updatefw.py            # interactive menu
~/mcu-updater/src/updatefw.py status     # what's tracked, built, and online
```

| Command | What it does |
| --- | --- |
| `status` | Every tracked type: whether its firmware is stale, and whether each board is online as Klipper, sitting in the Katapult bootloader, or offline |
| `add-type -t NAME -c CHIPSET` | Register a board model |
| `add-serial -t NAME -s SERIAL` | Track a physical board under a type |
| `remove-type` / `remove-serial` | The inverse |
| `menuconfig -t NAME -f klipper\|katapult` | Configure a type, saved per type so it survives rebuilds |
| `build -t NAME -f klipper\|katapult` | Compile and stage the artifact |
| `flash -t NAME [-s SERIAL]` | Flash one board, or every board of a type |
| `update-all` | Stop Klipper, rebuild and reflash everything, start Klipper |
| `add-mcu -t NAME` | Guided first-time Katapult install on a new board |

Useful flags: `--dry-run` (global) rehearses anything without building or
flashing a thing; `-j N` on `build`/`update-all` for parallel make; `-y` to skip
confirmation prompts; `--force` where a prompt guards something destructive.

## Configuration

### `~/printer_data/config/mcu-updater/mcus.cfg`

The registry. Klipper-style, because it sits next to `printer.cfg` and gets
hand-edited — and **your comments survive** the panel writing to it.

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

Per-type keys:

- **`chipset`** — required; matches the chipset segment of the by-id name.
- **`serials`** — one tracked board per line.
- **`katapult_installed`** — only written when `false`; a board with no
  bootloader is the exception.
- **`<fw>_extra_args`** — appended to the `make` command line.
- **`<fw>_makefile_patches`** — `<file> -> <line>`, appended to that Makefile
  *for one build only*, then reverted. This exists because Klipper's build system
  has no way to add `src-y +=` lines from the command line, and a permanent edit
  would leak into every other type sharing that chipset and conflict on the next
  `git pull` of Klipper.

`mcus.cfg` at the repo root is a real example copied from a working printer.

> **`makefile_patches` makes your firmware version say `-dirty`.** Klipper stamps
> the version from git while the patch is applied, so the tree is briefly dirty.
> `v0.13.0-712-g6d43f8b3-dirty-...` is expected for a patched type and does not
> mean you have local Klipper modifications.

### `~/printer_data/config/mcu-updater/updater.conf`

Optional; every value has a default.

```ini
[updater]
make_jobs = 0              ; 0 = no -j flag, negative = one per CPU
clean_before_build = true  ; leave on: a stale object mix flashes a wrong binary
service = klipper          ; klipper-1, klipper-2... for KIAUH multi-instance
dry_run = false
```

## Layout

Files are split by what they are — see [docs/layout.md](docs/layout.md) for the
reasoning.

```
~/mcu-updater/src/updatefw.py        entry point (a shim onto the package)

~/printer_data/config/mcu-updater/   hand-edited, backed up, editable in Mainsail
    mcus.cfg                             the registry
    updater.conf                         tool settings
    <type>/<fw>.config                   saved menuconfig answers

~/printer_data/mcu-updater/          generated, not backed up
    <type>/<fw>.bin                      built firmware
    <type>/<fw>.build.json               build provenance, for staleness checks
```

Config lives under `config/` so it's backed up and reachable by Mainsail's own
editor. Firmware binaries deliberately don't: backup tools git-commit everything
in that directory, so a `.bin` there means a binary churn commit after every
build — and they're regenerable anyway.

Staleness compares recorded provenance — the source-tree commit and a hash of
the `.config` used — rather than file timestamps. So `status` correctly reports
every board as stale after you pull Klipper, and a stray `touch` doesn't lie.

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check src tests
```

The whole test suite runs on any OS with no printer attached, because every
filesystem location comes from a `Paths` object that honours these overrides:

| Variable | Replaces |
| --- | --- |
| `KLIPPER_UPDATER_HOME` | `~` |
| `KLIPPER_UPDATER_PRINTER_DATA` | `~/printer_data` |
| `KLIPPER_UPDATER_CONFIG_DIR` | `…/config/mcu-updater` |
| `KLIPPER_UPDATER_DATA_DIR` | `…/mcu-updater` |
| `KLIPPER_UPDATER_FAKE_BUS` | `/dev/serial/by-id` |

`KLIPPER_UPDATER_FAKE_BUS` is worth knowing about: `touch` and `rm` files named
`usb-<fw>_<chipset>_<serial>` in that directory to simulate a board
re-enumerating between Klipper and Katapult, and combine it with `--dry-run` for
a complete end-to-end rehearsal with no hardware and no risk.
