# ADR-002: Snapshot integrity via a per-cluster shared secret

**Status:** Accepted
**Date:** 2026-08-05
**Deciders:** mmanning (project owner)

## Context

The restore path takes data out of a shared Valkey hash and applies it directly
to Home Assistant's state machine via `hass.states.async_set()`. That state
machine drives physical actuators. **The restore path is therefore a remote-write
primitive for the house, not a cache.**

In v0.1 it had no integrity checking at all. The v1 adversarial review
(AR-0005) rated the
combination P0 across five personas, because three things compounded:

1. Entries were applied verbatim — whatever was in the hash became local state.
2. The only trust check was `entry.source_node == node_id`, and `source_node`
   was an ordinary field an attacker could set to anything.
3. The transport was plaintext, into a keyspace protected by one shared
   password with no ACL, on a flat LAN carrying IoT devices.

The concrete attack is not subtle: read the hash to learn the house is empty,
write `alarm_control_panel.house = "disarmed"` claiming to be the peer, wait for
the standby to be promoted.

A related question had no answer either: **what should a node do when it cannot
establish where a snapshot came from?**

## Decision

**Sign every snapshot entry with HMAC-SHA256 keyed by a per-cluster shared
secret, and refuse to restore anything that does not verify. With no secret
configured, refuse to restore at all.**

### What is signed

The canonical JSON form of `{entity_id, schema_version, state, attributes,
last_changed, last_updated, source_node}`, with `sort_keys=True` so the form is
stable across nodes.

Two inclusions are load-bearing and easy to omit:

| Field | Why it must be signed |
|---|---|
| `source_node` | The restore trusts entries "from the peer". Unsigned, that field is just a string the attacker sets, and the check is decorative. |
| `entity_id` | It is the hash *field name*, not part of the stored value. Unsigned, a validly-signed `input_boolean.holiday_mode = "on"` can be copied onto the `alarm_control_panel.house` field and verifies perfectly — a good signature on entirely the wrong entity. |

### Order of operations

Verification runs **before** the payload is structurally interpreted. Nothing
parses fields out of bytes whose origin has not been established. This is not
theoretical tidiness: writing the check second surfaced a real crash, where a
non-integer schema version raised `TypeError` in the version comparison before
any authenticity check ran.

### Fail-closed on a missing secret

If no cluster secret is configured, the node **publishes its own state but
refuses to restore anything**, and logs an error saying so.

Starting cold is a worse failover. Obeying a forged alarm state is a worse
*outcome*. A snapshot that cannot be authenticated is not a degraded snapshot —
it is input from an unknown writer, fed to a remote-write primitive.

### Key management

- Generated at setup with `secrets.token_urlsafe(32)` (256 bits) and pre-filled
  in the form, so the operator's path of least resistance is a strong random key
  rather than a memorable one.
- The operator copies the identical value to the peer. A mismatch means neither
  node accepts the other's state — noisy and diagnosable, rather than silent.
- **No rotation mechanism.** See Consequences.

## Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| **HMAC with a shared secret (chosen)** | Symmetric, no PKI, cheap, both nodes are equally trusted anyway | Shared secret is a single compromise point; no per-node attribution |
| TLS only | No key distribution beyond certs | Protects the wire, not the store. Anyone with Valkey write access still owns the state machine. Solves a different problem. |
| Redis ACL only | Server-side, no client changes | Same objection: authorises *access*, does not authenticate *content*. Compromise any authorised writer and you are back to square one. |
| Asymmetric signatures (Ed25519) | Per-node identity; a compromised follower cannot forge the leader | Key distribution and rotation for a two-node home cluster is disproportionate. Revisit if the cluster ever exceeds two nodes. |
| Trust the network (status quo) | Nothing to build | The finding. A flat home LAN with IoT devices is not a trust boundary. |

## Consequences

### Positive

- The `source_node` peer-trust check becomes meaningful instead of decorative.
- Two clusters can share one Valkey without being able to write into each other.
- Tampering with state, attributes, or timestamps is detected, not applied.
- The failure mode on a wrong key is loud and specific, not silent corruption.

### Negative

- **The secret is stored in plaintext.** Home Assistant writes config entries to
  `.storage/` as unencrypted JSON, so anyone who can read `config/` has it. The
  field is masked in the UI, which stops shoulder-surfing and nothing else. This
  is documented in both READMEs rather than implied away.
- **No rotation.** Rotating means editing both nodes and accepting that neither
  restores from the other until both are updated. Acceptable for two nodes;
  would not be for more.
- Both nodes share one identity. A compromised follower can forge the leader.
- Signing costs a hash per entry per flush — negligible at hundreds of entities,
  worth remembering at tens of thousands.

### Risks

- **Operator sets different secrets on each node.** Presents as "failover
  restored nothing"; covered in the incident-response runbook §2.4.
- **Secret leaks via a config backup.** Config backups now contain a credential
  that grants write access to the house's state. Treat `config/` backups as
  secret material.
- **False confidence.** Signing authenticates the *writer*, not the *content*. A
  compromised leader signs bad state perfectly. The restore bounds
  ([ADR-004](./ADR-004-snapshot-write-semantics.md), AR-0009/0035) exist because
  authenticity is not sufficiency.

## References

- Supersedes nothing. Related: [ADR-001](./ADR-001-active-passive-topology.md), [ADR-004](./ADR-004-snapshot-write-semantics.md)
- Findings: AR-0005 (integrity), AR-0003 (TLS), AR-0004 (sensitive domains), AR-0008 (ACL), AR-0035 (timestamp guard)
- Design docs: 03 Security Architecture, 22 STRIDE
- Implementation: `cluster_state_sync/backend.py` (`_sign`, `SnapshotEntry.from_json`)
- Tests: `tests/test_security.py`

## Compliance Cross-References

- **SDLC Framework** Phase 2 (Architecture Review Gate), Phase 3 (Pre-Release Security Gate).
- Closes the P0 combination recorded in the internal closure record.
