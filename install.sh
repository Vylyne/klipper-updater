#!/bin/bash
# Installs the mcu-updater Moonraker agent.
#
# Idempotent: safe to re-run, which matters because Moonraker's update manager
# re-runs it after every update.

KLIPPER_PATH="${KLIPPER_PATH:-${HOME}/klipper}"
KATAPULT_PATH="${KATAPULT_PATH:-${HOME}/katapult}"
PRINTER_DATA="${PRINTER_DATA:-${HOME}/printer_data}"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/mcu-updater}"
CONFIG_PATH="${CONFIG_PATH:-${PRINTER_DATA}/config/mcu-updater}"
DATA_PATH="${DATA_PATH:-${PRINTER_DATA}/mcu-updater}"
# One file for everything hand-edited: the [updater] section and the [mcu ...]
# sections. Must match Paths.main_config.
MAIN_CONFIG="${CONFIG_PATH}/mcu-updater.cfg"
# udev rule letting dfu-util open a bare STM32 without root.
DFU_UDEV_RULE="${DFU_UDEV_RULE:-/etc/udev/rules.d/99-mcu-updater-dfu.rules}"
# Constrained from two directions:
#  * Moonraker only permits a `managed_services` value equal to the
#    [update_manager <name>] section, `klipper`, or `moonraker` - so the unit name
#    and that section name must agree.
#  * KIAUH finds instances with ^<component>(-[0-9a-zA-Z]+)?\.service$, so a unit
#    called klipper-* is taken for a Klipper instance and crashes its menu.
# Hence a name that starts with no component name at all.
SERVICE_NAME="mcu-updater"
LEGACY_SERVICE_NAMES="klipper_updater_agent klipper-updater"
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
    # Warnings, not errors: the agent is still worth having for status alone, and
    # the individual capabilities degrade rather than the whole thing failing.
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

function check_flash_deps {
    # flashtool.py does `import serial`. Without pyserial the failure lands in the
    # middle of a flash, with klipper already stopped - so check it up front and
    # offer to fix it.
    if "${PYTHON_BIN}" -c 'import serial' >/dev/null 2>&1; then
        printf "[DEPS] pyserial present.\n\n"
        return 0
    fi

    echo "[DEPS] python3-serial is missing. katapult's flashtool.py needs it, and"
    echo "       without it a flash fails part-way with klipper already stopped."
    local answer=""
    read -r -p "[DEPS] Install python3-serial with apt now? [Y/n]: " answer || answer=""
    case "${answer}" in
        n | N | no | NO)
            echo "[WARN] Skipped. Flashing will not work until you run:"
            printf "       sudo apt install python3-serial\n\n"
            return 0
            ;;
    esac

    if sudo apt-get install -y python3-serial; then
        if "${PYTHON_BIN}" -c 'import serial' >/dev/null 2>&1; then
            printf "[DEPS] Installed.\n\n"
        else
            # e.g. PYTHON_BIN is a venv without system site-packages.
            echo "[WARN] python3-serial installed, but ${PYTHON_BIN} still cannot import it."
            printf "       Flashing will not work until that interpreter can.\n\n"
        fi
    else
        printf "[WARN] apt install failed. Run 'sudo apt install python3-serial' yourself.\n\n"
    fi
}

