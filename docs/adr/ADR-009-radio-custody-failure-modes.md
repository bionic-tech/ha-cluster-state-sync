# ADR-009: Radio custody follows the lease only on host loss

**Status:** Accepted — records a **known, unfixed gap** and why it is not being patched blind
**Date:** 2026-09-07
**Deciders:** mmanning (project owner)

## Context

A promoted Home Assistant that cannot reach the house's radios is not a working house. On this
fleet the radios are the difference between a failover and an outage: Zigbee via a ConBee II, 433
MHz via three RFXtrx transceivers, and the light switches people actually press are on the 433
side.

Two of those transceivers and the ConBee are reached over **USB-over-IP (VirtualHere)**, so they
are not bolted to either node and *can* move. Promotion hooks claim them:
`pre-start.d/10-virtualhere-claim.sh` runs before the device pre-flight and before
`docker start`, waits for the by-id paths to appear, and gives up after `DEADLINE=45s`.

Cold-boot RTO was measured on the real pair on 2026-09-07 and **passed**: host loss simulated on
the leader, standby serving HTTP 200 in **78.8 s** against a 150 s budget.

**The same test showed the promoted node had no radios at all.**

```
19:58:37  timed out waiting for: usb-RFXCOM_RFXtrx433 usb-dresden_elektronik
19:58:37  Attached serial devices: 0
19:58:37  3 config entr(ies) reference absent hardware -> Disabled 3
```

The device pre-flight behaved correctly — disabling entries whose hardware is absent is what lets
Home Assistant start at all instead of failing setup three times. But the house had nothing to
talk to.

**The cause is the failure mode, not the code.** The simulation stopped the promoter and killed
Home Assistant, leaving the node's `virtualhereclient.service` running and still holding its USB
claims. **Nothing reaps a claim from a client that is alive.** Proven by completing the
simulation: the moment that client was stopped, the peer claimed every device via Auto-Use in
**~12 seconds** — comfortably inside the 45 s deadline.

## Decision

### 1. State the limitation precisely rather than paper over it

> **CORRECTED 2026-09-07, same day.** The first version of this ADR said radios do not follow when
> "Home Assistant dies, host alive". **That was wrong**, and wrong for the same reason the day's
> other two errors were: a conclusion generalised from a simulation that did not reproduce the case
> it claimed to.
>
> The simulation stopped the promoter *and* killed Home Assistant. Stopping the promoter is what
> prevented the release — not the failure being modelled.
>
> Verified afterwards by reading the installed chain rather than inferring it:
> `cluster_promoter.py:661` runs `notify_backup.sh` on demotion → which stops the container and
> then runs `post-stop.d/` → where `90-virtualhere-release.sh` stops the VirtualHere client and
> waits for the devices to disappear. That hook **is installed on both nodes.**

| Failure | What releases the claims | Radios follow? |
|---|---|---|
| Host loss, power cut, kernel panic | the USB/IP **server reaps a dead client** (~17 s measured) | ✅ within the 45 s claim deadline |
| Home Assistant dies, host alive | the node's **own demote path**, after the 600 s probe grace | ✅ **but only after ~10 minutes** |
| **Promoter dies, host and USB/IP client alive** | **nothing** | ❌ **the real gap** |

**The genuine gap is narrow.** It needs the promoter itself to stop while the machine, and its
USB-over-IP client, keep running. Then nothing releases the lease (the peer still takes it once the
30 s TTL expires) and nothing releases the radios, because no server reaps a claim from a live
client. `systemd` would normally restart the timer, so this is an unusual state — but it is exactly
the state the RTO test induced, which is why it was seen at all.

**The second row is the one that matters in practice**, and it is a *latency* problem rather than a
custody one: an HA-only failure moves the radios correctly, ~10 minutes late, because the demote
path is gated behind the full probe grace. That is the same 600 s that makes this failure mode miss
the 2.5-minute budget ([ADR-001](./ADR-001-active-passive-topology.md)), so it is one decision with
two symptoms, not two problems.

### 2. Do NOT lengthen `DEADLINE` to "fix" it

The claims are held **indefinitely**, not slowly. A longer deadline converts a fast, honest,
radio-less promotion into a slow one that is still radio-less, and delays the point at which Home
Assistant starts serving anything at all. The 45 s value is correct for the case it can serve.

