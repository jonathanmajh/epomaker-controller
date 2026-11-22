#!/bin/bash
set -euo pipefail

# Installs a udev rule so the logged-in desktop user (and only that user) can
# talk to the Epomaker DynaTab 75X over USB HID, without running as root.
#
# Access is granted with TAG+="uaccess", which gives the user at the active
# seat an ACL on the device. It is deliberately NOT world-accessible: a
# MODE="0666" rule on hidraw would let any local process read the keyboard's
# raw input reports (i.e. log keystrokes).
#
# The file is named 70-* on purpose: uaccess tags must be applied before
# 73-seat-late.rules runs, so a 99-* name would silently have no effect.

RULE_FILE="/etc/udev/rules.d/70-epomaker-dynatab.rules"
OLD_RULE_FILE="/etc/udev/rules.d/99-epomaker-dynatab.rules"

echo "Creating udev rule file at ${RULE_FILE}..."

sudo tee "${RULE_FILE}" >/dev/null <<'EOF'
# Epomaker DynaTab 75X udev rules
# For hidraw backend
KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="3151", ATTRS{idProduct}=="4015", MODE="0660", TAG+="uaccess"
# For libusb backend
SUBSYSTEM=="usb", ATTRS{idVendor}=="3151", ATTRS{idProduct}=="4015", MODE="0660", TAG+="uaccess"
EOF

# Remove the world-writable (0666) rule written by older versions of this script.
if [ -e "${OLD_RULE_FILE}" ]; then
    echo "Removing old rule file ${OLD_RULE_FILE}..."
    sudo rm -f "${OLD_RULE_FILE}"
fi

echo "Reloading udev rules..."
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=usb

echo "udev rules configured. Unplug and replug the keyboard, then you can"
echo "control it without sudo (from a normal desktop login session)."
