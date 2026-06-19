#!/bin/bash

SETTINGS_PATH="$HOME/mcus"
MCUS_JSON="$HOME/mcus/mcus.json"


# Ensure the directory and MCUS_JSON exist before doing anything
mkdir -p "$(dirname "$MCUS_JSON")"
if [ ! -f "$MCUS_JSON" ] || [ ! -s "$MCUS_JSON" ]; then
    echo '[]' >> $MCUS_JSON
fi

usage() {
    echo "Usage:
            -a <MCU Type Name> | --add=<MCU Type Name>   Adds a new device"
    exit 2
}

# Parse command-line options
# Added -a to the short options so '-a name' actually works
OPTS=$(getopt -o ha: --long help,add: -n 'updatefw.sh' -- "$@")

if [ $? -ne 0 ]; then
    echo "Failed to parse options" >&2
    exit 1
fi

eval set -- "$OPTS"

addMCU=""

while true; do
    case "$1" in
        -h | --help)
            usage
            shift ;;
        -a | --add)
            addMCU="$2"
            shift 2 ;;
        --)
            shift
            break ;;
        *)
            echo "Invalid option $1"
            exit 1 ;;
    esac
done

# JQ Functions

McuTypeExists() {
	local params=$(getopt -o m: --long type: -- "$@")
	local type=""
	eval set -- "$params"

	while true; do
        case "$1" in
            -t | --type)
                type="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: McuTypeExists $@"
                exit 1
                ;;
        esac
    done
	if jq -e --arg type "$type" 'has($type)' "$MCUS_JSON" > /dev/null; then
		return 0
	else
		return 1
	fi
}

ShowMcuType() {
	local params=$(getopt -o m: --long type: -- "$@")
	local type=""
	eval set -- "$params"

	while true; do
        case "$1" in
            -t | --type)
                type="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: ShowMcuType $@"
                exit 1
                ;;
        esac
    done

	jq --arg type "$type" 'to_entries[] | select(.key == $type) | [.] | from_entries' "$MCUS_JSON"
}

GetMcuTypes() {
	jq 'keys[]' "$MCUS_JSON"
}

GetMcuType() {
	local params=$(getopt -o d:s: --long dev:,serial: -- "$@")
	local dev=""
	local serial=""
    eval set -- "$params"

	while true; do
        case "$1" in
            -d | --dev)
                dev="$2"
                shift 2 ;;
			-s | --serial)
				serial="$2"
				shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: GetMcuType $@"
                exit 1
                ;;
        esac
    done
	if [ $dev] && ! [ $serial ]; then
		serial="${dev##*_}"
	fi

	local type=$(jq --arg serial $serial 'to_entries | map(select(.value.serials[]==$serial))[].key' "$MCUS_JSON" | tr -d '"')
	if [ $type ]; then
		echo $type
		return 0
	else
		return 1
	fi
}

GetMCUPath() {
	local params=$(getopt -o s: --long serial: -- "$@")
	local serial=""
    eval set -- "$params"

	while true; do
        case "$1" in
			-s | --serial)
				serial="$2"
				shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: GetMCUPath $@"
                exit 1
                ;;
        esac
    done

	if ! [ $serial ]; then
		echo "Error: no Serial provided."
		return 1
	fi
	echo $(ls /dev/serial/by-id/usb-*$serial)
}

GetSerials() {
	local params=$(getopt -o t: --long type: -- "$@")
	local type=""
    eval set -- "$params"

	while true; do
        case "$1" in
            -t | --type)
                type="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: GetSerials $@"
                exit 1
                ;;
        esac
    done

	if [ $type ]; then
		jq --arg type "$type" ' .[$type].serials[]' "$MCUS_JSON"
	else
		jq '.[].serials[]' "$MCUS_JSON"
	fi
}

GetDevicePaths() {
	local params=$(getopt -o f:t: --long fw:,type: -- "$@")
    local fw=""
	local type=""
    eval set -- "$params"

	while true; do
        case "$1" in
            -f | --fw)
                fw="$2"
                shift 2 ;;
            -t | --type)
                type="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: GetDevicePaths $@"
                return 1
                ;;
        esac
    done

    local searchFilter="/dev/serial/by-id/usb-"
    [ "$fw" ] && searchFilter+="${fw}_*" || searchFilter+="*"
	local serials=""
	if [ $type ]; then
		serials=$(jq --arg type $type '[.[$type].serials[]] | join("|")' "$MCUS_JSON" | tr -d '"')
	else
		serials=$(jq '[.[].serials[]] | join("|")' "$MCUS_JSON" | tr -d '"')
	fi
	ls $searchFilter | grep -E $serials
}

