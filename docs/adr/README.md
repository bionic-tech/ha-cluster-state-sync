# Architecture Decision Records (ADRs)

**Project:** Cluster State Sync (active-passive Home Assistant)
**Created:** 2026-05-27

---

## Purpose

Architecture Decision Records capture the context, decision, and consequences of significant architectural choices. They are immutable once accepted — if a decision changes, a new ADR supersedes the old one.

## ADR Index

| ADR | Title | Status | Date |
|---|---|---|---|
| [ADR-001](./ADR-001-active-passive-topology.md) | Active-passive topology: selectable cold / warm standby with a built-in wizard | ✅ Accepted | 2026-05-27 |
| [ADR-002](./ADR-002-snapshot-integrity.md) | Snapshot integrity via a per-cluster shared secret | ✅ Accepted | 2026-08-05 |
| [ADR-003](./ADR-003-leadership-resolution.md) | Leadership resolution — three signals, all failing closed | ✅ Accepted | 2026-08-05 |
| [ADR-004](./ADR-004-snapshot-write-semantics.md) | Snapshot write semantics — full authoritative map, merge not replace | ✅ Accepted | 2026-08-05 |
| [ADR-005](./ADR-005-generate-not-control.md) | Generate host configuration, do not control the host | ✅ Accepted | 2026-08-06 |
| [ADR-006](./ADR-006-lease-promoter.md) | The lease promoter replaces Keepalived | ✅ Accepted | 2026-09-01 |
| [ADR-007](./ADR-007-operator-surface.md) | The operator surface — a self-registering panel, and controls as flag files | ✅ Accepted | 2026-09-07 |

## Status Key

| Status | Meaning |
|---|---|
| ✅ Accepted | Decision made and in effect |
| 🔴 Pending | Decision required — blocking |
| 🟡 Pending | Decision required — non-blocking |
| ⚠️ Deprecated | No longer relevant |
| 🔄 Superseded | Replaced by a newer ADR |

## File Structure

```
docs/adr/
├── README.md              ← THIS FILE (index)
├── ADR-001-active-passive-topology.md
├── ADR-002-snapshot-integrity.md
├── ADR-003-leadership-resolution.md
├── ADR-004-snapshot-write-semantics.md
└── ADR-005-generate-not-control.md
```

## Reading order

ADR-001 sets the topology; the rest fill in decisions it deferred or that the
adversarial review forced.

- **New to the project?** ADR-001, then ADR-004 (what the snapshot *is*), then
  ADR-002 (how it is trusted).
- **Reviewing security?** ADR-002, then 22 STRIDE.
- **Deploying?** ADR-001 for the model, ADR-003 for the leadership signal,
  ADR-005 for what the wizard will and will not do for you.
