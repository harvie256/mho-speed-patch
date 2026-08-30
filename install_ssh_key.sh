#!/usr/bin/env bash
# Install your own SSH public key on the Rigol MHO934 for persistent root SSH.
#
# The scope (Android/RK3399) has root via ADB (`su`), sshd allows
# `PermitRootLogin without-password`, and reads /data/ssh/authorized_keys.
# That file is restored from /system/etc/security/authorized_keys.default on
# boot, so persistence requires editing the (read-only) /system partition.
#
# Safe to re-run: it won't add a duplicate key. Review before running.
set -euo pipefail

ADB="/tmp/claude-1000/-home-derryn-Source-rigol/79fd7ea6-930f-4af1-8d01-9f5904aeb297/scratchpad/platform-tools/adb"
SCOPE_IP="172.30.188.217"
KEYFILE="$HOME/.ssh/id_rigol"

# 1. Generate a keypair if you don't already have one.
if [[ ! -f "$KEYFILE" ]]; then
    echo ">> generating keypair at $KEYFILE (RSA-3072 for OpenSSH 7.1 compat)"
    ssh-keygen -t rsa -b 3072 -N "" -C "derryn@rigol-mho934" -f "$KEYFILE"
fi
KEY="$(cat "$KEYFILE.pub")"
echo ">> public key: $KEY"

"$ADB" connect "$SCOPE_IP:55555" >/dev/null 2>&1 || true

# 2. Add to the live file (immediate effect) unless already present.
echo ">> adding to /data/ssh/authorized_keys (live)"
"$ADB" shell "su -c 'grep -qF \"$KEY\" /data/ssh/authorized_keys || echo \"$KEY\" >> /data/ssh/authorized_keys'"

# 3. Add to the .default on /system (survives reboot). Remount rw, edit, ro.
# Rockchip/toybox: remount by mountpoint returns EBUSY; naming the source
# device + fs type works. Try the by-name source first, then fallbacks.
echo ">> adding to /system/etc/security/authorized_keys.default (persistent)"
SYSSRC="/dev/block/platform/fe320000.dwmmc/by-name/system"
"$ADB" shell "su -c '
  set -e
  mount -o remount,rw -t ext4 $SYSSRC /system 2>/dev/null ||
  mount -o rw,remount $SYSSRC /system 2>/dev/null ||
  mount -o remount,rw /system
  grep -qF \"$KEY\" /system/etc/security/authorized_keys.default 2>/dev/null ||
    echo \"$KEY\" >> /system/etc/security/authorized_keys.default
  sync
  mount -o remount,ro -t ext4 $SYSSRC /system 2>/dev/null ||
  mount -o ro,remount $SYSSRC /system 2>/dev/null ||
  mount -o remount,ro /system 2>/dev/null || true
'"

# 4. Verify both files contain our key.
echo ">> verification (should show our key in both):"
"$ADB" shell "su -c 'echo live:; grep -F \"$KEY\" /data/ssh/authorized_keys; echo default:; grep -F \"$KEY\" /system/etc/security/authorized_keys.default'"

# 5. Test the SSH login (legacy host-key algo required for OpenSSH 7.1).
echo ">> testing SSH login as root ..."
ssh -i "$KEYFILE" \
    -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa \
    -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 \
    root@"$SCOPE_IP" 'echo SSH-OK: $(id); uname -a' || {
        echo "!! SSH test failed (key installed, but login didn't work -- investigate)"; exit 1; }

echo
echo ">> Done. Connect any time with:"
echo "   ssh -i $KEYFILE -o HostKeyAlgorithms=+ssh-rsa root@$SCOPE_IP"