function check_dfu_permissions {
    # dfu-util needs raw USB access. Without a udev rule it prints
    #   Cannot open DFU device 0483:df11 ... (LIBUSB_ERROR_ACCESS)
    # and lists nothing - which for a long time we reported as "no DFU device
    # detected, hold BOOT0 and replug", sending people to redo the one step that
    # had actually worked. A rule fixes it for both the CLI and the agent, so
    # neither needs sudo at flash time.
    #
    # Only relevant for installing Katapult onto a bare STM32 (add-mcu). Boards
    # that already have Katapult never touch dfu-util.
    if ! command -v dfu-util >/dev/null 2>&1; then
        printf "[DFU]  dfu-util not installed - only needed for add-mcu on a bare board.\n\n"
        return 0
    fi
    if [ -f "${DFU_UDEV_RULE}" ]; then
        printf "[DFU]  udev rule already present.\n\n"
        return 0
    fi

    echo "[DFU]  No udev rule for STM32 DFU mode (${DFU_UDEV_RULE})."
    echo "       Without it dfu-util cannot open a board in DFU mode, and add-mcu"
    echo "       fails on a board whose boot jumper is perfectly fine."
    local answer=""
    read -r -p "[DFU]  Install the udev rule now? [Y/n]: " answer || answer=""
    case "${answer}" in
        n | N | no | NO)
            echo "[WARN] Skipped. add-mcu on a bare board will need sudo until you add it."
            printf "\n"
            return 0
            ;;
    esac

    local tmp
    tmp="$(mktemp)"
    cat > "${tmp}" <<'RULE'
# STM32 in DFU mode (0483:df11) - lets dfu-util open the device without root, so
# mcu-updater's add-mcu works from the CLI and from the Moonraker agent.
# Installed by mcu-updater's install.sh.
SUBSYSTEM=="usb", ATTR{idVendor}=="0483", ATTR{idProduct}=="df11", MODE="0664", GROUP="plugdev", TAG+="uaccess"
RULE
    sudo install -m 0644 -o root -g root "${tmp}" "${DFU_UDEV_RULE}"
    rm -f "${tmp}"

    # The rule grants access to the plugdev group, so the service account has to
    # be in it. A group change needs a fresh login - or for a daemon, a restart,
    # which install_service does later anyway.
    if getent group plugdev >/dev/null 2>&1; then
        if ! id -nG "${USER}" | tr ' ' '\n' | grep -qx plugdev; then
            echo "[DFU]  Adding ${USER} to the plugdev group..."
            sudo usermod -aG plugdev "${USER}"
            echo "[DFU]  Log out and back in for your shell to pick that up."
        fi
    else
        echo "[WARN] No plugdev group on this system; relying on TAG+=\"uaccess\"."
    fi

    sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
    echo "[DFU]  Rule installed. Replug a board in DFU mode for it to take effect."
    printf "\n"
}

function check_config {
    mkdir -p "${CONFIG_PATH}" "${DATA_PATH}"

    # A registry left at the pre-0.10 location would otherwise read as "no MCU
    # types configured", and the next add-type would write a fresh file while the
    # real one sat untouched. Refuse loudly instead.
    if [ -f "${HOME}/mcus/mcus.json" ] && [ ! -f "${MAIN_CONFIG}" ]; then
        echo "[ERROR] Found an old registry at ${HOME}/mcus/mcus.json but nothing at"
        echo "        ${MAIN_CONFIG}."
        echo "        The layout moved - see docs/layout.md for the handful of commands."
        echo "        Refusing to continue so an empty registry cannot overwrite anything."
        exit 1
    fi

    # Same again for the previous split-file layout. Settings now live in the
    # [updater] section of the main config; a leftover updater.conf is no longer
    # read, and enable_flashing silently reverting to false is worth saying out
    # loud rather than leaving someone to wonder where the flash buttons went.
    if [ -f "${CONFIG_PATH}/updater.conf" ]; then
        echo "[WARN]  ${CONFIG_PATH}/updater.conf is no longer read."
        echo "        Settings moved into the [updater] section of ${MAIN_CONFIG}."
        echo "        Copy anything you had set across, then delete it."
        printf "\n"
    fi
    if [ -f "${CONFIG_PATH}/mcus.cfg" ] && [ ! -f "${MAIN_CONFIG}" ]; then
        echo "[ERROR] The registry is now ${MAIN_CONFIG}, not ${CONFIG_PATH}/mcus.cfg."
        echo "        Rename it (settings and the [mcu ...] sections share one file now):"
        echo "            mv ${CONFIG_PATH}/mcus.cfg ${MAIN_CONFIG}"
        exit 1
    fi

    # A broken registry is surfaced here, loudly, rather than by the agent
    # reporting it as an error to the UI after the fact.
    if [ ! -f "${MAIN_CONFIG}" ]; then
        printf "[CONFIG] No config at %s yet - nothing to validate.\n\n" "${MAIN_CONFIG}"
        return 0
    fi
    # A traceback here would be noise: the exception message already says exactly
    # what is wrong and how to fix it, so print that and nothing else.
    if PYTHONPATH="${INSTALL_PATH}/src" "${PYTHON_BIN}" -c '
import sys
from mcu_updater.config import Registry
from mcu_updater.errors import UpdaterError
from mcu_updater.paths import Paths
from mcu_updater.settings import load_settings
paths = Paths.from_env()
try:
    reg = Registry.load(paths)
    print(f"[CONFIG] {len(reg)} MCU type(s), {len(reg.all_serials())} tracked serial(s).")
    # Same file, so a typo in [updater] is worth catching here too - the agent
    # would otherwise fall back to defaults with only a line in its log.
    s = load_settings(paths.settings_file)
    state = "ENABLED" if s.enable_flashing else "disabled"
    print(f"[CONFIG] Flashing from the web UI is {state}.")
except UpdaterError as exc:
    print(f"[ERROR] {exc}", file=sys.stderr)
    sys.exit(1)
'; then
        printf "\n"
    else
        echo "[ERROR] Fix ${MAIN_CONFIG}, then re-run."
        exit 1
    fi
}

