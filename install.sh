#!/bin/bash
# Installs the klipper-updater Moonraker agent.
#
# Idempotent: safe to re-run, which matters because Moonraker's update manager
# re-runs it after every update.

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
KATAPULT_PATH="${KATAPULT_PATH:-${HOME}/katapult}"
PRINTER_DATA="${PRINTER_DATA:-${HOME}/printer_data}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/klipper-updater}"
SETTINGS_PATH="${SETTINGS_PATH:-${HOME}/mcus}"
SERVICE_NAME="klipper_updater_agent"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

set -eu
export LC_ALL=C

function preflight_checks {
    if [ "$EUID" -eq 0 ]; then
        echo "[PRE-CHECK] This script must not be run as root!"
        exit 1
    fi

    if [ "$(sudo systemctl list-units --full -all -t service --no-legend | grep -F 'moonraker.service')" ]; then
        printf "[PRE-CHECK] Moonraker service found! Continuing...\n\n"
    else
        echo "[ERROR] Moonraker service not found. This agent is useless without it."
        exit 1
    fi

    if [ -z "${PYTHON_BIN}" ]; then
        echo "[ERROR] python3 not found on PATH."
        exit 1
    fi

    # 3.9 is the floor; that is what ships on the Raspberry Pi OS release most
    # printers run.
    if ! "${PYTHON_BIN}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
        echo "[ERROR] python3 >= 3.9 required, found $(${PYTHON_BIN} -V)"
        exit 1
    fi
    printf "[PRE-CHECK] Using %s (%s)\n\n" "${PYTHON_BIN}" "$(${PYTHON_BIN} -V)"
}

function check_paths {
    # Warnings, not errors: the agent is read-only in this phase and is still
    # useful for viewing status even if you cannot build or flash yet.
    if [ ! -d "${KLIPPER_PATH}" ]; then
        echo "[WARN] ${KLIPPER_PATH} not found - klipper firmware cannot be built."
    fi
    if [ ! -f "${KATAPULT_PATH}/scripts/flashtool.py" ]; then
        echo "[WARN] ${KATAPULT_PATH}/scripts/flashtool.py not found - flashing unavailable."
    fi
    if [ ! -f "${KLIPPER_PATH}/lib/kconfiglib/kconfiglib.py" ]; then
        echo "[WARN] vendored kconfiglib not found - the web config editor will be unavailable."
    fi
    if [ ! -S "${PRINTER_DATA}/comms/moonraker.sock" ]; then
        echo "[WARN] ${PRINTER_DATA}/comms/moonraker.sock not present yet."
        echo "       The agent retries on a loop, so this resolves itself once Moonraker is up."
    fi
    printf "\n"
}

function check_config {
    # load_data used to swallow a JSON error and read as "no MCU types
    # configured", which meant the next add-type could overwrite the whole
    # registry. It now refuses - so surface a broken file here, loudly, before
    # the agent starts and reports it as an error to the UI.
    if [ ! -f "${SETTINGS_PATH}/mcus.json" ]; then
        printf "[CONFIG] No registry at %s/mcus.json yet - nothing to validate.\n\n" "${SETTINGS_PATH}"
        return 0
    fi
    if PYTHONPATH="${INSTALL_PATH}/src" "${PYTHON_BIN}" -c '
import sys
from klipper_updater.config import Registry
from klipper_updater.paths import Paths
reg = Registry.load(Paths.from_env())
print(f"[CONFIG] {len(reg)} MCU type(s), {len(reg.all_serials())} tracked serial(s).")
'; then
        printf "\n"
    else
        echo "[ERROR] ${SETTINGS_PATH}/mcus.json could not be read. Fix or restore it, then re-run."
        exit 1
    fi
}

function install_service {
    echo "[INSTALL] Installing systemd unit ${SERVICE_NAME}.service..."
    local tmp
    tmp="$(mktemp)"
    sed -e "s|%USER%|${USER}|g" \
        -e "s|%INSTALL_DIR%|${INSTALL_PATH}|g" \
        -e "s|%PRINTER_DATA%|${PRINTER_DATA}|g" \
        -e "s|%PYTHON%|${PYTHON_BIN}|g" \
        "${INSTALL_PATH}/scripts/klipper_updater_agent.service" > "${tmp}"

    sudo cp "${tmp}" "/etc/systemd/system/${SERVICE_NAME}.service"
    rm -f "${tmp}"
    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    sudo systemctl restart "${SERVICE_NAME}.service"
    printf "[INSTALL] Service installed and started.\n\n"
}

function add_asvc {
    # Lets you restart the agent from Mainsail's own Services UI.
    local asvc="${PRINTER_DATA}/moonraker.asvc"
    if [ ! -f "${asvc}" ]; then
        echo "[MOONRAKER] ${asvc} not found, skipping allow-list entry."
        return 0
    fi
    if grep -qxF "${SERVICE_NAME}" "${asvc}"; then
        printf "[MOONRAKER] %s already in moonraker.asvc.\n\n" "${SERVICE_NAME}"
    else
        echo "${SERVICE_NAME}" >> "${asvc}"
        printf "[MOONRAKER] Added %s to moonraker.asvc.\n\n" "${SERVICE_NAME}"
    fi
}

function add_update_manager {
    local conf="${PRINTER_DATA}/config/moonraker.conf"
    if [ ! -f "${conf}" ]; then
        echo "[MOONRAKER] ${conf} not found, skipping update_manager entry."
        return 0
    fi
    if grep -q "^\[update_manager klipper-updater\]" "${conf}"; then
        printf "[MOONRAKER] update_manager entry already present.\n\n"
    else
        echo "[MOONRAKER] Adding update_manager entry to moonraker.conf..."
        {
            printf "\n"
            cat "${INSTALL_PATH}/scripts/moonraker-update-manager.conf"
        } >> "${conf}"
        printf "[MOONRAKER] Added. Restart Moonraker for it to take effect.\n\n"
    fi
}

function restart_moonraker {
    echo "[MOONRAKER] Restarting Moonraker so the new config applies..."
    sudo systemctl restart moonraker
    printf "[MOONRAKER] Done.\n\n"
}

function print_next_steps {
    cat <<EOF
================================================================
 klipper-updater agent installed.

 Check it registered with Moonraker:

   curl -s http://localhost:7125/server/extensions/list

 ...should list an agent named "klipper_updater". Then try it:

   curl -s -X POST http://localhost:7125/server/extensions/request \\
     -H 'Content-Type: application/json' \\
     -d '{"agent":"klipper_updater","method":"fw.status","arguments":{}}'

 Logs:  ${PRINTER_DATA}/logs/klipper_updater_agent.log
 Status: sudo systemctl status ${SERVICE_NAME}

 The CLI is unchanged and still works:  ${INSTALL_PATH}/src/updatefw.py status

 For the Mainsail panel, point your Update Manager at the fork by changing
 one line in moonraker.conf:

   [update_manager mainsail]
   repo: Vylyne/mainsail        # was mainsail-crew/mainsail

 This agent is READ-ONLY. It cannot build or flash anything yet.
================================================================
EOF
}

preflight_checks
check_paths
check_config
install_service
add_asvc
add_update_manager
restart_moonraker
print_next_steps
