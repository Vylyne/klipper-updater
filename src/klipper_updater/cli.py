"""Command line interface.

This module and :mod:`tui` are the only places allowed to call ``input()``,
``sys.exit()`` or bare ``print()``. Everything below them raises and reports.

Behavioural parity with the original single-file script is deliberate: same nine
subcommands, same flags, same messages, same exit codes, and a bare invocation
still drops into the interactive menu. New flags (``--dry-run``, ``-j``,
``--force``, the ``status`` subcommand) are additive.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from typing import Optional

from . import __version__
from .build import build, menuconfig_tty, staleness
from .config import Registry
from .devices import (
    STATE_KATAPULT,
    STATE_KLIPPER,
    device_state,
    find_untracked,
)
from .errors import (
    ConfigNotFoundError,
    DuplicateTypeError,
    UnknownSerialError,
    UpdaterError,
)
from .flash import adoptable_devices, flash_initial_bootloader, flash_katapult
from .lock import exclusive
from .paths import FW_TARGETS, Paths
from .service import Journal, klipper_stopped, make_controller, reconcile
from .settings import Settings, load_settings

# --------------------------------------------------------------------------
# process-wide context
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Context:
    paths: Paths
    settings: Settings

    def registry(self) -> Registry:
        """Always a fresh read - the file may have changed under us."""
        return Registry.load(self.paths)


_ctx: Optional[Context] = None


def ctx() -> Context:
    """The context main() built. A module global so handlers can keep their
    ``(args)`` signature, which is what lets the menu drive them with a
    hand-built Namespace."""
    global _ctx
    if _ctx is None:
        paths = Paths.from_env()
        _ctx = Context(paths=paths, settings=load_settings(paths.settings_file))
    return _ctx


def stdout_reporter(stream: str, line: str) -> None:
    if stream in ("stderr", "warn"):
        print(line, file=sys.stderr)
    elif stream == "error":
        print(f"ERROR: {line}", file=sys.stderr)
    elif stream == "cmd":
        print(f"+ {line}")
    else:
        print(line)


def _confirm(prompt: str) -> bool:
    resp = input(f"{prompt} [y/N]: ").strip().lower()
    return resp in ("y", "yes")


# --------------------------------------------------------------------------
# registry commands
# --------------------------------------------------------------------------


def add_mcu_type(args: argparse.Namespace) -> None:
    c = ctx()
    reg = c.registry()

    if args.type in reg and not args.force:
        print(
            f"MCU Type '{args.type}' already exists:\n"
            f"{json.dumps(reg.get(args.type).to_json(), indent=2)}"
        )
        if not _confirm("Overwrite?"):
            print("Aborting add.")
            return

    try:
        reg.add_type(
            args.type,
            args.chipset,
            klipper_args=args.klipper_args,
            katapult_args=args.katapult_args,
            katapult_installed=not args.no_katapult,
            overwrite=True,
        )
    except DuplicateTypeError:  # pragma: no cover - overwrite=True can't raise it
        raise
    reg.save(c.paths)
    print(f"Successfully added/updated MCU Type: {args.type}")


def add_serial(args: argparse.Namespace) -> None:
    c = ctx()
    reg = c.registry()
    reg.get(args.type)  # raises UnknownTypeError
    if reg.add_serial(args.type, args.serial):
        reg.save(c.paths)
        print(f"Added serial {args.serial} to {args.type}")
    else:
        print(f"Serial {args.serial} already exists under {args.type}")


def remove_mcu_type(args: argparse.Namespace) -> None:
    c = ctx()
    reg = c.registry()
    mcu = reg.get(args.type)

    if not args.force:
        n = len(mcu.serials)
        if not _confirm(f"Remove type '{args.type}' and its {n} tracked serial(s)?"):
            print("Aborted.")
            return

    reg.remove_type(args.type)
    reg.save(c.paths)
    print(f"Removed MCU Type: {args.type}")


def remove_serial(args: argparse.Namespace) -> None:
    c = ctx()
    reg = c.registry()
    reg.get(args.type)
    if reg.remove_serial(args.type, args.serial):
        reg.save(c.paths)
        print(f"Removed serial {args.serial} from {args.type}")
    else:
        print(f"Serial {args.serial} isn't tracked under {args.type} - nothing to do.")


def status_cmd(args: argparse.Namespace) -> None:
    """Read-only overview. Promoted from menu-only to a real subcommand."""
    c = ctx()
    reg = c.registry()
    if not reg:
        print("No MCU types configured yet.")
        return

    for name in reg.names():
        mcu = reg.get(name)
        print(f"\n{name}  (chipset={mcu.chipset or '?'})")

        for fw in FW_TARGETS:
            if fw == "katapult" and not mcu.katapult_installed:
                continue
            stale, reason = staleness(c.paths, name, fw)
            if reason == "never_built":
                print(f"  {fw}: not built")
            elif stale:
                print(f"  {fw}: STALE ({reason})")
            else:
                print(f"  {fw}: up to date")

        if not mcu.serials:
            print("  (no tracked serials)")
            continue
        for serial in mcu.serials:
            state, _ = device_state(c.paths, mcu.chipset, serial)
            label = {
                STATE_KLIPPER: "online (klipper)",
                STATE_KATAPULT: "online (katapult/bootloader)",
            }.get(state, "offline" if state == "offline" else f"online ({state})")
            print(f"  - {serial}: {label}")

    untracked = find_untracked(c.paths, reg.all_serials())
    if untracked:
        print("\nUntracked devices on the bus:")
        for dev in untracked:
            print(f"  - {dev.serial}  (fw={dev.fw}, chipset={dev.chipset or '?'})")


# --------------------------------------------------------------------------
# build commands
# --------------------------------------------------------------------------


def make_menuconfig_cmd(args: argparse.Namespace) -> None:
    c = ctx()
    c.registry().get(args.type)
    menuconfig_tty(c.paths, args.type, args.fw, pause=not getattr(args, "no_pause", False))


def _build_interactive(c: Context, mcu_type: str, fw: str, jobs: Optional[int] = None):
    """Build, offering menuconfig first if this type has never been configured.

    The original did this inside do_build via an `interactive` flag. It lives here
    now so the core stays safe to call from a daemon, which must never touch an
    ncurses UI.
    """
    reg = c.registry()
    try:
        return build(
            c.paths, reg, c.settings, mcu_type, fw, reporter=stdout_reporter, jobs=jobs
        )
    except ConfigNotFoundError:
        print(f"Configuration file not found for {mcu_type} ({fw}). Launching menuconfig...")
        menuconfig_tty(c.paths, mcu_type, fw)
        return build(
            c.paths, reg, c.settings, mcu_type, fw, reporter=stdout_reporter, jobs=jobs
        )


def build_fw_cmd(args: argparse.Namespace) -> None:
    c = ctx()
    with exclusive(c.paths, f"build {args.fw}/{args.type}"):
        _build_interactive(c, args.type, args.fw, jobs=getattr(args, "jobs", None))


# --------------------------------------------------------------------------
# flash commands
# --------------------------------------------------------------------------


def flash_fw_cmd(args: argparse.Namespace) -> None:
    c = ctx()
    reg = c.registry()

    if not args.serial and not args.type:
        print("ERROR: provide -s <serial>, -t <type>, or both.", file=sys.stderr)
        sys.exit(1)

    if not args.yes and not _confirm(
        f"Flashing requires stopping the '{c.settings.service}' service "
        f"(aborts any active print!). Continue?"
    ):
        print("Aborted.")
        return

    svc = make_controller(c.settings)

    # Whole type: flash every tracked serial under it.
    if args.type and not args.serial:
        mcu = reg.get(args.type)
        if not mcu.serials:
            print(f"No serials tracked under '{args.type}'.", file=sys.stderr)
            sys.exit(1)

        failures = []
        with exclusive(c.paths, f"flash type {args.type}"):
            with klipper_stopped(c.paths, svc, f"flash {args.type}", reporter=stdout_reporter):
                for serial in mcu.serials:
                    try:
                        flash_katapult(
                            c.paths,
                            c.settings,
                            args.type,
                            mcu.chipset,
                            serial,
                            reporter=stdout_reporter,
                        )
                    except UpdaterError as exc:
                        print(f"ERROR: {exc}", file=sys.stderr)
                        failures.append(serial)
        if failures:
            print(f"Failures: {', '.join(failures)}", file=sys.stderr)
            sys.exit(1)
        return

    # Single device.
    if args.type:
        try:
            mcu_type = reg.resolve_serial(args.serial, args.type)
        except UnknownSerialError:
            # Untracked under this type, and not tracked elsewhere (that case
            # raises SerialTrackedElsewhereError and is refused outright).
            if not _confirm(
                f"Serial '{args.serial}' isn't tracked under '{args.type}' yet. Add it now?"
            ):
                print("Aborted.")
                sys.exit(1)
            reg.add_serial(args.type, args.serial)
            reg.save(c.paths)
            print(f"Added serial {args.serial} to {args.type}")
            mcu_type = args.type
    else:
        mcu_type = reg.resolve_serial(args.serial)
        print(f"Resolved serial {args.serial} -> type '{mcu_type}'")

    chipset = reg.get(mcu_type).chipset
    with exclusive(c.paths, f"flash {mcu_type}/{args.serial}"):
        with klipper_stopped(c.paths, svc, f"flash {args.serial}", reporter=stdout_reporter):
            flash_katapult(
                c.paths, c.settings, mcu_type, chipset, args.serial, reporter=stdout_reporter
            )


def update_all(args: argparse.Namespace) -> None:
    c = ctx()
    reg = c.registry()
    if not reg:
        print("No MCU types configured.", file=sys.stderr)
        sys.exit(1)

    if not args.yes and not _confirm(
        f"This stops the '{c.settings.service}' service (aborts any active print!), "
        f"rebuilds + reflashes every tracked MCU, then restarts it. Continue?"
    ):
        print("Aborted.")
        return

    svc = make_controller(c.settings)
    failures: list[tuple[str, Optional[str]]] = []

    with exclusive(c.paths, "update-all"):
        # Klipper stays down across the builds too. That matches the original;
        # narrowing the window to just the flashes is a later change, not a
        # silent one.
        with klipper_stopped(c.paths, svc, "update-all", reporter=stdout_reporter):
            for name in reg.names():
                mcu = reg.get(name)
                print(f"\n=== {name} ===")
                try:
                    result = build(
                        c.paths,
                        reg,
                        c.settings,
                        name,
                        "klipper",
                        reporter=stdout_reporter,
                        jobs=getattr(args, "jobs", None),
                    )
                except UpdaterError as exc:
                    print(f"ERROR: {exc}", file=sys.stderr)
                    failures.append((name, None))
                    continue

                for serial in mcu.serials:
                    try:
                        flash_katapult(
                            c.paths,
                            c.settings,
                            name,
                            mcu.chipset,
                            serial,
                            fw_bin=result.bin_path,
                            reporter=stdout_reporter,
                        )
                    except UpdaterError as exc:
                        print(f"ERROR: {exc}", file=sys.stderr)
                        failures.append((name, serial))

    if failures:
        print("\nCompleted with failures:")
        for failed_type, failed_serial in failures:
            print(
                f"  - {failed_type}"
                + (f" / {failed_serial}" if failed_serial else " (build failed)")
            )
        sys.exit(1)
    print("\nAll MCU types built and flashed successfully.")


def add_mcu(args: argparse.Namespace) -> None:
    c = ctx()
    reg = c.registry()
    mcu = reg.get(args.type)
    chipset = mcu.chipset

    with exclusive(c.paths, f"add-mcu {args.type}"):
        # A brand new type has no saved .config, so this launches menuconfig.
        result = _build_interactive(c, args.type, "katapult")

        before = set(reg.all_serials()) | {
            d.serial for d in find_untracked(c.paths, reg.all_serials())
        }
        flash_initial_bootloader(
            c.paths, c.settings, chipset, result.bin_path, reporter=stdout_reporter
        )

        print("Waiting for the device to enumerate as Katapult...")
        candidates = adoptable_devices(c.paths, before, chipset)

    if not candidates:
        print(
            f"No new, unassigned Katapult device found for chipset '{chipset}'. "
            f"Check `ls /dev/serial/by-id/` and use 'add-serial' manually."
        )
        return

    reg = c.registry()
    for dev in candidates:
        if _confirm(
            f"Found unassigned Katapult device: {dev.serial} ({dev.path}). "
            f"Add it to '{args.type}'?"
        ):
            if reg.add_serial(args.type, dev.serial):
                reg.save(c.paths)
                print(f"Added serial {dev.serial} to {args.type}")


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Klipper/Katapult Firmware Management Utility"
    )
    parser.add_argument("--version", action="version", version=f"mcu-updater {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Echo commands and fake their output instead of building/flashing",
    )
    subparsers = parser.add_subparsers(title="Commands", dest="command", required=True)

    p = subparsers.add_parser("add-type", help="Add a new MCU type configuration")
    p.add_argument("-t", "--type", required=True, help="Unique MCU Type Name (e.g., bttebb36)")
    p.add_argument("-c", "--chipset", required=True, help="Chipset (e.g., stm32g0b1xx)")
    p.add_argument("--klipper-args", default="", help="Extra make arguments for Klipper")
    p.add_argument("--katapult-args", default="", help="Extra make arguments for Katapult")
    p.add_argument("--no-katapult", action="store_true", help="Set Katapult installed to false")
    p.add_argument("--force", action="store_true", help="Overwrite without prompting")
    p.set_defaults(func=add_mcu_type)

    p = subparsers.add_parser("add-serial", help="Add a serial number to an existing MCU type")
    p.add_argument("-t", "--type", required=True, help="MCU Type Name")
    p.add_argument("-s", "--serial", required=True, help="The device serial string")
    p.set_defaults(func=add_serial)

    p = subparsers.add_parser(
        "remove-type", help="Remove an MCU type configuration and its tracked serials"
    )
    p.add_argument("-t", "--type", required=True, help="MCU Type Name")
    p.add_argument("--force", action="store_true", help="Skip the confirmation prompt")
    p.set_defaults(func=remove_mcu_type)

    p = subparsers.add_parser("remove-serial", help="Remove a tracked serial from an MCU type")
    p.add_argument("-t", "--type", required=True, help="MCU Type Name")
    p.add_argument("-s", "--serial", required=True, help="The device serial string")
    p.set_defaults(func=remove_serial)

    p = subparsers.add_parser("status", help="Show tracked types, staleness, and bus state")
    p.set_defaults(func=status_cmd)

    p = subparsers.add_parser("menuconfig", help="Launch make menuconfig for a specific target")
    p.add_argument("-t", "--type", required=True, help="MCU Type Name")
    p.add_argument("-f", "--fw", required=True, choices=list(FW_TARGETS), help="Firmware target")
    p.add_argument("--no-pause", action="store_true", help="Skip the 'press Enter' prompt")
    p.set_defaults(func=make_menuconfig_cmd)

    p = subparsers.add_parser("build", help="Compile the firmware for a specific target")
    p.add_argument("-t", "--type", required=True, help="MCU Type Name")
    p.add_argument("-f", "--fw", required=True, choices=list(FW_TARGETS), help="Firmware target")
    p.add_argument("-j", "--jobs", type=int, default=None, help="Parallel make jobs (0 disables -j)")
    p.set_defaults(func=build_fw_cmd)

    p = subparsers.add_parser(
        "flash", help="Flash a single tracked device with its built klipper.bin"
    )
    p.add_argument(
        "-t", "--type", default=None,
        help="MCU Type Name (optional - inferred from the serial if omitted)",
    )
    p.add_argument("-s", "--serial", default=None, help="Device serial (must already be tracked)")
    p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    p.set_defaults(func=flash_fw_cmd)

    p = subparsers.add_parser(
        "update-all",
        help="Build + flash klipper for every tracked MCU type/device, "
        "stopping/restarting klipper around it",
    )
    p.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    p.add_argument("-j", "--jobs", type=int, default=None, help="Parallel make jobs")
    p.set_defaults(func=update_all)

    p = subparsers.add_parser(
        "add-mcu", help="Interactive routine to setup, build, and flash a new MCU"
    )
    p.add_argument("-t", "--type", required=True, help="MCU Type Name")
    p.set_defaults(func=add_mcu)

    return parser


def main(argv: Optional[list[str]] = None) -> None:
    global _ctx

    argv = list(sys.argv[1:] if argv is None else argv)
    paths = Paths.from_env()

    try:
        settings = load_settings(paths.settings_file)
    except UpdaterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    parser = build_parser()

    # Bare invocation drops into the interactive menu, as it always has.
    if not argv:
        _ctx = Context(paths=paths, settings=settings)
        _reconcile_quietly(_ctx)
        from . import tui

        tui.run_menu()
        return

    args = parser.parse_args(argv)
    if getattr(args, "dry_run", False):
        settings.dry_run = True
    _ctx = Context(paths=paths, settings=settings)
    _reconcile_quietly(_ctx)

    try:
        args.func(args)
    except UpdaterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


def _reconcile_quietly(c: Context) -> None:
    """If a previous run died with klipper stopped, put it back."""
    if Journal(c.paths).pending() is None:
        return
    if os.name == "nt":  # no systemd on the dev box
        return
    try:
        reconcile(c.paths, make_controller(c.settings), reporter=stdout_reporter)
    except Exception as exc:  # noqa: BLE001 - never block the requested command
        print(f"WARNING: could not reconcile previous run: {exc}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    main()