function migrate_legacy_service {
    # Two previous names, each removed for a concrete reason:
    #   klipper_updater_agent - Moonraker rejects it as a managed_services value
    #   klipper-updater       - KIAUH mistakes it for a Klipper instance and, if
    #                           the unit is not world-readable, crashes its menu
    # Leaving either behind means two units racing for the same socket.
    local asvc="${PRINTER_DATA}/moonraker.asvc"
    local conf="${PRINTER_DATA}/config/moonraker.conf"
    local backed_up=0
    local legacy_name legacy_unit

    for legacy_name in ${LEGACY_SERVICE_NAMES}; do
        legacy_unit="/etc/systemd/system/${legacy_name}.service"
        if [ -f "${legacy_unit}" ]; then
            echo "[MIGRATE] Removing the old ${legacy_name} service..."
            sudo systemctl stop "${legacy_name}.service" 2>/dev/null || true
            sudo systemctl disable "${legacy_name}.service" 2>/dev/null || true
            sudo rm -f "${legacy_unit}"
            sudo systemctl daemon-reload
        fi

        if [ -f "${asvc}" ] && grep -qxF "${legacy_name}" "${asvc}"; then
            echo "[MIGRATE] Dropping stale ${legacy_name} from moonraker.asvc..."
            sed -i "/^${legacy_name}\$/d" "${asvc}"
        fi

        # Repair a moonraker.conf written by an earlier install. add_update_manager
        # only appends when the section is absent, so without this a re-run would
        # leave the stale section in place.
        if [ -f "${conf}" ] && grep -qE "^\[update_manager ${legacy_name}\]|^managed_services:[[:space:]]*${legacy_name}[[:space:]]*\$" "${conf}"; then
            if [ "${backed_up}" -eq 0 ]; then
                cp "${conf}" "${conf}.bak-mcu-updater"
                backed_up=1
            fi
            echo "[MIGRATE] Renaming the ${legacy_name} update_manager entry..."
            sed -i "s|^\[update_manager ${legacy_name}\]|[update_manager ${SERVICE_NAME}]|" "${conf}"
            sed -i "s|^managed_services:[[:space:]]*${legacy_name}[[:space:]]*\$|managed_services: ${SERVICE_NAME}|" "${conf}"

            # The section also carries path: and origin:, which still point at the
            # old clone and the old repo URL. Renaming only the header leaves
            # Moonraker updating a directory that may not exist any more - and
            # add_update_manager will not fix it, because it only appends when the
            # section is absent. Take the correct values from the shipped template
            # so they are defined in exactly one place.
            local want_path want_origin
            want_path="$(grep -m1 '^path:' "${INSTALL_PATH}/scripts/moonraker-update-manager.conf" || true)"
            want_origin="$(grep -m1 '^origin:' "${INSTALL_PATH}/scripts/moonraker-update-manager.conf" || true)"
            # Plain substitution rather than sed's `c\`, whose backslash handling
            # swallows the expansion and writes the variable name verbatim.
            # Scoped to lines mentioning the old name, so another add-on's
            # update_manager section is never touched. '|' is safe as the
            # delimiter: paths and URLs contain '/', never '|'.
            if [ -n "${want_path}" ]; then
                sed -i "s|^path:.*${legacy_name}.*|${want_path}|" "${conf}"
            fi
            if [ -n "${want_origin}" ]; then
                sed -i "s|^origin:.*${legacy_name}.*|${want_origin}|" "${conf}"
            fi
        fi
    done

    # Whatever happened above, an update_manager path that does not exist means
    # Moonraker will keep erroring on it. Say so rather than leaving it to be
    # discovered later.
    if [ -f "${conf}" ]; then
        local declared
        declared="$(sed -n "/^\[update_manager ${SERVICE_NAME}\]/,/^\[/p" "${conf}" \
            | grep -m1 '^path:' | sed 's/^path:[[:space:]]*//' || true)"
        if [ -n "${declared}" ]; then
            # shellcheck disable=SC2088
            case "${declared}" in "~/"*) declared="${HOME}/${declared#\~/}" ;; esac
            if [ ! -d "${declared}" ]; then
                echo "[WARN] moonraker.conf's update_manager path does not exist: ${declared}"
                echo "       Moonraker will error on that section. Expected ${INSTALL_PATH}."
            fi
        fi
    fi

    if [ "${backed_up}" -eq 1 ]; then
        printf "[MIGRATE] moonraker.conf updated (backup at %s.bak-mcu-updater).\n" "${conf}"
    fi

    # An earlier install.sh used `sudo cp` from a mktemp file, so the unit could
    # be mode 0600 and unreadable to anything scanning /etc/systemd/system.
    local unit="/etc/systemd/system/${SERVICE_NAME}.service"
    if [ -f "${unit}" ] && [ ! -r "${unit}" ]; then
        echo "[MIGRATE] Fixing permissions on ${unit}..."
        sudo chmod 0644 "${unit}"
    fi
    printf "\n"
}

