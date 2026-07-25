#!/usr/bin/env python3
import os
import json
import argparse
import subprocess
import sys
import time
import contextlib
import glob

# --- CONFIGURATION ---
SETTINGS_PATH = os.path.expanduser("~/mcus")
MCUS_JSON = os.path.join(SETTINGS_PATH, "mcus.json")
FLASHTOOL = os.path.expanduser("~/katapult/scripts/flashtool.py")

# systemd service name for Klipper. Check `systemctl list-units | grep klipper`
# if you run multiple printer instances (KIAUH multi-instance uses klipper-1, etc).
KLIPPER_SERVICE = "klipper"

# Device naming under /dev/serial/by-id/usb-<FW>_<chipset>_<serial>. Confirm
# the casing matches what `ls /dev/serial/by-id/` actually shows you - it's
# been seen as both "usb-klipper_..." and "usb-Klipper_..." depending on board.
KLIPPER_FW_NAME = "Klipper"
KATAPULT_FW_NAME = "katapult"

BOOTLOADER_WAIT_TIMEOUT = 15  # seconds to wait for a device to re-enumerate after a bootloader request

# --- DATA ACCESS LAYER ---

def load_data():
    """Loads the JSON data safely."""
    if not os.path.exists(MCUS_JSON) or os.path.getsize(MCUS_JSON) == 0:
        return {}
    with open(MCUS_JSON, 'r') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def save_data(data):
    """Saves the JSON data atomically."""
    os.makedirs(os.path.dirname(MCUS_JSON), exist_ok=True)
    tmp_path = MCUS_JSON + ".tmp"
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, MCUS_JSON)

@contextlib.contextmanager
def apply_makefile_patches(mcu_type, fw, fw_dir):
    """Temporarily appends configured lines to source-tree Makefiles for the
    duration of the `with` block, then restores the original file bytes
    afterward - even if the build raises or fails. This is deliberately NOT
    a permanent edit: a permanent line gated on e.g. CONFIG_MACH_STM32F072
    would leak into every other MCU type that happens to share that chipset,
    and a tracked file like src/stm32/Makefile gets clobbered/conflicts on
    the next `git pull` of klipper anyway. Reads
    data[type][fw]["makefile_patches"] = [{"file": "...", "line": "..."}]
    where "file" is relative to fw_dir (e.g. "src/stm32/Makefile")."""
    data = load_data()
    patches = data.get(mcu_type, {}).get(fw, {}).get("makefile_patches", [])
    backups = []  # (target_path, original_bytes_or_None)
    try:
        for patch in patches:
            target = os.path.join(fw_dir, patch["file"])
            line = patch["line"]
            if not os.path.exists(target):
                print(f"WARNING: patch target {target} not found, skipping '{line}'", file=sys.stderr)
                continue
            with open(target, "rb") as f:
                original = f.read()
            if line.encode() in original:
                print(f"NOTE: '{line}' already present in {target} (left over from an "
                      f"interrupted run?) - leaving it alone, not reverting it.")
                backups.append((target, None))
                continue
            backups.append((target, original))
            with open(target, "ab") as f:
                if not original.endswith(b"\n"):
                    f.write(b"\n")
                f.write((line + "\n").encode())
            print(f"Temporarily patched {target}: added '{line}'")
        yield
    finally:
        for target, original in backups:
            if original is None:
                continue
            with open(target, "wb") as f:
                f.write(original)
            print(f"Restored {target} to its original contents")

def get_extra_args(mcu_type, fw):
    data = load_data()
    if mcu_type in data and fw in data[mcu_type]:
        extra_args = data[mcu_type][fw].get("extra_args", "")
        print(F"Extra args: '{extra_args}'")
        return extra_args
    return ""

# --- BUILD CORE (plain functions - callable from CLI handlers or internally) ---

