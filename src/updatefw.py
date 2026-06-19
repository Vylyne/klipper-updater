#!/usr/bin/env python3
import os
import json
import argparse
import subprocess
import sys
import time
import contextlib

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
KLIPPER_FW_NAME = "klipper"
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

    env = os.environ.copy()
    env["KCONFIG_CONFIG"] = config_file
    # -e: let the KCONFIG_CONFIG we just set win over Klipper's own
    # "export KCONFIG_CONFIG := $(CURDIR)/.config" line in its top-level
    # Makefile. Without -e, that line silently overrides whatever we pass in.
    subprocess.run(["make", "-e", "menuconfig"], cwd=fw_dir, env=env, stdin=sys.stdin, stdout=sys.stdout)

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
    env = os.environ.copy()
    env["KCONFIG_CONFIG"] = config_file

    with apply_makefile_patches(mcu_type, fw, fw_dir):
        subprocess.run(["make", "-e", "clean"], cwd=fw_dir, env=env)
        make_cmd = ["make", "-e"] + extra_args
        print(f"{make_cmd}")
        res = subprocess.run(make_cmd, cwd=fw_dir, env=env)

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

def make_menuconfig_cmd(args):
    do_menuconfig(args.type, args.fw)

def build_fw_cmd(args):
    if do_build(args.type, args.fw) is None:
        sys.exit(1)

def flash_fw_cmd(args):
    data = load_data()
    if args.type not in data:
        print(f"ERROR: MCU Type '{args.type}' not tracked.", file=sys.stderr)
        sys.exit(1)
    if args.serial not in data[args.type]["serials"]:
        print(f"ERROR: serial '{args.serial}' is not tracked under '{args.type}'.", file=sys.stderr)
        sys.exit(1)
    chipset = data[args.type]["chipset"]
    ok = flash_device(args.type, chipset, args.serial)
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

def add_mcu(args):
    data = load_data()
    if args.type not in data:
        print(f"ERROR: MCU Type '{args.type}' not tracked. Use 'add-type' first.", file=sys.stderr)
        sys.exit(1)

    do_menuconfig(args.type, "katapult")
    do_build(args.type, "katapult")

    print(">>> NOTE: Execute flashing logic here (flash_NewSTM32 / flash_NewRP2040) <<<")
    print("Scanning for unassigned Katapult devices...")
    # dev = get_unknown_serials()
    # if dev: prompt user to add via add_serial()

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

    parser_menu = subparsers.add_parser("menuconfig", help="Launch make menuconfig for a specific target")
    parser_menu.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_menu.add_argument("-f", "--fw", required=True, choices=["klipper", "katapult"], help="Firmware target")
    parser_menu.set_defaults(func=make_menuconfig_cmd)

    parser_build = subparsers.add_parser("build", help="Compile the firmware for a specific target")
    parser_build.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_build.add_argument("-f", "--fw", required=True, choices=["klipper", "katapult"], help="Firmware target")
    parser_build.set_defaults(func=build_fw_cmd)

    parser_flash = subparsers.add_parser("flash", help="Flash a single tracked device with its built klipper.bin")
    parser_flash.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_flash.add_argument("-s", "--serial", required=True, help="Device serial (must already be tracked under this type)")
    parser_flash.set_defaults(func=flash_fw_cmd)

    parser_update = subparsers.add_parser("update-all", help="Build + flash klipper for every tracked MCU type/device, stopping/restarting klipper around it")
    parser_update.add_argument("-y", "--yes", action="store_true", help="Skip the confirmation prompt")
    parser_update.set_defaults(func=update_all)

    parser_addmcu = subparsers.add_parser("add-mcu", help="Interactive routine to setup, build, and flash a new MCU")
    parser_addmcu.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_addmcu.set_defaults(func=add_mcu)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()