# ADR-006: The lease promoter replaces Keepalived

**Status:** Accepted
**Date:** 2026-09-01
**Deciders:** mmanning (project owner)
**Supersedes:** the leadership-transport parts of [ADR-001](./ADR-001-active-passive-topology.md)
**Extends:** [ADR-003](./ADR-003-leadership-resolution.md)

## Context

[ADR-001](./ADR-001-active-passive-topology.md) said leadership would be *"expressed by Keepalived
VRRP state on each host, and applied to every gating layer by Keepalived `notify_*` scripts."* The
integration duly generated those scripts, and generated an example `keepalived-cluster.conf` to go
with them.

Keepalived was never installed. Not on `node-a`, not on `node-b`, and — once the ingress moved
to `node-nas` and the tunnel stopped needing a floating `:443` on the tigers — there was no longer a
reason to install it. The VIP had been dead by decision since 2026-08-27.

So the generated scripts sat on disk with nothing to call them. Nothing wrote
`/run/cluster-sync/vrrp-state`. The fileset pull timer read that file once a minute, found no
`BACKUP`, and skipped. Every minute. Indefinitely. The entire failover path was inert and its only
symptom was a log line.

That is AR-0040's shape exactly, which is why this was fixed rather than documented. We had already
lost this project's founding incident to a feature that had never once worked while 305 tests passed
at 93% coverage.

The obvious repair — install Keepalived — was the wrong one. Keepalived has four jobs here: detect
the peer is gone, decide who leads, run three shell scripts, and hold a VIP. The VIP was already
dead. The leadership decision **already existed**: the Valkey lease from ADR-003 is atomic, tested
against a real server, and is this project's split-brain guard. Installing Keepalived would have
added a second election that can disagree with the first, plus a package needing root and
`NET_ADMIN` on both hosts and VRRP traffic on the network, to solve a problem that was already
solved. Its only genuine advantage was that it runs when Home Assistant does not — and that is a
property a systemd timer has too.

## Decision

**A generated host-side program, `cluster-promoter`, takes or renews the Valkey lease every ten
seconds and — only when leadership actually changes — writes the state file and runs the matching
notify script.** It runs on both nodes, symmetrically, under systemd. `keepalived-cluster.conf` is
no longer generated.

Ten seconds against a thirty-second TTL gives three attempts per lease, so one missed poll never
costs a healthy leader its lease.

Three things make it safe to bolt a second lease-holder onto ADR-003:

**It shares the lease script rather than reimplementing it.** `lease.py` holds the Lua, and both
`backend.py` and the promoter evaluate the same string. Two subtly different take-or-renew
implementations racing on one key is how a cluster ends up with two leaders, and we had already lost
a day to an identity collision doing something adjacent to that.

**It acts on transitions, never on state.** Re-running `notify_master.sh` every ten seconds would
stop the container, swap `.storage`, and reapply firewall rules on a timer, forever, on a perfectly
healthy leader. The rule that the integration's `ServiceGate` already follows applies here too.

**It presents this node's own `node_id`,** the same identity the integration uses, so the promoter
and the integration renewing the same lease do not conflict — the second caller simply renews. That
reads oddly until you know they share an identity, which is why it is commented where it happens.

### The liveness condition, and why it is not optional

The first version of this renewed the lease whether or not Home Assistant was running, and that was
wrong in a way worth recording, because the reasoning that produced it was superficially sound.

The design said health checking was out of scope, on the grounds that *"a leader that is wedged but
still renewing keeps the lease — that is the lease's existing semantics, unchanged here."* The
semantics were not unchanged. **The renewer had moved.** Before this ADR, `acquire_leadership` was
called only from inside Home Assistant, so a dead Home Assistant stopped renewing and the lease
lapsed on its own. A systemd timer on the host renews it regardless. The lease had quietly stopped
meaning *this node is serving Home Assistant* and started meaning *this host is powered on*.