def do_menuconfig(mcu_type, fw):
    config_dir = os.path.join(SETTINGS_PATH, mcu_type)
    os.makedirs(config_dir, exist_ok=True)
    config_file = os.path.join(config_dir, f"{fw}.config")

    fw_dir = os.path.expanduser(f"~/{fw}")
    if not os.path.exists(fw_dir):
        print(f"ERROR: Source directory {fw_dir} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Making config for {mcu_type} with {fw}")
    input("Press Enter to continue to menuconfig...")

    kconfig_arg = f"KCONFIG_CONFIG={config_file}"
    subprocess.run(["make", kconfig_arg, "menuconfig"], cwd=fw_dir, stdin=sys.stdin, stdout=sys.stdout)

def do_build(mcu_type, fw, interactive=True):
    """Builds firmware for one MCU type/fw target. Returns the path to the
    compiled .bin on success, or None on failure (never raises/exits, so
    callers like update_all can keep going across multiple types)."""
    config_file = os.path.join(SETTINGS_PATH, mcu_type, f"{fw}.config")
    fw_out = os.path.join(SETTINGS_PATH, mcu_type, f"{fw}.bin")
    fw_dir = os.path.expanduser(f"~/{fw}")

    if not os.path.exists(config_file):
        if not interactive:
            print(f"ERROR: no saved config for {mcu_type} ({fw}) at {config_file}. "
                  f"Run 'menuconfig -t {mcu_type} -f {fw}' once first.", file=sys.stderr)
            return None
        print(f"Configuration file not found for {mcu_type} ({fw}). Launching menuconfig...")
        do_menuconfig(mcu_type, fw)

    extra_args = get_extra_args(mcu_type, fw).split()

    print(f"Building {fw} for {mcu_type}...")
    kconfig_arg = f"KCONFIG_CONFIG={config_file}"

    with apply_makefile_patches(mcu_type, fw, fw_dir):
        subprocess.run(["make", kconfig_arg, "clean"], cwd=fw_dir)
        make_cmd = ["make", kconfig_arg] + extra_args
        print(f"{make_cmd}")
        res = subprocess.run(make_cmd, cwd=fw_dir)

    if res.returncode != 0:
        print("ERROR: Firmware build failed.", file=sys.stderr)
        return None

    compiled_bin = os.path.join(fw_dir, "out", f"{fw}.bin")
    if not os.path.exists(compiled_bin):
        print("ERROR: Compilation succeeded but output binary was not found.", file=sys.stderr)
        return None

    os.makedirs(os.path.dirname(fw_out), exist_ok=True)
    with open(compiled_bin, 'rb') as src, open(fw_out, 'wb') as dst:
        dst.write(src.read())
    print(f"Firmware built and copied to {fw_out}")
    return fw_out

# --- FLASHING ---

def device_path(fw_name, chipset, serial):
    return f"/dev/serial/by-id/usb-{fw_name}_{chipset}_{serial}"

def find_device_path(chipset, serial):
    return glob.glob(f"/dev/serial/by-id/usb-*_{chipset}_{serial}")[0]

def wait_for_path(path, timeout=BOOTLOADER_WAIT_TIMEOUT):
    waited = 0.0
    while waited < timeout:
        if os.path.exists(path):
            return True
        time.sleep(0.5)
        waited += 0.5
    return False

def flash_device(mcu_type, chipset, serial, fw_bin=None):
    """Flashes one device via katapult's flashtool.py. If the device is
    currently running Klipper (not already in the bootloader), this requests
    the bootloader first and waits for it to re-enumerate as a Katapult
    device before flashing - flashtool.py's documented two-step process for
    devices it can't auto-detect into bootloader mode."""
    if not os.path.exists(FLASHTOOL):
        print(f"ERROR: flashtool.py not found at {FLASHTOOL}", file=sys.stderr)
        return False

    if fw_bin is None:
        fw_bin = os.path.join(SETTINGS_PATH, mcu_type, "klipper.bin")
    if not os.path.exists(fw_bin):
        print(f"ERROR: firmware binary not found at {fw_bin}. Build it first.", file=sys.stderr)
        return False

    katapult_dev = device_path(KATAPULT_FW_NAME, chipset, serial)
    klipper_dev = device_path(KLIPPER_FW_NAME, chipset, serial)

    if not os.path.exists(katapult_dev):
        if os.path.exists(klipper_dev):
            print(f"{serial} is running Klipper - requesting bootloader...")
            subprocess.run([sys.executable, FLASHTOOL, "-d", klipper_dev, "-r"])
            print(f"Waiting for {serial} to re-enumerate as a Katapult device...")
            if not wait_for_path(katapult_dev):
                print(f"ERROR: {serial} never reappeared as {katapult_dev}.", file=sys.stderr)
                return False
        else:
            print(f"ERROR: no device found for {serial} (looked for {katapult_dev} and "
                  f"{klipper_dev}). Is it plugged in?", file=sys.stderr)
            return False

    print(f"Flashing {serial} ({mcu_type}) via {katapult_dev}...")
    res = subprocess.run([sys.executable, FLASHTOOL, "-d", katapult_dev, "-f", fw_bin])
    if res.returncode != 0:
        print(f"ERROR: flashtool.py failed for {serial}", file=sys.stderr)
        return False
    print(f"Flashed {serial} successfully.")
    return True