function install_service {
    echo "[INSTALL] Installing systemd unit ${SERVICE_NAME}.service..."
    local tmp
    tmp="$(mktemp)"
    sed -e "s|%USER%|${USER}|g" \
        -e "s|%INSTALL_DIR%|${INSTALL_PATH}|g" \
        -e "s|%PRINTER_DATA%|${PRINTER_DATA}|g" \
        -e "s|%PYTHON%|${PYTHON_BIN}|g" \
        "${INSTALL_PATH}/scripts/${SERVICE_NAME}.service" > "${tmp}"

    # install, not cp: mktemp creates 0600, and cp would carry that mode over,
    # leaving a unit only root can read. Anything that scans /etc/systemd/system
    # as a normal user then dies on it - which is exactly how this broke KIAUH.
    sudo install -m 0644 -o root -g root "${tmp}" "/etc/systemd/system/${SERVICE_NAME}.service"
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
    if grep -q "^\[update_manager ${SERVICE_NAME}\]" "${conf}"; then
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

function allow_sudo_fallback {
    # The normal path needs no sudo: the agent stops klipper through Moonraker's
    # machine.services API. This is purely the safety net for Moonraker dying
    # between the stop and the start, when that API is unreachable and the agent
    # would otherwise be unable to bring klipper back.
    local target="/etc/sudoers.d/mcu-updater"
    if [ -f "${target}" ]; then
        printf "[SUDO] Fallback rule already installed.\n\n"
        return 0
    fi

    cat <<EOF
[SUDO] Optional safety net.

  The agent stops klipper via Moonraker, which needs no special privileges. But
  if Moonraker dies *between* the stop and the start, the agent cannot put
  klipper back, and the printer stays down until you notice.

  Installing a narrow sudoers rule (three exact systemctl commands for the
  klipper unit, no wildcards) lets the agent recover on its own. Declining is
  safe - the systemd unit's ExecStopPost still covers some cases - but the net
  is weaker, and the CLI will prompt for a password when it stops klipper.

EOF
    local answer=""
    read -r -p "[SUDO] Install /etc/sudoers.d/mcu-updater? [y/N]: " answer || answer=""
    case "${answer}" in
        y | Y | yes | YES) ;;
        *)
            printf "[SUDO] Skipped.\n\n"
            return 0
            ;;
    esac

    local tmp
    tmp="$(mktemp)"
    sed -e "s|%USER%|${USER}|g" "${INSTALL_PATH}/scripts/sudoers.d-mcu-updater" > "${tmp}"
    # Validate before installing: a malformed sudoers file can lock you out of
    # sudo entirely, so never copy one in unchecked.
    if sudo visudo -c -f "${tmp}" >/dev/null 2>&1; then
        sudo install -m 0440 -o root -g root "${tmp}" "${target}"
        printf "[SUDO] Installed %s\n\n" "${target}"
    else
        echo "[ERROR] Generated sudoers file failed validation; not installing it."
        sudo visudo -c -f "${tmp}" || true
    fi
    rm -f "${tmp}"
}

