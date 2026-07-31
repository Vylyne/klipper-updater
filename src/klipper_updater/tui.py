"""The interactive numbered menu shown when the tool is run with no arguments.

A direct port of the original's menu layer. It drives the same handler functions
the CLI does, by building an ``argparse.Namespace`` by hand - so there is exactly
one implementation of each action and the two front ends cannot drift.
"""

from __future__ import annotations

import argparse
from typing import Callable, Optional

from . import cli
from .config import Registry
from .devices import find_untracked
from .errors import UpdaterError
from .paths import FW_TARGETS


def _registry() -> Registry:
    return cli.ctx().registry()


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------


def prompt_choice(title: str, options: list[str], allow_cancel: bool = True) -> Optional[int]:
    """Numbered list, looping until a valid selection. Returns a 0-based index,
    or None if cancelled (entering 0)."""
    while True:
        print(f"\n{title}:")
        for i, opt in enumerate(options, 1):
            print(f"  {i}. {opt}")
        if allow_cancel:
            print("  0. Cancel")
        raw = input("> ").strip()
        if allow_cancel and raw == "0":
            return None
        try:
            choice = int(raw)
        except ValueError:
            print("Please enter a number.")
            continue
        if 1 <= choice <= len(options):
            return choice - 1
        print("Out of range, try again.")