Those differ in precisely the most common outage. Demonstrated against a real Valkey with two
promoters, Home Assistant dead on the leader and its host still up: the lease stayed put across
twice the TTL, the standby never promoted, and the leader never demoted. Failover never fired.

So: **before renewing a lease it already holds, the promoter asks Home Assistant's HTTP API whether
it is answering.** Any response counts as alive — `GET /api/` returns 401 without a token, and that
401 is proof the server is up — so the probe needs no credentials at all. Only a refused connection,
a DNS failure or a timeout counts as dead, and on that the promoter releases the lease and demotes.

An HTTP probe rather than `docker inspect` because Home Assistant's web server shares its event loop
with everything else: a blocked loop stops answering while the container still reads as perfectly
healthy. The wedged-but-running leader is the outage this design exists for, and only the probe sees
it.

Two properties keep that from being a cure worse than the disease:

- **The probe gates renewal, never taking.** A standby's Home Assistant is *deliberately* stopped,
  and taking the lease is what starts it. Gating both would leave a cold standby permanently unable
  to promote — worse than the bug being fixed. There is a test aimed squarely at that mistake.
- **A grace window from the moment this node recorded `MASTER`.** `docker start` returns long before
  Home Assistant serves HTTP, and a cold boot routinely outlasts the TTL. Without grace a node
  promotes, fails its own probe while booting, releases the lease it just took, and flaps.

## Consequences

**Failover now depends on Valkey.** It already did for replication; this deepens it to the promotion
decision itself. If Valkey is unreachable the promoter changes nothing and logs — it never infers
leadership from silence. The escape hatch is `touch /run/cluster-sync/force-master`, which claims the
lease unconditionally and records the bypass in `force-master.used` so a later promotion cannot
quietly inherit it. It lives under `/run` on purpose: tmpfs clears on reboot, so a forgotten override
cannot outlive the incident.

**A wedged node with a dead peer will cycle.** Releasing the lease demotes it, which stops Home
Assistant; the next tick finds the lease free and promotes it again. A hold-down after a
probe-triggered release keeps that to a slow retry rather than a tight loop, which matters because
each promotion also swaps `.storage`.

**There is no VIP, and no plan for one.** Clients reach Home Assistant through the ingress on
`node-nas`. Anything that assumed a floating address needs rethinking, not restoring.

**No liveness judgement beyond "is it answering".** An instance returning 500 to every request looks
alive to this probe. That remains a separate question and a separate design.

## Alternatives rejected

| Option | Why not |
|---|---|
| Install Keepalived as ADR-001 intended | A second election that can disagree with the lease, needing root, `NET_ADMIN` and VRRP on the wire, to decide something already decided |
| Make the promoter the sole authority, and have the integration read `vrrp-state` | Conceptually cleaner — exactly one writer per node — but it changes ADR-003 and the code path all failover depends on. This design changes nothing inside the integration, so it cannot regress what the existing suite already proves |
| Have the promoter stand down while Home Assistant is running | "Is HA up" is a slippery question, and a container that is running but wedged is exactly the case failover exists for. That design would sit on its hands through it |
| Check `docker inspect` instead of probing HTTP | Catches a crash, an OOM kill and a `docker stop`, but not a blocked event loop — which is the failure that motivated adding a check at all |

## Verification

Proven in `tools/rehearsal/rehearse.py fileset`, which hard-kills the leader and asserts the standby
promotes **on its own**, with no manual step anywhere in the path. That harness used to drive
promotion by hand, and that is precisely what hid the original defect from a 50-check green run.

---

## Addendum — 2026-09-07: two behaviours found by the first real fail-back

Recorded here rather than as a new ADR: neither changes the decision, and both surprise operators.

### A promoter restart looks like a release

Restarting `cluster-promoter.service` on a node that was leading causes it to record a release and
write the M3 hold-down marker. It then **refuses to retake the lease for `--release-holddown`
(900s)**, which during a hand-driven fail-back means the node you are trying to return to sits at
BACKUP with a free lease and an idle house.