function restart_moonraker {
    echo "[MOONRAKER] Restarting Moonraker so the new config applies..."
    sudo systemctl restart moonraker
    printf "[MOONRAKER] Done.\n\n"
}

function print_next_steps {
    cat <<EOF
================================================================
 mcu-updater agent installed.

 Check it registered with Moonraker:

   curl -s http://localhost:7125/server/extensions/list

 ...should list an agent named "mcu_updater". Then try it:

   curl -s -X POST http://localhost:7125/server/extensions/request \\
     -H 'Content-Type: application/json' \\
     -d '{"agent":"mcu_updater","method":"fw.status","arguments":{}}'

 Logs:   ${PRINTER_DATA}/logs/mcu-updater.log
         (not in Mainsail's Logfiles panel - that lists a fixed set - but it is
          downloadable through Moonraker's file manager)
 Status: sudo systemctl status ${SERVICE_NAME}

 The CLI is unchanged and still works:  ${INSTALL_PATH}/src/updatefw.py status

 The systemd unit is 'mcu-updater', deliberately not 'klipper-*': KIAUH
 matches ^klipper(-[0-9a-zA-Z]+)?.service$ and would mistake it for a Klipper
 instance.

 For the Mainsail panel, point your Update Manager at the fork by changing
 one line in moonraker.conf:

   [update_manager mainsail]
   repo: Vylyne/mainsail        # was mainsail-crew/mainsail

 Config:    ${MAIN_CONFIG}     (backed up, editable in Mainsail)
              one file: the [updater] section and one [mcu ...] per board
 Artifacts: ${DATA_PATH}        (generated, not backed up)

 Flashing from the web UI is OFF by default. To enable it, add to the
 [updater] section of ${MAIN_CONFIG}:

   [updater]
   enable_flashing: true

 ...then: sudo systemctl restart ${SERVICE_NAME}
================================================================
EOF
}

preflight_checks
check_paths
check_flash_deps
check_dfu_permissions
check_config
migrate_legacy_service
install_service
add_asvc
add_update_manager
allow_sudo_fallback
restart_moonraker
print_next_steps