GetExtraArgs() {
	local params=$(getopt -o f:t: --long fw:,type: -- "$@")
    local fw=""
	local type=""
    eval set -- "$params"

	while true; do
        case "$1" in
            -f | --fw)
                fw="$2"
                shift 2 ;;
            -t | --type)
                type="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: GetExtraArgs $@"
                exit 1
                ;;
        esac
    done

	if [ $fw ] && [ $type ]; then
		jq --arg fw "$fw" --arg type "$type" '.[$type][$fw]["extra_src"]' "$MCUS_JSON"
	else
		return 1
	fi
}

AddMcuType() {
	local params=$(getopt -o t:c: --long type:,chipset:,klipperExtraArgs:,katapultExtraArgs:,noKatapult,force -- "$@")
    local Type=""
    local chipset=""
	local klipperExtraArgs=""
	local katapultExtraArgs=""
	local katapault="true"
	local force=""

    eval set -- "$params"

    while true; do
        case "$1" in
            -t | --type)
                type="$2"
                shift 2 ;;
            -c | --chipset)
                chipset="$2"
                shift 2 ;;
			--klipperExtraArgs)
				klipperExtraArgs="$2"
				shift 2 ;;
			--katapultExtraArgs)
				katapultExtraArgs="$2"
				shift 2 ;;
			noKatapult)
				kataplut="false"
				shift ;;
			force)
				force="true"
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: AddMcuType $@"
                exit 1
                ;;
        esac
    done

	# checking for force flag or if it's not found,
	if [ ! $force ] || jqMcuTypeExists --type $type then

		jqShowMcuType --type "$type"
		# checking if we shouyld Overwrite it.
		read -p "MCU Type '$type' found in $MCUS_JSON. Overwrite? [y/N]: " response
		case "$response" in
			[yY][eE][sS]|[yY])
				;;
			*)
				echo "Aborting add, "
				return 1
				;;
		esac
    fi

	jq --arg type "$type" --arg chipset "$chipset" \
		--arg katapult "$katapult" --arg katapultExtraArgs "$katapultExtraArgs" \
		--arg klipperExtraArgs "$klipperExtraArgs" \
		'setpath([$type]; {
				"chipset": $chipset,
				"katapult": {"installed": $katapult, "extra_src": $katapultExtraArgs},
				"klipper": { "extra_src": $klipperExtraArgs},
				"serials":[]
			}
		)' "$MCUS_JSON" > "${MCUS_JSON}.tmp" \
		&& mv "${MCUS_JSON}.tmp" "$MCUS_JSON"
}

AddSerial() {
    local params=$(getopt -o t:s: --long type:,serial: -- "$@")
    local type=""
    local serial=""
    eval set -- "$params"

    while true; do
        case "$1" in
            -t | --type)
                type="$2"
                shift 2 ;;
            -s | --serial)
                serial="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: AddSerial $@"
                exit 1
                ;;
        esac
    done
	if [ $type ] && [ $serial ]; then
		jq --arg type "$type" --arg serial "$serial" '.[$type].serials |= (.+ [$serial] | unique)' "$MCUS_JSON" > "${MCUS_JSON}.tmp" \
			&& mv "${MCUS_JSON}.tmp" "$MCUS_JSON"
	else
		echo "ERROR: AddSerial(): must provide both tag and serial. $params"
		return 1
	fi
}

getUnknownSerials() {
    local params=$(getopt -o f:c: --long fw:,chipset: -- "$@")
    local fw=""
    local chipset=""
    eval set -- "$params"

    while true; do
        case "$1" in
            -f | --fw)
                fw="$2"
                shift 2 ;;
            -c | --chipset)
                chipset="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: getUnknownSerials $@"
                exit 1
                ;;
        esac
    done

    # Reset/Initialize array globally or in shared scope
    UnknownSerials=()
    
    # Ensure directory exists before querying it
    [ -d "/dev/serial/by-id" ] || return

    # Build up the search pattern robustly
    local searchFilter="/dev/serial/by-id/usb-"
    [ "$fw" ] && searchFilter+="${fw}_" || searchFilter+="*_"
    [ "$chipset" ] && searchFilter+="${chipset}_" || searchFilter+="*_"
    searchFilter+="*"

    for dev in $searchFilter; do
        # Protect against empty glob results
        [ -e "$dev" ] || continue

        local fullName="$(basename "$dev")"
        
        # Pull the firmware prefix safely (e.g. "katapult" or "klipper")
        local rawPrefix="${fullName%%_*}"
        local devFW="${rawPrefix#usb-}" # strips "usb-" off the front

        # Strip up to the first underscore following the firmare name prefix
        # "usb-katapult_stm32f072xb_12345-if00" -> "stm32f072xb_12345-if00"
        local remainder="${fullName#usb-${devFW}_}"

        # Slices everything before the first remaining underscore -> "stm32f072xb"
        local devChipset="${remainder%%_*}"

        # Slices everything after the last remaining underscore -> "12345-if00"
        local devSerial="${fullName##*_}"

        # Check against your JSON schema
        if ! jq -e --arg serial "$devSerial" --arg chipset "$devChipset" \
            '.[] | select(.serials[] == $serial and .chipset == $chipset)' "$MCUS_JSON" >/dev/null; then
            # Use parentheses to append explicitly as a single array item
			echo "${devFW}_${devChipset}_${devSerial}"
            UnknownSerials+=("${devFW}_${devChipset}_${devSerial}")
        fi
    done
}

