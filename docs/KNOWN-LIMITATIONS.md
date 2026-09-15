# Known limitations

Read this before installing, not after something surprises you.

Everything here is **true, unfixed, and deliberate** — a property of the design
rather than a defect in it. Some of these were found the expensive way on a real
house. None of them is a secret, and none is going to be quietly fixed and
removed from this page without the change appearing in `CHANGELOG.md`.

If something here is a dealbreaker for you, that is the right outcome. Finding
out at 3am is not.

---

## Both instances run automations during the overlap

There is **no leader election for automations**, on purpose.

During a failover both instances can be live for a few seconds. Both will run
their automations. If you have one that is not safe to run twice — anything that
toggles rather than sets, anything that increments a counter, anything that
sends a message — it can happen twice.

**What to do about it:** make such automations idempotent. Write `turn_on`, not
`toggle`. Guard anything expensive with a condition on current state.

Do **not** make them conditional on being the leader: the whole point of the
cluster is that leadership changes, and an automation that only runs on one
named node is an automation that stops when that node does.

## One radio never fails over, and it may be your most important one

USB devices move between nodes through VirtualHere. A radio plugged directly
into one machine cannot move, because it is physically attached to it.

On the estate this was built for, that radio is the transceiver a firewall
recovery watchdog uses. So failing over removes the last-resort recovery path
for the thing most likely to need recovering. **That is not a bug and cannot be
fixed in software** — it needs a second radio on the other machine, and then a
way to map one onto the other, which is an open design problem rather than a
missing feature.

**What to do about it:** know which of your radios is hardwired, and decide
whether you can live without it while the standby is serving.

## A promoted standby has no devices you have not arranged to move

It restores state. It does not conjure hardware. Anything reachable only from
the other machine — a USB stick, a Bluetooth adapter, a serial device — is gone
until that machine comes back.

## Dependencies in their own containers do not fail over

This integration replicates Home Assistant's state and configuration. It does
not move the other services your automations depend on.

If your Zigbee coordinator, MQTT broker, or database run in separate containers
on the leader, promoting the standby gives you a Home Assistant that starts
cleanly and cannot reach any of them.

**Accepted and out of scope, deliberately.** Making it work would mean this
project becoming a general-purpose container orchestrator, which is a different
product with a much larger failure surface.

**What to do about it:** run shared dependencies somewhere neither node owns, or
accept that failover is partial.

## A Home Assistant-only failure is slow by design

Losing the whole machine is fast: measured at **~102 seconds to a working,
initialised standby**.

Home Assistant dying *on its own*, while the machine stays up, is **not**. The
leader keeps renewing its lease for the full probe grace before releasing —
around **ten and a half minutes** on the default settings.

That is intentional. A short grace means a Home Assistant that is slow to
restart, or briefly wedged, hands your house to the other machine for no reason.
The cost of that trade is that one specific failure mode does not beat the
2.5-minute budget, and saying the budget covers every failure would be untrue.

## A node can be healthy to itself and unreachable to you

The promoter checks Home Assistant at `127.0.0.1`. A node whose service network
interface has gone — while the machine is otherwise fine — looks perfectly well
from the inside, keeps the lease, and **will not fail over**.

This has happened, for 61 minutes, on a real installation.

The ingress probe detects it and will alert you if you have
`ingress_unreachable` in your alert conditions (it is in the defaults). But
detection is not reaction: **nothing will move the house for you**, and it will
not clear on its own.

**What to do about it:** keep the ingress alert enabled, and when it fires,
check that node's own IP address on its service interface first. An interface
can keep its link and lose its address, and its VLANs can survive while the
parent does not.

## The restore writes state; it does not command devices

For `light`, `switch`, `cover` and other device-backed domains, restoring writes
what Home Assistant *believes* — the integration that owns the device corrects it
on the next poll, usually within seconds.

So replicating those domains is cosmetic. It will not turn your lights on, and
it will not keep them on either. `automation` is the exception, because it is
applied by calling a service rather than by writing state.

See [`GUIDE-choosing-domains.md`](GUIDE-choosing-domains.md) for which domains
are worth replicating and the edge cases where the usual answer flips.

## The namespace is not a security boundary

Anything holding the datastore credential can read any namespace. The namespace
is an organisational label, so that two clusters can share one Valkey without
colliding — not isolation between tenants who do not trust each other.

## A reload loses an active alert's acknowledgement state

Fixed in 0.5.2 for the common case: a reload no longer re-announces conditions
that are still true, and an alert that clears afterwards still sends its
all-clear.

What remains: this state lives in memory, so a **full Home Assistant restart**
re-evaluates everything. If a condition is still true after a restart, you will
be told about it again. That is correct for a restart and would be wrong for a
reload, which is why the two are treated differently.

## An unwritable `/run` restart-loops the leader

If `/run` is not writable — a read-only root, an unusual container runtime — the
promoter cannot record its own state and the leader will restart repeatedly.
Recorded rather than handled, because every environment this has run on has a
writable `/run` and guessing at the alternative would be inventing a
requirement.

---

## What this project deliberately is not

It is **not native Home Assistant clustering**. It works entirely through public
Home Assistant APIs: no monkey-patching, no forked core. That is a constraint
chosen on purpose — fragility against a minor Home Assistant upgrade is exactly
the outcome this design exists to avoid — and it is why some things above cannot
be fixed from here.

## Reporting something not on this list

[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) covers what to send, and how to get a
diagnostics file you can read before you attach it.

If it is a security problem, [`SECURITY.md`](../SECURITY.md) — not a public
issue.
