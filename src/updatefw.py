#!/usr/bin/env python3
import os
import json
import argparse
import subprocess
import sys
import shutil

# --- CONFIGURATION ---
SETTINGS_PATH = os.path.expanduser("~/mcus")
MCUS_JSON = os.path.join(SETTINGS_PATH, "mcus.json")

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

def get_extra_args(mcu_type, fw):
    data = load_data()
    if mcu_type in data and fw in data[mcu_type]:
        extra_args = data[mcu_type][fw].get("extra_args", "")
        print(F"Extra args: '{extra_args}'")
        return extra_args
    return ""

# --- ACTION FUNCTIONS ---

def add_mcu_type(args):
    data = load_data()
    
    if args.type in data and not args.force:
        print(f"MCU Type '{args.type}' already exists:\n{json.dumps(data[args.type], indent=2)}")
        resp = input("Overwrite? [y/N]: ").strip().lower()
        if resp not in ('y', 'yes'):
            print("Aborting add.")
            return

    # Create or overwrite the object
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
    
    # Add serial if not exists
    if args.serial not in data[args.type]["serials"]:
        data[args.type]["serials"].append(args.serial)
        save_data(data)
        print(f"Added serial {args.serial} to {args.type}")
    else:
        print(f"Serial {args.serial} already exists under {args.type}")

def make_menuconfig(args):
    config_dir = os.path.join(SETTINGS_PATH, args.type)
    os.makedirs(config_dir, exist_ok=True)
    config_file = os.path.join(config_dir, f"{args.fw}.config")
    print(F"config_file: {config_file}")
    
    fw_dir = os.path.expanduser(f"~/{args.fw}")
    if not os.path.exists(fw_dir):
        print(f"ERROR: Source directory {fw_dir} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Making config for {args.type} with {args.fw}")
    input("Press Enter to continue to menuconfig...")
    
    # Run menuconfig natively (pass stdin/stdout so curses UI works)
    subprocess.run(["make", "menuconfig", "KCONFIG_CONFIG=" + config_file], cwd=fw_dir, stdin=sys.stdin, stdout=sys.stdout)

def build_fw(args):
    config_file = os.path.join(SETTINGS_PATH, args.type, f"{args.fw}.config")
    fw_out = os.path.join(SETTINGS_PATH, args.type, f"{args.fw}.bin")
    fw_dir = os.path.expanduser(f"~/{args.fw}")
    makefile = fw_dir + "/Makefile"
    temp_makefile = False

    if not os.path.exists(config_file):
        print(f"Configuration file not found for {args.type} ({args.fw}). Launching menuconfig...")
        make_menuconfig(args)

    extra_args = get_extra_args(args.type, args.fw)
    if extra_args:
        shutil.copyfile(makefile, makefile + ".tmp")
        makefile = makefile + ".tmp"
        with open(makefile, 'a') as file:
            file.write(extra_args)
        temp_makefile = True
    
    print(f"Building {args.fw} for {args.type}...")

    # Make clean
    subprocess.run(["make", "clean", "KCONFIG_CONFIG=" + config_file, "--file", makefile], cwd=fw_dir)
    
    # Make with extra args
    res = subprocess.run(["make", "KCONFIG_CONFIG=" + config_file, "--file", makefile], cwd=fw_dir)

    # if temp_makefile:
    #     os.remove(makefile)

    if res.returncode == 0:
        compiled_bin = os.path.join(fw_dir, "out", f"{args.fw}.bin")
        if os.path.exists(compiled_bin):
            os.makedirs(os.path.dirname(fw_out), exist_ok=True)
            # Equivalent to cp -f
            with open(compiled_bin, 'rb') as src, open(fw_out, 'wb') as dst:
                dst.write(src.read())
            print(f"Firmware built and copied to {fw_out}")
        else:
            print("ERROR: Compilation succeeded but output binary was not found.")
    else:
        print("ERROR: Firmware build failed.", file=sys.stderr)
        sys.exit(1)

def add_mcu(args):
    data = load_data()
    if args.type not in data:
        print(f"ERROR: MCU Type '{args.type}' not tracked. Use 'add-type' first.", file=sys.stderr)
        sys.exit(1)

    # Convert args structure for sub-functions
    args.fw = "katapult"
    
    make_menuconfig(args)
    build_fw(args)
    
    # Stubbed flashing logic
    print(">>> NOTE: Execute flashing logic here (flash_NewSTM32 / flash_NewRP2040) <<<")
    
    # Stubbed serial detection logic
    print("Scanning for unassigned Katapult devices...")
    # dev = get_unknown_serials() 
    # if dev: prompt user to add via add_serial()
    
# --- CLI PARSER SETUP ---

def main():
    parser = argparse.ArgumentParser(description="Klipper/Katapult Firmware Management Utility")
    subparsers = parser.add_subparsers(title="Commands", dest="command", required=True)

    # Command: add-type
    parser_type = subparsers.add_parser("add-type", help="Add a new MCU type configuration")
    parser_type.add_argument("-t", "--type", required=True, help="Unique MCU Type Name (e.g., bttebb36)")
    parser_type.add_argument("-c", "--chipset", required=True, help="Chipset (e.g., stm32g0b1xx)")
    parser_type.add_argument("--klipper-args", default="", help="Extra make arguments for Klipper")
    parser_type.add_argument("--katapult-args", default="", help="Extra make arguments for Katapult")
    parser_type.add_argument("--no-katapult", action="store_true", help="Set Katapult installed to false")
    parser_type.add_argument("--force", action="store_true", help="Overwrite without prompting")
    parser_type.set_defaults(func=add_mcu_type)

    # Command: add-serial
    parser_serial = subparsers.add_parser("add-serial", help="Add a serial number to an existing MCU type")
    parser_serial.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_serial.add_argument("-s", "--serial", required=True, help="The device serial string")
    parser_serial.set_defaults(func=add_serial)

    # Command: menuconfig
    parser_menu = subparsers.add_parser("menuconfig", help="Launch make menuconfig for a specific target")
    parser_menu.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_menu.add_argument("-f", "--fw", required=True, choices=["klipper", "katapult"], help="Firmware target")
    parser_menu.set_defaults(func=make_menuconfig)

    # Command: build
    parser_build = subparsers.add_parser("build", help="Compile the firmware for a specific target")
    parser_build.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_build.add_argument("-f", "--fw", required=True, choices=["klipper", "katapult"], help="Firmware target")
    parser_build.set_defaults(func=build_fw)

    # Command: add-mcu
    parser_addmcu = subparsers.add_parser("add-mcu", help="Interactive routine to setup, build, and flash a new MCU")
    parser_addmcu.add_argument("-t", "--type", required=True, help="MCU Type Name")
    parser_addmcu.set_defaults(func=add_mcu)

    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()