def prompt_yn(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


def prompt_nonempty(prompt: str) -> str:
    while True:
        val = input(f"{prompt}: ").strip()
        if val:
            return val
        print("This can't be blank.")


# --------------------------------------------------------------------------
# pickers
# --------------------------------------------------------------------------


def pick_mcu_type(reg: Registry, allow_new: bool = True) -> Optional[str]:
    """Picker over existing types.

    With allow_new, appends an "Add a new MCU type" entry that runs the add-type
    flow inline and returns the new name - so a flow that needs a type never
    dead-ends just because none exist yet.
    """
    types = reg.names()
    if not types:
        if not allow_new:
            print("No MCU types configured yet.")
            return None
        print("No MCU types configured yet - let's add one.")
        return menu_add_mcu_type()

    options = [
        f"{t}  (chipset={reg.get(t).chipset or '?'}, {len(reg.get(t).serials)} serial(s))"
        for t in types
    ]
    if allow_new:
        options.append("+ Add a new MCU type")
    idx = prompt_choice("Select MCU type", options)
    if idx is None:
        return None
    if allow_new and idx == len(types):
        return menu_add_mcu_type()
    return types[idx]


def pick_fw_target() -> Optional[str]:
    idx = prompt_choice("Select firmware target", list(FW_TARGETS))
    if idx is None:
        return None
    return FW_TARGETS[idx]


def pick_serial_for_type(mcu_type: str, reg: Registry) -> Optional[str]:
    """Tracked serials, plus untracked devices detected on the bus, plus manual
    entry. Used by Flash, where either is a valid target."""
    mcu = reg.get(mcu_type)
    tracked = list(mcu.serials)
    untracked = find_untracked(cli.ctx().paths, reg.all_serials(), chipset=mcu.chipset)

    options = [f"{s} (tracked)" for s in tracked]
    options += [f"{d.serial} (untracked, detected on bus)" for d in untracked]
    options.append("Enter serial manually")

    idx = prompt_choice(f"Select a device under '{mcu_type}'", options)
    if idx is None:
        return None
    if idx < len(tracked):
        return tracked[idx]
    if idx < len(tracked) + len(untracked):
        return untracked[idx - len(tracked)].serial
    return prompt_nonempty("Serial string")


def pick_tracked_serial(mcu_type: str, reg: Registry) -> Optional[str]:
    """Only already-tracked serials - used by Remove-serial."""
    tracked = list(reg.get(mcu_type).serials)
    if not tracked:
        print(f"No serials tracked under '{mcu_type}'.")
        return None
    idx = prompt_choice(f"Select a serial to remove from '{mcu_type}'", tracked)
    if idx is None:
        return None
    return tracked[idx]


# --------------------------------------------------------------------------
# action bridge
# --------------------------------------------------------------------------


def call_action(func: Callable[[argparse.Namespace], None], ns: argparse.Namespace) -> None:
    """Invoke a CLI handler from the menu.

    Catches SystemExit and UpdaterError so a failed sub-action returns control to
    the menu loop instead of ending the session - the handler's own output has
    already explained what happened. KeyboardInterrupt is deliberately not caught
    here; it propagates to run_menu() and ends the session, per ^C convention.
    """
    try:
        func(ns)
    except UpdaterError as exc:
        print(f"ERROR: {exc}")
        print("(action did not complete successfully - see messages above)")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        if code not in (0, None):
            print("(action did not complete successfully - see messages above)")


# --------------------------------------------------------------------------
# menu entries
# --------------------------------------------------------------------------


def menu_status() -> None:
    call_action(cli.status_cmd, argparse.Namespace())


def menu_add_mcu_type() -> str:
    type_name = prompt_nonempty("MCU type name (e.g. bttebb36)")
    chipset = prompt_nonempty("Chipset (e.g. stm32g0b1xx)")
    klipper_args = input("Extra klipper make args (blank for none): ").strip()
    katapult_args = input("Extra katapult make args (blank for none): ").strip()
    no_katapult = prompt_yn("Skip katapult (no bootloader)?", default=False)
    call_action(
        cli.add_mcu_type,
        argparse.Namespace(
            type=type_name,
            chipset=chipset,
            klipper_args=klipper_args,
            katapult_args=katapult_args,
            no_katapult=no_katapult,
            force=False,
        ),
    )
    return type_name


def menu_remove_mcu_type() -> None:
    mcu_type = pick_mcu_type(_registry(), allow_new=False)
    if mcu_type is None:
        return
    call_action(cli.remove_mcu_type, argparse.Namespace(type=mcu_type, force=False))


def menu_add_serial() -> None:
    mcu_type = pick_mcu_type(_registry(), allow_new=True)
    if mcu_type is None:
        return
    reg = _registry()  # refresh - pick_mcu_type may have just created this type
    if mcu_type not in reg:
        return
    chipset = reg.get(mcu_type).chipset
    untracked = find_untracked(cli.ctx().paths, reg.all_serials(), chipset=chipset)

    options = [f"{d.serial} (detected on bus)" for d in untracked]
    options.append("Enter serial manually")
    idx = prompt_choice(f"Select a serial to add to '{mcu_type}'", options)
    if idx is None:
        return
    serial = untracked[idx].serial if idx < len(untracked) else prompt_nonempty("Serial string")
    call_action(cli.add_serial, argparse.Namespace(type=mcu_type, serial=serial))


def menu_remove_serial() -> None:
    reg = _registry()
    mcu_type = pick_mcu_type(reg, allow_new=False)
    if mcu_type is None:
        return
    serial = pick_tracked_serial(mcu_type, reg)
    if serial is None:
        return
    call_action(cli.remove_serial, argparse.Namespace(type=mcu_type, serial=serial))


def menu_add_mcu() -> None:
    mcu_type = pick_mcu_type(_registry(), allow_new=True)
    if mcu_type is None:
        return
    call_action(cli.add_mcu, argparse.Namespace(type=mcu_type))


def menu_menuconfig() -> None:
    mcu_type = pick_mcu_type(_registry(), allow_new=True)
    if mcu_type is None:
        return
    fw = pick_fw_target()
    if fw is None:
        return
    call_action(
        cli.make_menuconfig_cmd, argparse.Namespace(type=mcu_type, fw=fw, no_pause=False)
    )


def menu_build() -> None:
    mcu_type = pick_mcu_type(_registry(), allow_new=True)
    if mcu_type is None:
        return
    fw = pick_fw_target()
    if fw is None:
        return
    call_action(cli.build_fw_cmd, argparse.Namespace(type=mcu_type, fw=fw, jobs=None))


def menu_flash() -> None:
    reg = _registry()
    mcu_type = pick_mcu_type(reg, allow_new=False)
    if mcu_type is None:
        return
    serials = list(reg.get(mcu_type).serials)

    scope_options = ["Flash every tracked serial under this type"]
    if serials:
        scope_options.append("Flash one specific device")
    idx = prompt_choice(f"Flash scope for '{mcu_type}'", scope_options)
    if idx is None:
        return

    if idx == 0:
        ns = argparse.Namespace(type=mcu_type, serial=None, yes=False)
    else:
        serial = pick_serial_for_type(mcu_type, reg)
        if serial is None:
            return
        ns = argparse.Namespace(type=mcu_type, serial=serial, yes=False)
    call_action(cli.flash_fw_cmd, ns)


def menu_update_all() -> None:
    call_action(cli.update_all, argparse.Namespace(yes=False, jobs=None))


MENU_ITEMS: list[tuple[str, Callable[[], object]]] = [
    ("List MCU types / status", menu_status),
    ("Add MCU type", menu_add_mcu_type),
    ("Remove MCU type", menu_remove_mcu_type),
    ("Add serial to existing type", menu_add_serial),
    ("Remove serial from a type", menu_remove_serial),
    ("Guided add-mcu (new physical board)", menu_add_mcu),
    ("Menuconfig", menu_menuconfig),
    ("Build firmware", menu_build),
    ("Flash device(s)", menu_flash),
    ("Update all (rebuild + reflash everything)", menu_update_all),
]


def run_menu() -> None:
    try:
        while True:
            print("\n=== Klipper/Katapult Firmware Manager ===")
            if cli.ctx().settings.dry_run:
                print("  (dry-run mode: nothing will actually be built or flashed)")
            for i, (label, _) in enumerate(MENU_ITEMS, 1):
                print(f"  {i}. {label}")
            print("  0. Exit")
            raw = input("> ").strip()
            if raw == "0":
                print("Goodbye.")
                return
            try:
                choice = int(raw)
            except ValueError:
                print("Please enter a number.")
                continue
            if not (1 <= choice <= len(MENU_ITEMS)):
                print("Out of range, try again.")
                continue
            MENU_ITEMS[choice - 1][1]()
            input("\nPress Enter to continue...")
    except (KeyboardInterrupt, EOFError):
        print("\nExiting.")
