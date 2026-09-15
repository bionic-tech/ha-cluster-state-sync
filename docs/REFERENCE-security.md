# Security posture

The v1 adversarial review rated this integration's original security posture
**P0** — not because of one flaw, but because four of them combined: location
and alarm state mirrored by default, in plaintext, into a shared keyspace
protected by one password, then applied verbatim on restore with no integrity
check. Each leg is addressed below.

<details>
<summary><strong>Intermediate / advanced — the full security posture</strong> (click to expand)</summary>

### Cluster secret (required)

Every snapshot entry is signed with HMAC-SHA256 keyed by a per-cluster secret,
and verified on restore. The signature covers the state, the attributes, both
timestamps, the schema version, the writing node **and the entity ID** — so an
entry cannot be edited, cannot claim to come from the peer, and cannot be
replayed onto a different entity.

* The setup form pre-fills a fresh 256-bit secret. **Copy that exact value to
  the other node.** A mismatch means neither node accepts the other's state.
* **With no secret configured, this node refuses to restore anything.** It will
  still publish its own state. Starting cold is a worse failover; obeying a
  forged alarm state is a worse outcome.

### TLS

Off by default, because turning it on silently would break every deployment
whose Valkey has no TLS listener. Turn it on. Certificate verification is
always required when TLS is enabled — there is deliberately no "insecure TLS"
toggle, since TLS without verification just encrypts your traffic to whoever
is on the path.

### Sensitive domains are opt-in

`person`, `device_tracker` and `alarm_control_panel` are **no longer mirrored
by default**. They are exactly the states you most want a promoted standby to
know, and also exactly the states that tell a reader of the shared hash when
the house is empty and unarmed. Add them to `include_domains` deliberately, and
pair them with TLS — the integration logs a warning at startup if you track
them without it.

### Give it its own Redis user

The namespace is an organisational label, **not** a security boundary: anything
holding the credential can read every namespace, and `db=2` is not isolation
either. Provision a dedicated ACL user scoped to this integration's keys:

```
ACL SETUSER ha-cluster-sync on >CHANGE-ME \
    ~ha:cluster_state_sync:* \
    +@read +@write +@keyspace -@dangerous
```

That user can read and write the integration's own keys and nothing else — so a
leaked credential does not become the run of your Valkey.

### What is *not* protected

* **The password and cluster secret are stored in plaintext, in a
  world-readable file.** Home Assistant writes config entries to `.storage/` as
  unencrypted JSON, and it writes them **mode 0644** — verified on this fleet.
  So the honest statement is not "anyone who can read `config/`"; it is
  **anyone with a login on the host at all**. The setup form masks those fields,
  which stops shoulder-surfing and nothing more.

  Restricting the permissions on `config/` helps only if the directory itself
  denies traversal, and Home Assistant will keep rewriting the file 0644. Treat
  a host account as a compromise of the cluster secret, and treat a host
  compromise as a compromise of **both** nodes — the secret is the same on each,
  by definition.

  **And read that in the light of what a cluster does to `config/`.** If you
  replicate it to the standby — which the tier-1 model in ADR-001 does, and
  which is how a cold standby comes up with your dashboards intact — then both
  secrets now exist on two hosts. So **treat them as present on every node**,
  and give the integration its own Valkey ACL user, which is the mitigation that
  actually bounds a leak.

  Home Assistant's own backups are a separate question with a better answer:
  they are **encrypted**, so a backup on its own does not give up the cluster
  secret. But the passphrase has to be stored for unattended backups to work, and
  it lives in `.storage/backup` — beside the thing it encrypts. So the rule is to
  keep backups and `.storage` in *different places*: any destination that holds
  both has the ciphertext and the key.
  Full treatment in the security architecture, §3.1.
* **Leader election.** Both nodes still run automations during the overlap
  window. See the known limitations.
* **Tombstones.** A deleted entity lingers in the shared hash until overwritten.

</details>

---

This page is about how the integration protects what it replicates.
[SECURITY.md](../SECURITY.md) is a different document: how to report a
vulnerability in it.

Moved here from the README on 2026-09-12.
