#!/usr/bin/env bash
# Install a udev rule granting plugdev group rw access to Rigol USB-TMC
# instruments (vendor 1ab1, e.g. the MHO934 = 1ab1:0452), so /dev/usbtmcN
# no longer needs a manual chmod after each replug.
#
# Usage:  sudo ./install-udev-rule.sh
set -euo pipefail

RULE_FILE=/etc/udev/rules.d/60-rigol-usbtmc.rules

if [[ $EUID -ne 0 ]]; then
    echo "This script needs root. Re-run with:  sudo $0" >&2
    exit 1
fi

echo "Writing $RULE_FILE ..."
cat > "$RULE_FILE" <<'EOF'
# Rigol USB-TMC instruments (MHO934 = 1ab1:0452): grant plugdev group rw access.
# usbtmc char device (major 180) lives under the usbmisc subsystem:
SUBSYSTEM=="usbmisc", KERNEL=="usbtmc[0-9]*", ATTRS{idVendor}=="1ab1", GROUP="plugdev", MODE="0660"
# also relax the raw USB device node:
SUBSYSTEM=="usb", ATTRS{idVendor}=="1ab1", GROUP="plugdev", MODE="0660"
EOF

echo "Reloading udev rules ..."
udevadm control --reload-rules
echo "Reapplying to connected devices ..."
udevadm trigger --subsystem-match=usbmisc
udevadm trigger --subsystem-match=usb --attr-match=idVendor=1ab1 || true

echo
echo "Done. Current Rigol usbtmc nodes:"
ls -l /dev/usbtmc* 2>/dev/null || echo "  (none found -- is the scope plugged in and in USBTMC mode?)"
echo
echo "If a node still shows root:root 0600, unplug and replug the scope once."
