# ADR-003: Leadership resolution — three signals, all failing closed

**Status:** Accepted
**Date:** 2026-08-05
**Deciders:** mmanning (project owner)

## Context

[ADR-001](./ADR-001-active-passive-topology.md) makes leadership the single
source of truth for four gating layers — nftables, this integration's flush
loop, recorder writes, and automations — but left *how a node learns it is
leader* unresolved. It named three candidates and deferred the choice.

Meanwhile v0.1 had no leadership concept at all. Both nodes ran the flush loop
unconditionally (AR-0017),
which combined with the delete-then-write flush meant each node's flush erased
the peer's entries and the shared hash oscillated between two partial views.

Removing the `DEL` ([ADR-004](./ADR-004-snapshot-write-semantics.md)) made
concurrent writes *merge* rather than erase, which is damage control rather than
correctness: **the follower's view of the world is by definition the stale one,
and merging it into the leader's is still wrong.**

ADR-001 also flags a harder problem the flush loop cannot solve on its own:
**VRRP alone can dual-master under a network partition.** Both nodes can
genuinely believe they hold the VIP.

## Decision

**Gate the flush on an explicit leadership check, resolved from one of three
configurable signals. Every resolution path fails closed.**

| Signal | Resolution | Intended for |
|---|---|---|
| `always` | Always leader | Single node, or a **cold standby** whose Home Assistant is not running anyway |
| `entity` | State of a configured HA entity is truthy | **Warm standby** with Keepalived — `notify_master` / `notify_backup` toggle an `input_boolean` |
| `lease` | This node holds a TTL lease in Valkey | Warm standby, strongest option |

### `always` is the default

Deliberately. Silently stopping every existing single-node install from writing
would be a far worse regression than the dual-writer race the other modes guard
against. Operators opt in to gating when their topology needs it.

### The lease is atomic, and is the split-brain guard

Take-or-renew is a single Lua script evaluated inside Valkey:

```lua
local current = redis.call('GET', KEYS[1])
if current == false then
    redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2]); return 1
elseif current == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2]); return 1
end
return 0
```

A read-then-write in Python would reintroduce exactly the race the lease exists
to close — both nodes reading "unheld" in the same instant. Renewal is
conditional on already holding it, so a follower cannot steal leadership from a
live leader, and a dead leader's lease simply expires.

This is what makes the lease the answer to ADR-001's split-brain risk. VRRP can
dual-master; the lease cannot. A node that does not hold it does not write,
whatever VRRP believes.

> **Extended (2026-09-01) by [ADR-006](./ADR-006-lease-promoter.md).** There is no VRRP any more,
> so the lease is not merely the tiebreak — it is the election. Two consequences for this ADR:
>
> A second process now holds the same lease. `cluster-promoter` runs on the host and takes or
> renews under **this node's own `node_id`**, which is safe precisely because renewal is keyed on
> identity: the integration and the promoter present the same id, so the second caller renews
> rather than conflicts. Both evaluate the same Lua from `lease.py`; neither has its own copy.
>
> A lease can now be **released** as well as expire. If Home Assistant stops answering its own HTTP
> API, the promoter deletes the key — identity-checked, so it can only ever delete its own claim —
> and demotes. Before ADR-006 a dead Home Assistant stopped renewing and the lease lapsed by
> itself; once a host timer took over the renewing, that stopped being true and had to be made
> true again deliberately.
>
> The `entity` signal is unchanged in mechanism and stale in wording. Its row in the table above,
> and the consequence noted near the end of this ADR, both say "Keepalived scripts". Those scripts
> are the same `notify_master` / `notify_backup` they always were, still toggling an
> `input_boolean` from outside the container — what changed is what runs them. Read both mentions
> as "whatever drives your notify scripts", which on this fleet is now `cluster-promoter` and
> elsewhere may well still be Keepalived. The signal is deliberately agnostic about that, which is
> the whole reason it exists.

### Fail closed, everywhere

| Condition | Resolution |
|---|---|
| Configured entity does not exist | Follower |
| Entity configured but ID blank | Follower |
| Backend unreachable when checking the lease | Follower |
| Unknown signal value | Follower |

Assuming leadership when it cannot be established is precisely how you end up
with two leaders. Each of these has its own regression test.

### Lease handback

On clean shutdown *and on config-entry unload*, the leader deletes its own lease
— and only its own, never one the peer has since taken. Without handback the
standby waits out the full TTL before promoting, burning failover budget on the
one kind of shutdown that was entirely orderly.

## Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| **Three configurable signals (chosen)** | Fits cold and warm; no forced regression; lease available where it matters | Three code paths to test; operator must choose |
| Valkey lease only | One mechanism, strongest guarantee | Makes Valkey a hard dependency for *writing at all*; a single-node install breaks when Redis blinks |
| Sentinel-based election | Reuses the existing quorum | Sentinel elects a Redis master, not an application leader. Conflating them means a Redis failover silently reassigns HA leadership. |
| External consensus (etcd / Consul) | Purpose-built, battle-tested | A third clustering system for a two-node home deployment. ADR-001 already rejected heavyweight clustering. |
| Trust VRRP alone | Nothing to build; Keepalived already decides | The dual-master case. VRRP is a *VIP* protocol, not a consensus protocol. |
| Keepalived state file watched from the container | No extra infrastructure | Needs a host path mounted into the container; the `entity` signal achieves the same with no mount |

## Consequences

### Positive

- The follower does not write. AR-0017 closed rather than mitigated.
- ADR-001's split-brain risk has a real answer for warm deployments.
- Leadership is resolved in one place, so adding layers 3 and 4 (recorder,
  automations) means consuming an existing signal, not inventing another.
- Existing installs are unaffected until they opt in.

### Negative

- The default (`always`) is the least safe option. A warm deployment that
  forgets to change it has no gating at all — mitigated by documentation and by
  the wizard surfacing the choice, not by the code.
- Three signals is three paths to test and support.
- The `entity` signal depends on Keepalived scripts reaching into the container
  to flip a Home Assistant entity, which is more moving parts than it looks.
- The lease adds a Valkey round-trip per flush interval.

### Risks

- **Lease TTL vs snapshot interval.** The TTL (30s) must comfortably exceed the
  renewal cadence (the snapshot interval, default 5s) or a healthy leader drops
  its own lease between flushes. Changing the interval to something long enough
  to matter would break this; no code currently prevents it.
- **Clock-independent, but not partition-independent.** If a leader is isolated
  from Valkey but still serving, it stops writing — correct, but it also stops
  publishing state the standby may need. Degraded either way; the diagnostics
  make it visible.
- **`always` in a warm pair** is the realistic misconfiguration. Covered in the
  incident-response runbook §2.3.

## References

- Related: [ADR-001](./ADR-001-active-passive-topology.md) (leadership as single source of truth), [ADR-004](./ADR-004-snapshot-write-semantics.md) (the interim merge guard)
- Findings: AR-0017 (no leader election), AR-0001 (dual-writer thrash)
- Implementation: `cluster_state_sync/leadership.py`, `backend.py` (`acquire_leadership`, `release_leadership`)
- Tests: `tests/test_leadership.py`

## Compliance Cross-References

- **SDLC Framework** Phase 2 (Architecture Review Gate).
- Realises ADR-001's corruption-safe warm-standby model; resolves review conflict C4.