addMCU() {
	local params=$(getopt -o t: --long type: -- "$@")
    local type=""
    eval set -- "$params"

    while true; do
        case "$1" in
            -t | --type)
                type="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: addMCU $@"
                exit 1
                ;;
        esac
    done
    local katapult_config="$SETTINGS_PATH/$type/katapult.config"
    local fwOut="$SETTINGS_PATH/$type/katapult.bin"

    # Check if the MCU 'type' exists in the flat JSON array
    if jqMcuTypeExists --type $type then
        echo "MCU Type '$type' found in $MCUS_JSON."
    else

    fi

    # Let's build katapult!
    make_menuconfig  --type "$type" --fw "katapult"
    BuildFW --type "$type" --fw "katapult"

    # Ensure config file exists before reading it
    if [ ! -f "$katapult_config" ]; then
        echo "Error: $katapult_config was not generated."
        exit 1
    fi

	# Source the config file to automatically instantiate $CONFIG_BOARD_DIRECTORY and $CONFIG_MCU
    if [ -f "$katapult_config" ]; then
        # Replacing '#' comments with blanks and sourcing natively
        # (This avoids any weird shell behavior with Klipper's comments)
        eval "$(grep -v '^#' "$katapult_config")"
    else
        echo "Error: $katapult_config was not found."
        exit 1
    fi

    case "$CONFIG_BOARD_DIRECTORY" in
        "stm32")   flash_NewSTM32 "$fwOut" ;;
        "rp2040")  flash_NewRP2040 ;;
        "lpc176x") echo "Flashing new LPC176X not implemented yet." ;;
        *)         echo "MCU Board Directory not recognized." ;;
    esac

    getUnknownSerials --fw "katapult" --chipset "$CONFIG_MCU"

    if [ ${#UnknownSerials[@]} -gt 0 ]; then
        for dev in "${UnknownSerials[@]}"; do
            # Prompt the user with a default of 'No' (indicated by capital N)
			read -p "Unassigned Katapult device ($dev) detected. Add this device? [y/N]: " response
			case "$response" in
				[yY][eE][sS]|[yY])
					echo "Adding device..."
					local serial="${dev##*_}"
					jq --arg type "$mcu" --arg serial "$serial" 'map(if .type == $type then .serials += [$serial] else . end)' "$MCUS_JSON" > "${MCUS_JSON}.tmp" \
					&& mv "${MCUS_JSON}.tmp" "$MCUS_JSON"
					;;
				*)
					echo "Skipping device."
					# Your 'skip' logic goes here (or just leave empty to continue loop)
					;;
			esac
        done
    else
        echo "Device not found yet. You may need to reset the MCU manually to drop into Katapult bootloader mode."
    fi
}

MakeMenuConfig() {
	local params=$(getopt -o f:t: --long fw:,type: -- "$@")
    local type=""
    local fw=""
    local config="$SETTINGS_PATH/$type/$fw.config"
    eval set -- "$params"

    while true; do
        case "$1" in
            -f | --fw)
                fw="$2"
                shift 2 ;;
            -t | --type)
                type="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: MakeMenuConfig $@"
                exit 1
                ;;
        esac
    done

    # Ensure the configuration destination directory exists
    mkdir -p "$(dirname "$config")"

    # Switching directory for make
    pushd "$HOME/$fw" >/dev/null || exit

    # Pause so the user is aware of what config is loading
    printf "Making config for %s with %s\n" "$type" "$fw"
    read -n 1 -s -r -p "Press any key to continue..."
    echo ""
    make menuconfig KCONFIG_CONFIG="$config"

    # Returning to previous dir
    popd >/dev/null || exit
}

