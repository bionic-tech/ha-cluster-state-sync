#!/usr/bin/env bash
# EXAMPLE pre-start.d hook — VirtualHere. Copy, read, adapt. Not installed.
#
# Starts this node's VirtualHere client and waits for the radios to appear.
# Claiming is left to the client's own [AutoShare] rules in ~/.vhui, so this
# script never decides WHICH devices to take -- that stays the operator's,
# in the place they already configure it.
#
# 🚨 Never auto-share a USB-Ethernet device. On this fleet one radio host
# reaches the network through a USB LAN adapter, and claiming it takes the
# whole device off the network (GOTCHAS §15).
set -euo pipefail

# by-id fragments that must exist before Home Assistant starts.
REQUIRED=(
    "usb-RFXCOM_RFXtrx433"
    "usb-dresden_elektronik"
)
DEADLINE=$((SECONDS + 45))

systemctl start virtualhereclient.service

# Poll for the device NODES, not for the service being "started". The service
# returning says the client is running, not that the kernel has enumerated
# anything -- and the container's /dev is built from what exists right now.
while (( SECONDS < DEADLINE )); do
    missing=0
    for frag in "${REQUIRED[@]}"; do
        compgen -G "/dev/serial/by-id/*${frag}*" > /dev/null || missing=1
    done
    (( missing == 0 )) && { echo "all radios present after ${SECONDS}s"; exit 0; }
    sleep 1
done

echo "timed out waiting for: ${REQUIRED[*]}" >&2
exit 1