# --- SERVICE CONTROL ---

def klipper_service(action):
    """action: 'stop' or 'start'. Requires passwordless sudo for systemctl on
    this one unit (or run the whole script under sudo)."""
    print(f"{action.capitalize()}ing {KLIPPER_SERVICE} service...")
    subprocess.run(["sudo", "systemctl", action, KLIPPER_SERVICE])

# --- ACTION FUNCTIONS (argparse entry points) ---

def add_mcu_type(args):
    data = load_data()

    if args.type in data and not args.force:
        print(f"MCU Type '{args.type}' already exists:\n{json.dumps(data[args.type], indent=2)}")
        resp = input("Overwrite? [y/N]: ").strip().lower()
        if resp not in ('y', 'yes'):
            print("Aborting add.")
            return

    data[args.type] = {
        "chipset": args.chipset,
        "katapult": {
            "installed": not args.no_katapult,
            "extra_args": args.katapult_args
        },
        "klipper": {
            "extra_args": args.klipper_args
        },
        "serials": []
    }
    save_data(data)
    print(f"Successfully added/updated MCU Type: {args.type}")

def add_serial(args):
    data = load_data()
    if args.type not in data:
        print(f"ERROR: MCU Type '{args.type}' does not exist. Please add it first.", file=sys.stderr)
        sys.exit(1)

    if args.serial not in data[args.type]["serials"]:
        data[args.type]["serials"].append(args.serial)
        save_data(data)
        print(f"Added serial {args.serial} to {args.type}")
    else:
        print(f"Serial {args.serial} already exists under {args.type}")

