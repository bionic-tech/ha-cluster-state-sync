# ADR-005: Generate host configuration, do not control the host

**Status:** Accepted
**Date:** 2026-08-06
**Deciders:** mmanning (project owner)

## Context

[ADR-001](./ADR-001-active-passive-topology.md) set an explicit usability goal:
*"the operator answers a few questions and the integration generates the
host-side gating bundle for them"* rather than documenting Keepalived, nftables
and rsync and leaving them to hand-write it.

Four of ADR-001's gating layers need host-level action — swapping nftables
rulesets, starting and stopping the Home Assistant container, running rsync as
root. The assumption at the time was that Home Assistant runs as an
**unprivileged container**, so it could do none of those things, and arranging
for it to be able to would mean granting it `NET_ADMIN` and a Docker socket:
handing the integration the ability to reconfigure the host's firewall and
control every container on the box.

> **Correction (2026-08-15).** That premise is false for this deployment. Both
> `node-a` and `node-b` run Home Assistant with `privileged: true` and
> `cap_add: [NET_ADMIN, NET_BIND_SERVICE, SYS_ADMIN]`
> (READINESS §4.5
> F2). The container therefore *could* manipulate nftables today.
>
> This does not reverse the decision — it removes one of its arguments and
> leaves the rest, which are the load-bearing ones: an integration that rewrites
> the host firewall is a far larger attack surface than the problem it solves,
> the operator should read the rules before they run, and the generator has to
> work without access to the target host regardless. What it does change is the
> framing: the integration is unprivileged **by design here, not by
> circumstance**, and if the privilege were ever the deciding factor the honest
> move would be to drop `privileged: true` rather than to rely on it.

A second constraint emerged during implementation and turned out to matter more
than expected. The correct firewall filter point depends on how Home Assistant
is networked (host / macvlan / bridge), and for this deployment **that was not
known at design time**. Nor were the hosts reachable from the development
machine, so nothing generated could be tested against reality.

## Decision

**The integration generates host-side artifacts and writes them to the config
directory. The operator installs them. It never applies them itself, and it is
explicit about what has and has not been validated.**

### The split

| Layer | Who acts | Why |
|---|---|---|
| 1 — nftables ruleset | Operator, via the generated promoter | Needs `NET_ADMIN` on the host |
| 2 — flush loop | **Integration** | Inside its own process ([ADR-003](./ADR-003-leadership-resolution.md)) |
| 3 — recorder writes | Integration (not yet built) | Inside its own process |
| 4 — automations | Integration (not yet built) | Inside its own process |
| Container start/stop | Operator, via the generated promoter | Needs the Docker socket |
| Config replication | Operator, via systemd | Needs root and the host filesystem |

> **Clarified (2026-09-01) by [ADR-006](./ADR-006-lease-promoter.md).** "Via Keepalived" in the two
> rows above is now "via `cluster-promoter`", a program this integration generates. That is a
> sharper test of this ADR than Keepalived was, so it is worth being explicit: the promoter is
> **emitted text, installed by the operator, and run by the operator's systemd**. The integration
> writes it and never executes it, never installs it, and cannot start it. Nothing in
> `custom_components/` imports `scripts/`, and there is no `subprocess` anywhere in the integration.
>
> The promoter itself does exec `notify_master.sh` and shell out to `docker` — on the host, as the
> operator's own unit. That is the line this ADR draws, and it lands on the same side of it as the
> notify scripts always did.

The integration governs what it already has authority over, and emits scripts
for everything else.

### Generate for every unknown, rather than guessing one

Where the Docker network mode is unknown, **all three variants are generated**
and the notify scripts call a dispatcher carrying a single `NETWORK_MODE=`
setting.

Guessing wrong here does not fail loudly. It produces rules that load cleanly
and match nothing — a follower that believes it is firewalled and is not. That
is the worst available outcome, because it looks like success.

### State the validation status on the artifact itself

Every generated ruleset carries an `UNVALIDATED` header saying it was produced
without access to the machine it targets, and the bundle ships
`nft-safety-revert.sh` — snapshot the ruleset, arm a timed restore, load,
confirm you still have a shell, cancel.

This is not boilerplate caution. Output that looks machine-generated reads as
tested. These hosts run other services and a wrong ruleset can lock out SSH, so
the artifact has to carry its own provenance.

### Regeneration prunes what it previously wrote

`write_bundle` removes artifacts from a prior configuration — and only its own,
never files the operator put there. Switching warm → cold otherwise leaves
`follower-host.nft` on disk while `INSTALL.md` says the cold model needs no
firewall, and a stale ruleset that still loads is exactly the sort of thing that
gets loaded by mistake.

## Alternatives Considered

| Option | Pros | Cons |
|---|---|---|
| **Generate; operator installs (chosen)** | No privilege escalation; operator reviews before anything runs; works from an unprivileged container | Two-step install; generated config can drift from the entry that produced it |
| Integration applies rules directly | One step, no drift | Requires `NET_ADMIN` + Docker socket. A Home Assistant integration that can rewrite the host firewall and stop arbitrary containers is a far larger attack surface than the problem it solves. |
| A privileged sidecar container | Integration stays unprivileged; automation preserved | Moves the privilege rather than removing it, and adds a component to deploy, version and secure |
| Document only, generate nothing | Zero blast radius | This is what ADR-001 explicitly rejected. Hand-writing Keepalived and nftables is where operators make the mistakes. |
| Generate only the mode the operator selects | Less output | Requires the operator to know the answer at setup time. They did not, and a wrong guess fails silently. |

## Consequences

### Positive

- No privilege escalation. The integration's blast radius stays inside its container.
- The operator reads the rules before anything touches the firewall.
- Works unchanged whether or not the developer can reach the target hosts.
- Provenance travels with the artifact rather than living only in a README
  nobody opens at 3am.

### Negative

- **Two-step install**, and the bundle can drift from the config entry if the
  operator edits the generated files. Nothing detects that.
- The integration cannot verify its own gating is in effect. Layer 1 could be
  absent entirely and the integration would not know.
- Warm-standby correctness depends on the operator completing steps the
  integration cannot check.
- Generated firewall rules remain untested against the real hosts until deployed.

### Risks

- **Silent no-op rules.** The `all` dispatcher refuses to run with
  `NETWORK_MODE` unset, but a *wrongly* set value produces rules that load and
  match nothing. Mitigated only by the `INSTALL.md` instruction to check that
  counters move.
- **Stale bundle after reconfiguration.** Regeneration prunes, but only when the
  operator re-runs the flow — and only in the config directory, not in
  `/etc/cluster-sync/` where the live copy lives.
- **Operator skips the safety script.** The most likely cause of a lockout, and
  outside the integration's control.

## References

- Related: [ADR-001](./ADR-001-active-passive-topology.md) (the wizard requirement and the four gating layers), [ADR-003](./ADR-003-leadership-resolution.md) (the layer the integration does control)
- Implementation: `cluster_state_sync/bundle.py`, `config_flow.py` (steps 2, 4, 5)
- Tests: `tests/test_bundle.py`, `tests/test_wizard.py`
- Validation status: Readiness assessment

## Compliance Cross-References

- **SDLC Framework** Phase 2 (Architecture Review Gate), Phase 4 (Operations).
- Delivers ADR-001 §4 wizard steps 2, 4 and 5.