BuildFW() {
	local params=$(getopt -o f:t: --long fw:,type: -- "$@")
    local type=""
    local fw=""
    local config="$SETTINGS_PATH/$type/$fw.config"
    local fwOut="$SETTINGS_PATH/$type/$fw.bin"
    local extra_src=""
    eval set -- "$params"

    while true; do
        case "$1" in
            -f | --fw)
                fw="$2"
                shift 2 ;;
            -t | --type)
                type="$2"
                shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: BuildFW $@"
                exit 1
                ;;
        esac
    done

    # Ensure the configuration exists
	if [ ! -f "$config" ]; then
		echo "Configuration file not found for $mcu ($fw). Launching menuconfig..."
		make_menuconfig "$mcu" "$fw"
	fi

	extra_src=$(GetExtraArgs --fw "$fw" --type "$type")
    # Switching directory for make (Fixed $BuildFW -> $fw)
    pushd "$HOME/$fw" >/dev/null || exit

	# if we have extra source files to include we need to add it to the make file
	# as the currentl Makefile setup does not allow for us to add them via the make command.
	if [ $extra_src ]; then
		echo "Adding Extra lines to src/Makefile"
		# backup contents
		MakefileContent=$(<src/Makefile)

		# add the extra src
		echo $extra_src >> src/Makefile
	fi

    printf "Building %s for %s\n" "$fw" "$mcu"
    
    make clean KCONFIG_CONFIG="$config"
    make KCONFIG_CONFIG="$config" $extra_make_args

	# restore contents.
	if [ $extra_src ]; then
		echo $MakefileContent > src/Makefile
	fi

    # Copying to compiled firmware mcu folder to keep (Streamlined for strict .bin outputs)
    cp -f "out/$fw.bin" "$fwOut"

    # Returning to previous dir
    popd >/dev/null || exit
}

# This is only required for flashing new STM32 Devices once Katapult is on them it's no longer needed.
FlashNewSTM32() {
	local fwImg=$1
	echo "Looking for STM32 device in DFU mode via dfu-util..."
	# Check if a DFU device exists
	if [ dfu-util -l | grep -ic "Found DFU" ]; then
		echo "DFU device found. Flashing Katapult via dfu-util..."
		# STM32 DFU flashing typically targets internal flash at 0x08000000
		dfu-util -a 0 -d 0483:df11 -D $fwImg -s 0x08000000:force:mass-erase:leave
		echo "Flash command sent. Wait for device to reboot into Katapult."
	else
		echo "CRITICAL: No STM32 DFU device detected! Make sure it is jumpered/button-pressed into DFU mode."
	fi
}


FlashNewRP2040() {
	echo "Looking for RP2040 device in BOOTSEL mode..."
	# RP2040 mounts as a USB mass storage block device when in BOOTSEL mode
	# We look for the Raspberry Pi volume label or standard mount points
	# Katapult build outputs a .uf2 file for rp2040 if configured correctly, but assuming raw binary conversion if needed.
	# Note: If your Katapult build outputs a .uf2, use that. Otherwise, we can use picotool.

	if command -v picotool &>/dev/null && picotool info &>/dev/null; then
		echo "RP2040 found via picotool. Flashing Katapult..."
		picotool load -x ~/mcus/"$mcuTypeName"-katapult.bin
	else
		# Fallback: Check if it's auto-mounted by the OS
		RP_MOUNT=$(lsblk -o MOUNTPOINTS,LABEL | grep -i "RPI-RP2" | awk '{print $1}')
		if [ -d "$RP_MOUNT" ]; then
			echo "Found RP2040 mounted at $RP_MOUNT. Copying uf2/bin..."
			# Katapult compilation creates a deployable file. If it creates a .uf2 copy that instead.
			cp ~/mcus/"$mcuTypeName"-katapult.bin "$RP_MOUNT"/
		else
			echo "CRITICAL: No RP2040 in BOOTSEL mode found via picotool or mount points."
		fi
	fi
}

FlashDevice() {
	local params=$(getopt -o f:s:p:t: --long fw:,serial:,path:,type:-- "$@")
    local fw=""
    local serial=""
	local path=""
	local type=""

    eval set -- "$params"

    while true; do
        case "$1" in
            -f | --fw)
                fw="$2"
                shift 2 ;;
            -s | --serial)
                serial="$2"
                shift 2 ;;
            -p | --path)
                path="$2"
                shift 2
				if [ ! -e $path ]; then
					echo "Path: $path not found"
					return 1
				fi ;;
			-t | --type)
				type="$2"
				shift 2 ;;
            --)
                shift
                break ;;
            *)
                echo "Invalid Call: flashDevice $@"
                exit 1
                ;;
        esac
    done

	if [ $path ] && ! [ $serial ]; then
		serial="${path##*_}"
	fi

	if ! [ $type ]; then
		type=$(GetMcuType --serial $serial)
	fi

	if ! [ $path ]; then
		path=$(GetMcuPath)
	fi

	fwImg="$SETTINGS_PATH/$type/$fw.bin"

	if [ ! -e $fwImg ]; then
		echo "Error: $fwImg not found"
		return 1
	fi

	if [ ! -e $path ]; then
		echo "Error $path not found"
	fi

	~/katapult/scripts/flashtool.py -d "$path" -f "$fwImage"
}