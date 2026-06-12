#!/bin/bash

MCU_JSON="$HOME/mcus/mcus.json"

# Ensure the directory and MCU_JSON exist before doing anything
mkdir -p "$(dirname "$MCU_JSON")"
if [ ! -f "$MCU_JSON" ] || [ ! -s "$MCU_JSON" ]; then
    echo "{}" > "$MCU_JSON"
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

# No 'local' here, and no spaces around '='
addMCU="unset"

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

getMcu() {

}

addMcu() {

}
# If addMCU was set and isn't still "unset"
if [ "$addMCU" != "unset" ]; then
    
    # 1. Check if the MCU key already exists safely using --arg
    # -e sets an exit code (0 if true, 1 if false) based on the filter
    if jq -e --arg key "$addMCU" 'has($key)' "$MCU_JSON" > /dev/null; then
        echo "MCU '$addMCU' already exists in $MCU_JSON."
    else
        echo "Adding new MCU '$addMCU'..."
        
        # 2. Add the key as an empty array and save it atomically
        jq --arg key "$addMCU" '.[$key] = []' "$MCU_JSON" > "${MCU_JSON}.tmp" && mv "${MCU_JSON}.tmp" "$MCU_JSON"
        
        echo "Successfully updated $MCU_JSON."
    fi
fi