### 3. A directly-attached USB radio can never fail over, and this is architectural

The third RFXtrx (`07VYAMCJ`) is plugged into the leader's own USB bus. No lease, hook or
protocol can move it. **Any radio that is not on a network-reachable transport is outside the
failover boundary by construction** — as is any radio whose consumer is a separate container that
does not itself fail over (deCONZ, Zigbee2MQTT, ESPHome).

This belongs in the deployment guidance, not in a footnote: a reader choosing hardware needs to
know that *how the radio is attached* decides whether it can participate at all.

### 4. Leave the partial-failure gap open, deliberately

With the correction above, the decision splits in two.

**For the latency (HA dies, radios follow 10 minutes later):**

| Approach | Cost |
|---|---|
| Shorten `--probe-grace` | Also shortens how long a leader rides out an ordinary Home Assistant restart, which is what the grace exists for. Trades one failure mode against another. |
| Release the radios early, before releasing the lease | Radios move fast while leadership stays put — but a node that has surrendered its radios and kept the lease is a new and worse state. |
| Accept ~10 minutes for this failure mode | Free, and honest, provided the docs say so plainly. |

**For the genuine gap (promoter dies, everything else alive):**

| Approach | Cost |
|---|---|
| Tie the USB/IP client's lifetime to holding the lease | The invariant "not leader ⇒ not holding radios" is simple and locally checkable — but it is enforced *by the promoter*, which is the thing that died. |

**Chosen 2026-09-08, for the residual gap: harden and make it visible.** The promoter now publishes
a heartbeat to `ha:cluster_state_sync:<ns>:promoters:<node_id>` on **every tick**, regardless of
leadership, with a 60 s TTL.

That key exists because **nothing else on a cold standby can answer the question.** The node
registry (`nodes:*`) is refreshed by the integration, whose Home Assistant is deliberately stopped
there — which is why `cluster_members` reads 1 on a *healthy* two-node cold cluster and would read
1 on a broken one too. The promoter is the only process running, so it is the only possible
reporter.

It is written **first in the tick**, before any branch can return early, so a node that is held,
holding down, or failing its probe still says it is alive — those are exactly the states an
operator is watching. And it is best-effort in the strongest sense: a failed heartbeat is
swallowed, because a promoter that skipped a promotion in order to write a diagnostic would be a
far worse bug than a missing diagnostic. Guarded by
`test_a_failed_heartbeat_never_changes_a_decision`.

This does not *close* the gap — a stopped promoter still holds its USB claims. It converts it from
silent to visible, which is what was asked for and is the honest limit of a change that does not
reach across ADR-005's boundary.
| Promoting node asks the USB/IP **server** to reap a named claim | The only approach that survives the failed node being unresponsive — and it reaches across a boundary this project deliberately does not cross ([ADR-005](./ADR-005-generate-not-control.md)). |
| Accept it; rely on `systemd` restarting the timer | Free. The failure requires the promoter to stay dead, which `Restart=` largely prevents. |

Choosing between them is a design decision that wants the owner, not a patch chosen under time
pressure. **Recording the gap accurately is worth more than closing it badly** — this project's
founding incident was a feature that appeared to work.

## Consequences

**Good.** The failover story is now qualified by failure mode rather than asserted generally. The
measured numbers (78.8 s promotion, ~17 s reap, 45 s deadline, ~12 s claim) are all on the record
and consistent with each other.

**Bad, and accepted for now.** A Home-Assistant-only failure produces a promoted node serving a
house it cannot control by radio. The pre-flight makes this *visible* — entries are disabled and
logged — rather than silent, which is the least this can do while the gap is open.

**Follow-on.** GOTCHAS §18d carries the operational detail. TODO carries the open item. Neither
the README nor the installation runbook may claim unqualified "automatic failover" for an estate
whose radios matter, and both are updated accordingly.

## Related

- `docs/GOTCHAS.md` §17 (container `/dev` is a snapshot), §18b (`STOP USING` clears Auto-Use),
  §18c (a reclaimed radio returns on a different node), §18d (this gap)
- [ADR-001](./ADR-001-active-passive-topology.md) — the RTO budget this was measured against
- [ADR-005](./ADR-005-generate-not-control.md) — why the promoter does not reach into the device server
- `docs/GUIDE-radios.md` — the reader-facing version of §3
