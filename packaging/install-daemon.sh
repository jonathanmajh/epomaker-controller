#!/bin/bash
# Install the user-level systemd unit and the udev rule that starts it when
# the keyboard is plugged in.
#
#   ./packaging/install-daemon.sh            # install + enable + start now
#   ./packaging/install-daemon.sh --uninstall
#
# The unit runs `epomakercontroller start-daemon`, rewritten to the absolute
# path of the executable currently on PATH (usually your venv). Activate the
# venv before running this script, or call the venv binary by path.
set -euo pipefail

UNIT_NAME="epomaker-daemon.service"
RULE_NAME="70-epomaker-daemon.rules"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

UNIT_DIR="${HOME}/.config/systemd/user"
RULE_DIR="/etc/udev/rules.d"

uninstall() {
    echo "Removing ${UNIT_DIR}/${UNIT_NAME}..."
    systemctl --user disable --now "${UNIT_NAME}" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${UNIT_NAME}"

    if [ -e "${RULE_DIR}/${RULE_NAME}" ]; then
        echo "Removing ${RULE_DIR}/${RULE_NAME}..."
        sudo rm -f "${RULE_DIR}/${RULE_NAME}"
        sudo udevadm control --reload-rules
    fi

    systemctl --user daemon-reload
    echo "Uninstalled."
}

if [ "${1:-}" = "--uninstall" ]; then
    uninstall
    exit 0
fi

if ! command -v epomakercontroller >/dev/null 2>&1; then
    echo "error: epomakercontroller not on PATH." >&2
    echo "       Create the venv first (see README), then retry." >&2
    exit 1
fi

echo "Installing ${UNIT_NAME} to ${UNIT_DIR}..."
mkdir -p "${UNIT_DIR}"

# Escape the sed replacement: & and \ are special, and | is our delimiter.
EPW_BIN="$(command -v epomakercontroller)"
EPW_ESCAPED="$(printf '%s' "${EPW_BIN}" | sed -e 's/[\\&|]/\\&/g')"
sed "s|^ExecStart=.*|ExecStart=${EPW_ESCAPED} start-daemon|" \
    "${SCRIPT_DIR}/${UNIT_NAME}" > "${UNIT_DIR}/${UNIT_NAME}"
chmod 644 "${UNIT_DIR}/${UNIT_NAME}"

echo "Installing ${RULE_NAME} to ${RULE_DIR}..."
sudo tee "${RULE_DIR}/${RULE_NAME}" >/dev/null < "${SCRIPT_DIR}/${RULE_NAME}"
sudo udevadm control --reload-rules

systemctl --user daemon-reload
systemctl --user enable "${UNIT_NAME}"
systemctl --user restart "${UNIT_NAME}"

echo
echo "Installed and started."
echo "  status: systemctl --user status ${UNIT_NAME}"
echo "  logs:   journalctl --user -u ${UNIT_NAME} -f"
echo
echo "To check that the udev rule starts it: stop it (systemctl --user stop ${UNIT_NAME}),"
echo "unplug and replug the keyboard, then look at the status above."
echo
echo "The daemon waits for the keyboard if it is absent or unplugged and"
echo "reconnects on its own, so it keeps running either way."
echo "If the unit ever lands in a failed state after a crash loop, run:"
echo "  systemctl --user reset-failed ${UNIT_NAME}"
echo
echo "To show a temperature too, run: systemctl --user edit ${UNIT_NAME}"
echo "and set Environment=EPOMAKER_TEMP_KEY=coretemp-0."
