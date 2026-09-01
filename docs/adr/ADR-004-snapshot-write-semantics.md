# ADR-004: Snapshot write semantics — full authoritative map, merge not replace

**Status:** Accepted
**Date:** 2026-08-05
**Deciders:** mmanning (project owner)

## Context

v0.1's flush was:

```python
pipe.delete(states_key(namespace))
pipe.hset(states_key(namespace), mapping=batch)
```

where `batch` was the drained `pending` buffer — the entities that had fired a
state-changed event in the preceding five seconds.

Two consequences, both fatal to the product's stated purpose:

1. **The snapshot was a five-second delta, not full state** (AR-0001). Whatever
   the standby read back was whatever happened to move just before it read.
2. **Nothing ever captured initial state** (AR-0002). An entity that never
   changed after boot — the holiday-mode flag, the alarm's armed state — was
   never in the snapshot at all.

Eight of fifteen review personas found this independently. It is the reason the
review concluded the integration "does not do what it claims": a failover would
report success while the standby restored almost nothing.

The `DEL` had a third effect nobody intended. With both nodes running the flush
loop, each node's flush wiped the peer's entries, so the hash oscillated between
two partial views (AR-0017).

## Decision

**Maintain a full authoritative in-memory map of every tracked entity, seeded
at setup, and write it whole on every flush with `HSET` and no preceding
`DEL`.**

### The authoritative map

`StateMirror` holds `dict[entity_id, SnapshotEntry]`, seeded at setup from
`hass.states.async_all()` filtered through `_should_track`, and updated on every
tracked state change.

This dissolves AR-0015 (unbounded buffer) rather than patching it: the map is
keyed by `entity_id` and bounded by the number of tracked entities, so no amount
of churn can grow it. There is no longer a buffer to cap.

### No `DEL` — merge, not replace

`HSET` alone merges. That makes the write **last-writer-wins per field**, so two
nodes flushing concurrently degrade freshness rather than destroying each
other's data.

This was the interim guard until leadership gating landed
([ADR-003](./ADR-003-leadership-resolution.md)). It remains in place, because
the default leadership signal is still `always` and a misconfigured warm pair
must degrade rather than corrupt.

### Revision tracking, not a dirty flag

```
revision           bumped on every recorded change
flushed_revision   the revision last successfully written
```

A monotonic counter rather than a boolean, because a change arriving *while a
write is in flight* must not be mistaken for persisted. On success only the
revision actually written is marked clean; on failure nothing is, so the next
interval retries (AR-0011). A backend outage degrades freshness; it never
silently discards changes.

An unchanged map skips the write entirely, so an idle cluster does not rewrite
an identical snapshot every five seconds.

### Consequence accepted: no tombstones

Because nothing deletes, an entity removed in Home Assistant lingers in the hash
until overwritten or the namespace is bumped. This is a deliberate trade, not an
oversight — see Consequences.

### Schema versioning

Every entry carries `"v"`. Landing this with the rewrite meant the AR-0005
signature field could later be a version bump the reader already understood,
rather than a second migration of the same payload. Entries without a version
read as v1 (so a rolling upgrade does not force a cold start); entries from a
newer schema are refused per entry.

## Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| **Full map, `HSET`, no `DEL` (chosen)** | Correct; multi-writer-safe; simple to reason about | Rewrites unchanged fields; no deletion propagates |
| `DEL` + full map | Deletions propagate for free | Destroys the peer's entries on every flush — the dual-writer failure, just with correct single-node behaviour. Also a window where the hash is empty. |
| Deltas + explicit tombstones | Minimal write volume; deletions propagate | Needs tombstone lifecycle and expiry; a missed tombstone is a resurrected entity. Complexity disproportionate to the entity counts involved. |
| Per-key TTL, refreshed on flush | Self-cleaning; deletions expire naturally | TTL must exceed the flush interval by a safe margin, so a paused leader silently loses the whole snapshot. Trades a visible problem for an invisible one. |
| Full replace inside a `MULTI` | Atomic, deletions propagate | Still single-writer-only. Serialises the whole snapshot per flush. |

## Consequences

### Positive

- The integration does what the README claims. AR-0001 and AR-0002 closed.
- Concurrent writers merge instead of erasing each other — the interim guard for
  AR-0017, retained.
- No unbounded buffer, by construction.
- Failed flushes retry instead of dropping data.
- Idle clusters generate no write traffic.

### Negative

- **No tombstones.** A deleted entity lingers. Mitigated by the restore's
  max-age guard: a stale entry degrades to noise rather than misinformation,
  because it cannot pass the age check indefinitely. Bump the namespace to
  force a clean slate.
- Each flush writes the full map, not a delta. Fine at hundreds of entities
  (~80KB); would need rethinking at tens of thousands.
- The map is per-node memory proportional to tracked entities.

### Risks

- **A node with a *smaller* tracked set does not shrink the hash.** If the
  include filter is narrowed, the previously-tracked entries remain until
  overwritten or aged out. Same mitigation as tombstones.
- **Scale.** The sizing assumption is a home with hundreds of tracked entities.
  This decision should be revisited before anyone points it at a commercial
  installation.

## References

- Related: [ADR-002](./ADR-002-snapshot-integrity.md) (what the entries carry), [ADR-003](./ADR-003-leadership-resolution.md) (who is allowed to write)
- Findings: AR-0001, AR-0002, AR-0011, AR-0015, AR-0016, AR-0026; interim guard for AR-0017
- Implementation: `cluster_state_sync/__init__.py` (`StateMirror`), `backend.py` (`write_snapshot`)
- Tests: `tests/test_snapshot.py`, `tests/test_backend.py`

## Compliance Cross-References

- **SDLC Framework** Phase 2 (Architecture Review Gate).
- Resolves review conflicts C1 (restore bounds) and C4 (multi-writer safety in the interim).
