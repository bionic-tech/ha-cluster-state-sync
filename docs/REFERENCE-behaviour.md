# Reference — how it behaves

What the integration does at runtime, in the detail you need when something
surprises you. The README says what it is; this says what it does.

For symptoms rather than mechanisms, start at
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). For the traps that cost this project
real outages, [GOTCHAS.md](GOTCHAS.md).

---

## Sizing & performance

* Each state takes roughly 200–500 bytes serialised.
* A typical home with 200 tracked entities → ~80KB snapshot.
* Write cost: one Redis `HSET` + `SET` per flush, and the flush is skipped
  entirely when nothing changed since the last one. Even on a Pi with a few
  hundred entities, this is sub-10ms.
* There is deliberately **no** `DEL` before the `HSET`. The write merges, so
  two nodes flushing concurrently cannot erase each other's entries — the
  interim guard until leader election lands.
* Read cost (restore): one `HGETALL` + `GET`. Single round-trip.

## What gets restored, what doesn't

On startup, after backend connection but before the automation engine
starts, the integration:

1. Reads the full snapshot.
2. Refuses the whole thing if it holds more entries than the cap — a
   legitimate snapshot is hundreds of entries, not thousands.
3. For each entity, checks: is it in our include filter? Was the snapshot
   written by *another* node? Is its timestamp readable, and not stamped
   implausibly far in the future? Is it within the max-age window? Are its
   attributes within the size budget? Is it newer than the local state?
4. If yes to all, calls `hass.states.async_set()` to seed it — with a single
   shared context, so the whole restore is one identifiable operation rather
   than a scatter of unattributable state changes.

A bad entry costs that entry and nothing else: an unreadable timestamp, an
oversized payload or a corrupt stored value is logged and skipped, never
allowed to abort the restore and leave the node cold.

**Restore only happens at startup.** If you add the integration to an
already-running Home Assistant, it deliberately does *not* restore — seeding
dozens of entities into a live system would fire every automation watching
them. The snapshot is applied on the next restart instead.

**At boot, the peer's snapshot wins.** This used to say the opposite — that
integrations restoring their own state via `RestoreEntity` take priority, and we
only fill gaps — and that turned out to mean the restore did nothing at all.

Every domain in the default list is a `RestoreEntity`. Home Assistant replays
their values from *this* node's disk at boot and stamps `last_updated` with the
boot time, so local state was always "newer" than any snapshot and every entry
was skipped. On a real two-node test an 18-second-old snapshot restored **zero**
entities. The comparison had no information in it.

So at boot the snapshot is the authority for tracked entities, still bounded by
max-age, the signature, the size cap and the include filter. A device-backed
entity that genuinely polled fresher state corrects itself on its next poll; a
helper never does. Off the boot path — a manual restore against a running
system — local state wins again, because there the timestamps mean something.


## Leadership — who writes

Only the leader writes to the shared snapshot. Choose the signal at setup:

| Signal | Use when | How it works |
|---|---|---|
| **Always leader** (default) | Single node, or a **cold standby** whose Home Assistant is stopped until promotion | This node always writes. The pre-v0.2 behaviour. |
| **Follow an entity** | **Warm standby**, or an external promoter you already trust | Point it at an `input_boolean` your `notify_master` / `notify_backup` scripts toggle. |
| **Valkey lease** | Warm standby, strongest option | The node takes a TTL lease in Valkey itself, renewed on every flush. |

The lease is the **split-brain guard** ADR-001 depends on, and since the promoter
replaced Keepalived it is the election as well. Take-or-renew is evaluated
atomically inside Valkey rather than as a read-then-write race, so two nodes
asking at the same instant get different answers. A heartbeat on the wire cannot
promise that: under a partition both halves can reasonably conclude they are the
survivor.

Every path **fails closed**. A missing entity, a typo'd entity ID, an
unreachable backend — all resolve to *follower*. Assuming leadership when you
cannot establish it is exactly how you end up with two leaders.

On a clean shutdown the leader hands its lease back, so the standby promotes
immediately instead of waiting out the TTL.

> This gates layer 2 of ADR-001's four. Layer 1 (nftables), layer 3 (recorder)
> and layer 4 (automations) are host-side or not yet implemented — see
> [ADR-001](adr/ADR-001-active-passive-topology.md).


## Actions

Two, and deliberately no more.

| Action | What it does |
|---|---|
| `cluster_state_sync.flush_snapshot` | Writes this node's entity state to the shared store immediately, rather than waiting for the next interval. **Refuses on a node that does not hold cluster leadership** — a follower writing would overwrite the leader's snapshot. Does nothing if nothing has changed since the last flush. |
| `cluster_state_sync.clear_degraded` | Acknowledges the "go-bag was stale or missing at promotion" marker and removes the repair issue it raised. It fixes nothing on its own: the next promotion is what proves the go-bag is healthy, and the marker returns if the condition persists. |

There is **no force-promote action**, on purpose. Forcing a promotion bypasses
the split-brain guard, so it stays a file you have to be on the host to create —
`touch /run/cluster-sync/force-master` — and it records the bypass in
`force-master.used`. A button for it would make "both nodes believe they lead"
something you could cause by accident from a phone.

Both are also wired to buttons on the generated dashboard
(`cluster-dashboard.yaml` in the bundle).

> **The promoter ships inside the integration, and is inert there.**
> `scripts/cluster_promoter.py`, `scripts/resp.py` and `lease.py` live in the
> package you copy into `custom_components/`, which reads as though installing
> the integration installs the promoter. It does not. `bundle.py` reads those
> files with `.read_text()` and emits them as *text* for the operator to install
> deliberately (ADR-005). The only `import subprocess` in the package is inside
> scripts the runtime modules never import, so nothing in a running Home
> Assistant can execute them. Verified independently by homelab, 2026-09-02.