This is the hold-down doing its job — it cannot distinguish "restarted deliberately" from
"released after failing" — but it is not obvious mid-incident.

**`--adopt` is the answer, and it does two useful things at once:** it clears the marker, and it
prints the **real lease holder**. That second half is the fastest way to answer "who leads?"
without reading two journals:

```
$ sudo /etc/cluster-sync/cluster-promoter.sh --adopt
cluster promoter: adopted BACKUP (/run/cluster-sync/vrrp-state); lease held by 'node-a'
```

### The hold-down message asserted a fact it never checked

The branch printed `NOT taking the free lease` while the peer demonstrably held it, because it
fires on `previous != "MASTER"` and the marker alone — there is no holder lookup anywhere in it.
An operator read that as a leaderless cluster and went hunting a split-brain that did not exist.

Fixed to state only what the branch knows, and to point at `--adopt` for the holder. Guarded by
`test_the_holddown_message_never_claims_the_lease_is_free`, verified to fail against the old
wording. The general rule is [ADR-008](./ADR-008-liveness-signals-must-prove-measurement.md) §3:
**a log line may not assert a fact the code did not establish.**

---

## Addendum — 2026-09-08: the probe grace became adaptive

ADR-006 fixed the grace at 600 s, chosen to exceed the slowest cold boot on the slowest node. It
does — and it applied that worst case to *every* failure, which turned out to be one setting with
three symptoms:

- a genuinely wedged Home Assistant cost **ten minutes** before its peer could promote;
- an HA-only failure therefore **could not meet the 2.5-minute budget** ([ADR-001](./ADR-001-active-passive-topology.md));
- and the radios, released by the demote path, arrived **ten minutes late** ([ADR-009](./ADR-009-radio-custody-failure-modes.md)).

**Decision: split the grace in two.** A **base** of 120 s — roughly twice the slowest boot measured
on this fleet (24.0 s and 62.9 s to `Home Assistant initialized`) — and the original 600 s retained
as a **ceiling**, reached only while the container is demonstrably coming back.

Three signals extend, read from `docker inspect` and nothing else:

| Signal | Why it counts |
|---|---|
| `State.Restarting == true` | Docker is actively restarting it. Unambiguous. |
| Exited with code **100** | Home Assistant's own "restart me", after a config change or upgrade. A deliberate restart, not a crash. |
| `StartedAt` within 120 s | It booted recently, so a failed probe means *still starting*. |

**A fourth was deliberately rejected: an increasing restart count.** That is a crash loop, and a
crash loop is precisely when the peer *should* take over.

🚨 **The ceiling is the load-bearing part.** Extension stops at 600 s no matter what the container
reports, so a container that loops forever still demotes on schedule. Without it the adaptive path
would build the one mode this must never have — a failover that silently never happens. Guarded by
`test_a_crash_loop_does_NOT_extend_forever`, verified to fail when the cap is removed.

**This does not weaken D3.** A wedged-but-running Home Assistant reports `Restarting == false` with
an old `StartedAt`, so it demotes on the base grace. ADR-006 rejected `docker inspect` as a
*replacement* for the HTTP probe, which was right; this uses it only to tell "down and coming back"
from "down".

**Where Docker cannot answer — bare metal, Core, no CLI — there is no extension**, which is exactly
the flat pre-adaptive behaviour. The generator emits `--ha-container` empty in that case, and a
promoter that refuses to demote because a CLI is missing would be worse than one that demotes early.

**The deferral is published, not silent.** The promoter logs why it extended and writes the reason
to `/run/cluster-sync/grace-reason` for the operator surface — because "no failover yet" and "no
failover ever" look identical from outside, and the difference is the whole explanation
([ADR-008](./ADR-008-liveness-signals-must-prove-measurement.md) §3).