def remove_mcu_type(args):
    data = load_data()
    if args.type not in data:
        print(f"ERROR: MCU Type '{args.type}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if not args.force:
        num_serials = len(data[args.type].get("serials", []))
        resp = input(f"Remove type '{args.type}' and its {num_serials} tracked serial(s)? [y/N]: ").strip().lower()
        if resp not in ('y', 'yes'):
            print("Aborted.")
            return

    del data[args.type]
    save_data(data)
    print(f"Removed MCU Type: {args.type}")

def remove_serial(args):
    data = load_data()
    if args.type not in data:
        print(f"ERROR: MCU Type '{args.type}' does not exist.", file=sys.stderr)
        sys.exit(1)

    if args.serial not in data[args.type]["serials"]:
        print(f"Serial {args.serial} isn't tracked under {args.type} - nothing to do.")
        return

    data[args.type]["serials"].remove(args.serial)
    save_data(data)
    print(f"Removed serial {args.serial} from {args.type}")

def make_menuconfig_cmd(args):
    do_menuconfig(args.type, args.fw)

def build_fw_cmd(args):
    if do_build(args.type, args.fw) is None:
        sys.exit(1)

def find_type_for_serial(serial, data):
    """Returns the list of MCU type names that have this serial tracked.
    Normally 0 or 1; more than 1 would mean the same serial got added under
    two types by mistake."""
    return [t for t, cfg in data.items() if serial in cfg.get("serials", [])]

def flash_fw_cmd(args):
    data = load_data()

    if not args.serial and not args.type:
        print("ERROR: provide -s <serial>, -t <type>, or both.", file=sys.stderr)
        sys.exit(1)

    if not args.yes:
        resp = input(f"Flashing requires stopping the '{KLIPPER_SERVICE}' service (aborts any "
                      f"active print!). Continue? [y/N]: ").strip().lower()
        if resp not in ('y', 'yes'):
            print("Aborted.")
            return

    if args.type and not args.serial:
        if args.type not in data:
            print(f"ERROR: MCU Type '{args.type}' not tracked.", file=sys.stderr)
            sys.exit(1)
        serials = data[args.type]["serials"]
        if not serials:
            print(f"No serials tracked under '{args.type}'.", file=sys.stderr)
            sys.exit(1)
        chipset = data[args.type]["chipset"]
        failures = []
        klipper_service("stop")
        try:
            for serial in serials:
                if not flash_device(args.type, chipset, serial):
                    failures.append(serial)
        finally:
            klipper_service("start")
        if failures:
            print(f"Failures: {', '.join(failures)}", file=sys.stderr)
            sys.exit(1)
        return
    
    if args.type:
        if args.type not in data:
            print(f"ERROR: MCU Type '{args.type}' not tracked.", file=sys.stderr)
            sys.exit(1)
        if args.serial not in data[args.type]["serials"]:
            # If it's already tracked under a DIFFERENT type, that's a much
            # stronger signal of "wrong -t" than "this is just a new device" -
            # refuse outright rather than offering to add it here too.
            elsewhere = find_type_for_serial(args.serial, data)
            if elsewhere:
                print(f"ERROR: serial '{args.serial}' is already tracked under "
                      f"'{elsewhere[0]}', not '{args.type}'. Did you mean -t {elsewhere[0]}?", file=sys.stderr)
                sys.exit(1)
            resp = input(f"Serial '{args.serial}' isn't tracked under '{args.type}' yet. "
                          f"Add it now? [y/N]: ").strip().lower()
            if resp not in ('y', 'yes'):
                print("Aborted.")
                sys.exit(1)
            data[args.type]["serials"].append(args.serial)
            save_data(data)
            print(f"Added serial {args.serial} to {args.type}")
        mcu_type = args.type
    else:
        matches = find_type_for_serial(args.serial, data)
        if not matches:
            print(f"ERROR: serial '{args.serial}' isn't tracked under any MCU type.", file=sys.stderr)
            sys.exit(1)
        if len(matches) > 1:
            print(f"ERROR: serial '{args.serial}' is tracked under multiple types "
                  f"({', '.join(matches)}) - pass -t to disambiguate.", file=sys.stderr)
            sys.exit(1)
        mcu_type = matches[0]
        print(f"Resolved serial {args.serial} -> type '{mcu_type}'")

    chipset = data[mcu_type]["chipset"]
    klipper_service("stop")
    try:
        ok = flash_device(mcu_type, chipset, args.serial)
    finally:
        klipper_service("start")
    sys.exit(0 if ok else 1)

def update_all(args):
    data = load_data()
    if not data:
        print("No MCU types configured.", file=sys.stderr)
        sys.exit(1)

    if not args.yes:
        resp = input(f"This stops the '{KLIPPER_SERVICE}' service (aborts any active print!), "
                      f"rebuilds + reflashes every tracked MCU, then restarts it. Continue? [y/N]: ").strip().lower()
        if resp not in ('y', 'yes'):
            print("Aborted.")
            return

    klipper_service("stop")
    failures = []
    try:
        for mcu_type, cfg in data.items():
            print(f"\n=== {mcu_type} ===")
            fw_bin = do_build(mcu_type, "klipper", interactive=False)
            if fw_bin is None:
                failures.append((mcu_type, None))
                continue
            chipset = cfg.get("chipset", "")
            for serial in cfg.get("serials", []):
                if not flash_device(mcu_type, chipset, serial, fw_bin=fw_bin):
                    failures.append((mcu_type, serial))
    finally:
        klipper_service("start")

    if failures:
        print("\nCompleted with failures:")
        for mcu_type, serial in failures:
            print(f"  - {mcu_type}" + (f" / {serial}" if serial else " (build failed)"))
        sys.exit(1)
    print("\nAll MCU types built and flashed successfully.")

def flash_dfu_stm32(fw_bin):
    """Flashes a .bin to an STM32 device currently sitting in DFU mode, via
    dfu-util. Used for the very first katapult flash on a brand new board
    (before it has any bootloader to talk flashtool.py's protocol to)."""
    print("Looking for STM32 device in DFU mode via dfu-util...")
    check = subprocess.run(["dfu-util", "-l"], capture_output=True, text=True)
    if "Found DFU" not in check.stdout:
        print("ERROR: No DFU device detected. Make sure it's jumpered/button-pressed into DFU mode.", file=sys.stderr)
        print(check.stdout)
        return False

    print("DFU device found. Flashing via dfu-util...")
    res = subprocess.run([
        "dfu-util", "-a", "0", "-d", "0483:df11",
        "-D", fw_bin,
        "-s", "0x08000000:force:mass-erase:leave",
    ])
    if res.returncode != 0:
        print("ERROR: dfu-util flashing failed.", file=sys.stderr)
        return False
    print("Flash command sent. Device should reboot into Katapult momentarily.")
    return True

def find_unassigned_devices(fw_name=None, chipset=None):
    """Scans /dev/serial/by-id/ for usb-<fw>_<chipset>_<serial> entries whose
    serial isn't already tracked under any MCU type in mcus.json."""
    serial_dir = "/dev/serial/by-id"
    if not os.path.isdir(serial_dir):
        return []

    data = load_data()
    known_serials = set()
    for cfg in data.values():
        known_serials.update(cfg.get("serials", []))

    results = []
    for name in os.listdir(serial_dir):
        if not name.startswith("usb-"):
            continue
        parts = name[len("usb-"):].split("_", 2)
        if len(parts) < 2:
            continue
        dev_fw, dev_chipset, dev_serial = (parts[0], parts[1], parts[2]) if len(parts) == 3 else (parts[0], "", parts[1])
        if fw_name and dev_fw.lower() != fw_name.lower():
            continue
        if chipset and dev_chipset != chipset:
            continue
        if dev_serial in known_serials:
            continue
        results.append({"fw": dev_fw, "chipset": dev_chipset, "serial": dev_serial, "path": os.path.join(serial_dir, name)})
    return results

def add_mcu(args):
    data = load_data()
    if args.type not in data:
        print(f"ERROR: MCU Type '{args.type}' not tracked. Use 'add-type' first.", file=sys.stderr)
        sys.exit(1)

    chipset = data[args.type]["chipset"]

    # This builds katapult, launching menuconfig automatically since a brand
    # new type has no saved .config yet.
    fw_bin = do_build(args.type, "katapult")
    if fw_bin is None:
        print("ERROR: katapult build failed, aborting add-mcu.", file=sys.stderr)
        sys.exit(1)

    if chipset.startswith("stm32"):
        ok = flash_dfu_stm32(fw_bin)
    elif chipset == "rp2040":
        print("ERROR: RP2040 BOOTSEL flashing isn't wired up here yet - mount it manually "
              "and copy the .uf2 over, then use 'add-serial' once it enumerates as Katapult.",
              file=sys.stderr)
        sys.exit(1)
    else:
        print(f"ERROR: don't know how to initial-flash chipset '{chipset}'. Flash katapult "
              f"manually, then use 'add-serial' once it enumerates.", file=sys.stderr)
        sys.exit(1)

    if not ok:
        sys.exit(1)

    print("Waiting a few seconds for the device to enumerate as Katapult...")
    time.sleep(3)
    candidates = find_unassigned_devices(fw_name="katapult", chipset=chipset)
    if not candidates:
        print(f"No new, unassigned Katapult device found for chipset '{chipset}'. "
              f"Check `ls /dev/serial/by-id/` and use 'add-serial' manually.")
        return

    for dev in candidates:
        resp = input(f"Found unassigned Katapult device: {dev['serial']} ({dev['path']}). "
                      f"Add it to '{args.type}'? [y/N]: ").strip().lower()
        if resp in ('y', 'yes'):
            data[args.type]["serials"].append(dev["serial"])
            save_data(data)
            print(f"Added serial {dev['serial']} to {args.type}")

# --- INTERACTIVE MENU ---

def prompt_choice(title, options, allow_cancel=True):
    """Prints a numbered list and loops on input() until a valid selection is
    made. Returns the 0-based index into `options`, or None if the user
    cancels (enters 0, when allow_cancel is True)."""
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

def prompt_yn(prompt, default=False):
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"{prompt} {suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in ('y', 'yes')

def prompt_nonempty(prompt):
    while True:
        val = input(f"{prompt}: ").strip()
        if val:
            return val
        print("This can't be blank.")

def pick_mcu_type(data, allow_new=True):
    """Numbered picker over existing MCU types. If allow_new, appends an
    "Add a new MCU type" entry that runs the add-type flow inline and returns
    the freshly created name - so a flow needing a type never dead-ends just
    because none exist yet. Returns None on cancel."""
    types = sorted(data.keys())
    if not types:
        if not allow_new:
            print("No MCU types configured yet.")
            return None
        print("No MCU types configured yet - let's add one.")
        return menu_add_mcu_type()

    options = [f"{t}  (chipset={data[t].get('chipset', '?')}, "
               f"{len(data[t].get('serials', []))} serial(s))" for t in types]
    if allow_new:
        options.append("+ Add a new MCU type")
    idx = prompt_choice("Select MCU type", options)
    if idx is None:
        return None
    if allow_new and idx == len(types):
        return menu_add_mcu_type()
    return types[idx]

def pick_fw_target():
    idx = prompt_choice("Select firmware target", ["klipper", "katapult"])
    if idx is None:
        return None
    return ["klipper", "katapult"][idx]

def pick_serial_for_type(mcu_type, data):
    """Lists tracked serials plus any untracked devices currently detected on
    the bus for this type's chipset, plus a manual-entry option. Used by the
    Flash flow, where either a tracked or not-yet-tracked device is valid."""
    tracked = data[mcu_type].get("serials", [])
    chipset = data[mcu_type].get("chipset", "")
    unassigned = find_unassigned_devices(chipset=chipset)
    options = [f"{s} (tracked)" for s in tracked]
    options += [f"{d['serial']} (untracked, detected on bus)" for d in unassigned]
    options.append("Enter serial manually")
    idx = prompt_choice(f"Select a device under '{mcu_type}'", options)
    if idx is None:
        return None
    if idx < len(tracked):
        return tracked[idx]
    if idx < len(tracked) + len(unassigned):
        return unassigned[idx - len(tracked)]["serial"]
    return prompt_nonempty("Serial string")

def pick_tracked_serial(mcu_type, data):
    """Lists only already-tracked serials - used by Remove-serial, where the
    target must already be tracked."""
    tracked = data[mcu_type].get("serials", [])
    if not tracked:
        print(f"No serials tracked under '{mcu_type}'.")
        return None
    idx = prompt_choice(f"Select a serial to remove from '{mcu_type}'", tracked)
    if idx is None:
        return None
    return tracked[idx]

def list_mcu_status():
    """Read-only view: for each tracked type/serial, checks whether it's
    currently enumerated as Klipper or Katapult(bootloader) on the bus."""
    data = load_data()
    if not data:
        print("No MCU types configured yet.")
        return
    for mcu_type in sorted(data):
        cfg = data[mcu_type]
        chipset = cfg.get("chipset", "?")
        serials = cfg.get("serials", [])
        print(f"\n{mcu_type}  (chipset={chipset})")
        if not serials:
            print("  (no tracked serials)")
            continue
        for serial in serials:
            if os.path.exists(device_path(KLIPPER_FW_NAME, chipset, serial)):
                status = "online (klipper)"
            elif os.path.exists(device_path(KATAPULT_FW_NAME, chipset, serial)):
                status = "online (katapult/bootloader)"
            else:
                status = "offline"
            print(f"  - {serial}: {status}")

def call_action(func, ns):
    """Invokes an existing argparse action function from the menu with a
    manually built Namespace. Catches SystemExit so a failed sub-action
    (build failure, flash failure, aborted prompt, etc.) returns control to
    the menu loop instead of ending the whole interactive session - the
    function's own print statements already explain what happened.
    KeyboardInterrupt is deliberately not caught here; it propagates up to
    run_menu()'s handler and ends the session, matching normal ^C convention."""
    try:
        func(ns)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        if code not in (0, None):
            print("(action did not complete successfully - see messages above)")

def menu_add_mcu_type():
    type_name = prompt_nonempty("MCU type name (e.g. bttebb36)")
    chipset = prompt_nonempty("Chipset (e.g. stm32g0b1xx)")
    klipper_args = input("Extra klipper make args (blank for none): ").strip()
    katapult_args = input("Extra katapult make args (blank for none): ").strip()
    no_katapult = prompt_yn("Skip katapult (no bootloader)?", default=False)
    ns = argparse.Namespace(type=type_name, chipset=chipset, klipper_args=klipper_args,
                             katapult_args=katapult_args, no_katapult=no_katapult, force=False)
    call_action(add_mcu_type, ns)
    return type_name

def menu_remove_mcu_type():
    data = load_data()
    mcu_type = pick_mcu_type(data, allow_new=False)
    if mcu_type is None:
        return
    call_action(remove_mcu_type, argparse.Namespace(type=mcu_type, force=False))

def menu_add_serial():
    data = load_data()
    mcu_type = pick_mcu_type(data, allow_new=True)
    if mcu_type is None:
        return
    data = load_data()  # refresh - pick_mcu_type may have just created this type
    chipset = data.get(mcu_type, {}).get("chipset", "")
    unassigned = find_unassigned_devices(chipset=chipset)
    options = [f"{d['serial']} (detected on bus)" for d in unassigned]
    options.append("Enter serial manually")
    idx = prompt_choice(f"Select a serial to add to '{mcu_type}'", options)
    if idx is None:
        return
    serial = unassigned[idx]["serial"] if idx < len(unassigned) else prompt_nonempty("Serial string")
    call_action(add_serial, argparse.Namespace(type=mcu_type, serial=serial))

def menu_remove_serial():
    data = load_data()
    mcu_type = pick_mcu_type(data, allow_new=False)
    if mcu_type is None:
        return
    serial = pick_tracked_serial(mcu_type, data)
    if serial is None:
        return
    call_action(remove_serial, argparse.Namespace(type=mcu_type, serial=serial))

def menu_add_mcu():
    data = load_data()
    mcu_type = pick_mcu_type(data, allow_new=True)
    if mcu_type is None:
        return
    call_action(add_mcu, argparse.Namespace(type=mcu_type))

def menu_menuconfig():
    data = load_data()
    mcu_type = pick_mcu_type(data, allow_new=True)
    if mcu_type is None:
        return
    fw = pick_fw_target()
    if fw is None:
        return
    call_action(make_menuconfig_cmd, argparse.Namespace(type=mcu_type, fw=fw))

def menu_build():
    data = load_data()
    mcu_type = pick_mcu_type(data, allow_new=True)
    if mcu_type is None:
        return
    fw = pick_fw_target()
    if fw is None:
        return
    call_action(build_fw_cmd, argparse.Namespace(type=mcu_type, fw=fw))

def menu_flash():
    data = load_data()
    mcu_type = pick_mcu_type(data, allow_new=False)
    if mcu_type is None:
        return
    serials = data.get(mcu_type, {}).get("serials", [])
    scope_options = ["Flash every tracked serial under this type"]
    if serials:
        scope_options.append("Flash one specific device")
    idx = prompt_choice(f"Flash scope for '{mcu_type}'", scope_options)
    if idx is None:
        return
    if idx == 0:
        ns = argparse.Namespace(type=mcu_type, serial=None, yes=False)
    else:
        serial = pick_serial_for_type(mcu_type, data)
        if serial is None:
            return
        ns = argparse.Namespace(type=mcu_type, serial=serial, yes=False)
    call_action(flash_fw_cmd, ns)

def menu_update_all():
    call_action(update_all, argparse.Namespace(yes=False))

def run_menu():
    menu_items = [
        ("List MCU types / status", list_mcu_status),
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
    try:
        while True:
            print("\n=== Klipper/Katapult Firmware Manager ===")
            for i, (label, _) in enumerate(menu_items, 1):
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
            if not (1 <= choice <= len(menu_items)):
                print("Out of range, try again.")
                continue
            menu_items[choice - 1][1]()
            input("\nPress Enter to continue...")
    except (KeyboardInterrupt, EOFError):
        print("\nExiting.")

# --- CLI PARSER SETUP ---

def main():
    parser = argparse.ArgumentParser(description="Klipper/Katapult Firmware Management Utility")
    subparsers = parser.add_subparsers(title="Commands", dest="command", required=True)

    parser_type = subparsers.add_parser("add-type", help="Add a new MCU type configuration")
    parser_type.add_argument("-t", "--type", required=True, help="Unique MCU Type Name (e.g., bttebb36)")
    parser_type.add_argument("-c", "--chipset", required=True, help="Chipset (e.g., stm32g0b1xx)")
    parser_type.add_argument("--klipper-args", default="", help="Extra make arguments for Klipper")
    parser_type.add_argument("--katapult-args", default="", help="Extra make arguments for Katapult")
    parser_type.add_argument("--no-katapult", action="store_true", help="Set Katapult installed to false")
    parser_type.add_argument("--force", action="store_true", help="Overwrite without prompting")
    parser_type.set_defaults(func=add_mcu_type)

    parser_serial = subparsers.add_parser("add-serial", help="Add a serial number to an existing MCU type")
    parser_serial.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_serial.add_argument("-s", "--serial", required=True, help="The device serial string")
    parser_serial.set_defaults(func=add_serial)

    parser_rm_type = subparsers.add_parser("remove-type", help="Remove an MCU type configuration and its tracked serials")
    parser_rm_type.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_rm_type.add_argument("--force", action="store_true", help="Skip the confirmation prompt")
    parser_rm_type.set_defaults(func=remove_mcu_type)

    parser_rm_serial = subparsers.add_parser("remove-serial", help="Remove a tracked serial from an MCU type")
    parser_rm_serial.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_rm_serial.add_argument("-s", "--serial", required=True, help="The device serial string")
    parser_rm_serial.set_defaults(func=remove_serial)

    parser_menu = subparsers.add_parser("menuconfig", help="Launch make menuconfig for a specific target")
    parser_menu.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_menu.add_argument("-f", "--fw", required=True, choices=["klipper", "katapult"], help="Firmware target")
    parser_menu.set_defaults(func=make_menuconfig_cmd)

    parser_build = subparsers.add_parser("build", help="Compile the firmware for a specific target")
    parser_build.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_build.add_argument("-f", "--fw", required=True, choices=["klipper", "katapult"], help="Firmware target")
    parser_build.set_defaults(func=build_fw_cmd)

    parser_flash = subparsers.add_parser("flash", help="Flash a single tracked device with its built klipper.bin")
    parser_flash.add_argument("-t", "--type", default=None, help="MCU Type Name (optional - inferred from the serial if omitted)")
    parser_flash.add_argument("-s", "--serial", default=None, help="Device serial (must already be tracked)")
    parser_flash.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    parser_flash.set_defaults(func=flash_fw_cmd)

    parser_update = subparsers.add_parser("update-all", help="Build + flash klipper for every tracked MCU type/device, stopping/restarting klipper around it")
    parser_update.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    parser_update.set_defaults(func=update_all)

    parser_addmcu = subparsers.add_parser("add-mcu", help="Interactive routine to setup, build, and flash a new MCU")
    parser_addmcu.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_addmcu.set_defaults(func=add_mcu)

    if len(sys.argv) == 1:
        run_menu()
        return

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
