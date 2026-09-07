#!/usr/bin/env bash
# EXAMPLE post-stop.d hook — VirtualHere. Copy, read, adapt. Not installed.
#
# Stops this node's VirtualHere client so the peer can claim the radios.
# Runs AFTER the container has stopped, so nothing is mid-read.
#
# This only covers an orderly demotion. A node that has crashed runs nothing,
# and the peer then waits for the VirtualHere server to reap a dead client's
# claim. Measure that timeout: it is your radio RTO.
set -euo pipefail

systemctl stop virtualhereclient.service || true

# Best effort: confirm the nodes have gone, so the peer is not racing us.
for _ in $(seq 1 15); do
    compgen -G "/dev/serial/by-id/*usb-RFXCOM*" > /dev/null || exit 0
    sleep 1
done
echo "radios still attached after 15s; peer may have to wait for the reap" >&2
exit 1
