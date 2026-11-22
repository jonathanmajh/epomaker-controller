#!/bin/bash
# Install the user-level systemd unit and the udev rule that starts it when
# the keyboard is plugged in.
#
#   ./packaging/install-daemon.sh            # install + enable + start now
#   ./packaging/install-daemon.sh --uninstall
#
# The unit runs: %h/.venv/bin/epomakercontroller start-daemon
# (the venv location from the README). Override with EPW_PYTHON if your
# install lives elsewhere.
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
sed "s|^ExecStart=.*|ExecStart=$(command -v epomakercontroller) start-daemon|" \
    "${SCRIPT_DIR}/${UNIT_NAME}" > "${UNIT_DIR}/${UNIT_NAME}"

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
echo "Unplug and replug the keyboard to confirm the udev rule starts it."
echo "The daemon exits non-zero if it cannot reach the keyboard, and systemd"
echo "restarts it (Restart=on-failure, up to 5 times per minute)."