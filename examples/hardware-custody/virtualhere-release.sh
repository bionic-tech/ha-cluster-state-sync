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
#
# 🚨 AR-0054. This waited 15 seconds and then `exit 1`, which the promoter
# reports as "this node is DEGRADED". A failover drill on 2026-09-09 showed a
# *normal, successful* handover trips it: the devices were still listed at 15s,
# the hook reported DEGRADED, and the peer claimed all five radios about six
# seconds later. Nothing had gone wrong.
#
# A status that says DEGRADED on the happy path is a status nobody reads, and
# this one sits on the promotion path where being ignored costs the most. So
# the wait now matches what the hardware actually does, and the two outcomes
# are told apart:
#
#   * gone within the wait          -> success, silent
#   * still there after the wait    -> WARN and exit 0. The peer waits for the
#                                      VirtualHere reap, which is a delay, not
#                                      a failure -- and the demotion itself
#                                      succeeded either way.
#
# Measure your own reap timeout and set RELEASE_WAIT to comfortably exceed the
# time your devices take to disappear. On the fleet this was written for they
# were gone by ~21s, so 15 was simply too tight.
RELEASE_WAIT="${RELEASE_WAIT:-30}"

for _ in $(seq 1 "$RELEASE_WAIT"); do
    compgen -G "/dev/serial/by-id/*usb-RFXCOM*" > /dev/null || exit 0
    sleep 1
done

# Deliberately exit 0. The radios not having detached yet delays the peer; it
# does not mean this node failed to demote, and marking the node DEGRADED for
# it trained the operator to ignore a real signal.
echo "radios still attached after ${RELEASE_WAIT}s; the peer will wait for the" \
     "VirtualHere reap before it can claim them" >&2
exit 0
