# Runbook — bringing up a standby from a brand-new Home Assistant

**For:** a second node (here `node-b`) that has just been rebuilt and is
sitting on Home Assistant's **own** onboarding wizard, having never been set up.

**Not for:** adding the integration to an established instance, and not the
general case. For requirements — which Home Assistant installation types this
works on at all — and for the general build order, read
[RUNBOOK-installation.md](RUNBOOK-installation.md) first. This document is the
fleet-specific walk-through with real hostnames.

> **Read this first, because it changes how much care Phase A deserves.**
> Almost everything you create during Home Assistant's onboarding on the standby
> is **temporary scaffolding**. The go-bag replicates `.storage`, and that
> includes `auth`, `auth_provider.homeassistant` and `onboarding` — so on the
> first real promotion, the standby's users, passwords and refresh tokens are
> *replaced by the primary's*. After that you log into this node with your
> **primary's** credentials, not the ones you are about to create.
>
> That is the entire point of the feature — a promoted standby is you, not a
> stranger asking everyone to log in again — but it is not something you would
> guess from the wizard in front of you.

---

## Order at a glance

| Phase | What | Gate before starting it |
|---|---|---|
| **A** | Home Assistant onboarding on the standby | — |
| **B** | Copy the integration onto the standby | A complete |
| **C** | Configure it, in a **scratch namespace** | B complete |
| **D** | Verify the plumbing | C complete |
| **E** | Back up the **primary**, off-host | D green |
| **F** | Install and configure on the **primary** | E verified |
| **G** | Switch both to the Valkey **lease** | F stable, both agreeing |
| **H** | Enable the **go-bag** on the primary only | G stable |
| **I** | Install the **host bundle** on both | H proven, primary's RAID healthy |

**The one that can hurt you is H.** The go-bag flows **leader → standby and
replaces `.storage`**. Enable it while leadership is wrong and you overwrite your
real node with your empty one. Everything before H is reversible.

---

## Phase A — Home Assistant's onboarding, on the standby

You are here now.

1. **Create the account.** Any name and password you like. Write it down anyway —
   you need it for Phases B–D, and it stops working the moment Phase H first
   promotes this node.
2. **Do NOT choose "restore from backup".** If you restore the primary's backup
   here you get two instances claiming one identity before anything is
   configured to arbitrate, which is the split-brain this project exists to
   prevent, arrived at by hand.
3. **Name the location something distinguishable** — `tiger2`, not the same name
   as the primary. It will be overwritten at Phase H, but until then it is how
   you tell two browser tabs apart, and how the logs read.
4. Set location, units, currency. None of it survives Phase H. Do not linger.
5. On the device-discovery screen, **skip everything**. Adding integrations here
   creates entries that the go-bag will later replace, so anything you configure
   now is work you will do twice.

**Done when:** you can log into the standby's Home Assistant and see an empty
dashboard.

---

## Phase B — put the integration on the standby

The repository is private, so HACS cannot install it. It is a file copy.

```bash
# on the standby host
git clone git@github.com:boywiz/ha-cluster-state-sync.git /tmp/csr
cd /tmp/csr && git checkout v0.2.0
cp -r custom_components/cluster_state_sync /path/to/config/custom_components/
chown -R <ha-uid>:<ha-gid> /path/to/config/custom_components/cluster_state_sync
docker restart <ha-container>
```

`<ha-uid>` matters: if Home Assistant cannot read the directory the integration
simply never appears, with nothing in the log that says why.

**Done when:** Settings → Devices & Services → **+ ADD INTEGRATION** lists
"Cluster State Sync". It will not appear anywhere else yet — there is no config
entry, and the integration adds nothing to the sidebar.

---

## Phase C — configure it, in a scratch namespace

**+ ADD INTEGRATION → "Cluster State Sync".**

1. **Accept the no-warranty notice.** It is a real gate and will not let you
   past. It names the thing that matters: `climate` and `water_heater` are
   mirrored by default, so a wrong restore changes heating or hot water, not a
   dashboard value. (`alarm_control_panel` is opt-in, and off unless you turn
   it on.)
2. Choose **direct** (not sentinel — Sentinel is deferred and untested).
3. Fill in the backend:

   | Field | Value |
   |---|---|
   | Host | `valkey-cluster-state.example.com` |
   | Port | `6380` |
   | Database | `2` (the default — see below) |
   | Username | `cluster_state_sync` |
   | Password | from OpenBao, `secret/mm/cluster-state-sync` |
   | Use TLS | **on** |
   | TLS CA | `/config/manning-madness-root.crt` |

   🚨 **Use the name, never the IP.** The client does hostname verification and
   the certificate's CN is that name, so `192.168.1.78` fails — correctly, and
   in a way that looks exactly like a broken deployment. The OpenBao record
   currently stores the IP; ignore it, and fix the record.

4. **Namespace: `smoke`.** Not `prod`.

   The standby has no real configuration yet. The moment this wizard finishes it
   starts publishing *its* state into the shared hash, and you do not want an
   empty node's state sitting in the namespace the primary will later join. A
   throwaway namespace costs one reconfigure at Phase F and keeps the real one
   clean.

5. **Node ID: leave the suggested value.** It is `hostname-<6 chars of the
   install UUID>`, unique per instance by construction (AR-0025). Two nodes
   built from the same compose file share a hostname, and identical node IDs
   mean both halves of the cluster claim one identity. Do not type over it and
   do not copy the primary's.
6. **Cluster secret:** generate one now and **store it** — Phase F needs the
   *identical* string. A mismatch is silent: every entry fails verification, the
   restore skips all of them, and the log says so politely while everything
   looks healthy.
7. **Fileset replication: OFF — but only because of *this* ordering.** Read the
   next paragraph before copying that answer anywhere else.

   The setting does two separate jobs. It gates **publishing**, which is
   leader-only at runtime; and it gates **what the bundle emits** — the pull
   program, the swap, the key, and the promoter all live inside `if fileset:`
   in `bundle.py`.

   In this phase the standby is configured *before* the primary exists, on
   `leadership_source: always` with no peer, so it **is** the leader and it
   **would** publish its empty identity as the cluster's. That is the only
   reason it is off here.

   🚨 **The standby is the node that pulls.** Once it is on
   `leadership_source: lease` with a healthy primary holding that lease, the
   answer inverts: fileset replication must be **ON** on the standby, or its
   bundle contains no `fileset_pull.py`, no swap, and **no promoter** — the
   standby ends up unable to promote at all. Phase G is the switch that flips
   this answer; see Phase H.

---

## Phase D — verify the plumbing

Settings → Devices & Services → **Cluster State Sync** → the device.

| Entity | Expected | If not |
|---|---|---|
| **Backend** | `on` | The connection failed. Check the log — hostname verification and ACL errors both say what they are. |
| **Entities tracked** | non-zero | Nothing matched the include-domains filter. Harmless on an empty instance. |
| **Cluster leader** | this node's id | With `leadership_source: always` and no peer, this node leads. |
| **Is leader** | `on` | As above. |
| **Shared snapshot age** | small, rising, resetting | Rising forever means writes are failing. |
| **Fileset degraded** | `off` | Should be off — the fileset is disabled. |

**Done when Backend is `on`.** That single reading proves the name resolves, TLS
verifies against the CA, and the ACL user authenticates. Nothing else here can
be true if that is false.

**Stop here for now.** Phases E onward touch the live primary and want a healthy
backup RAID behind them.

---

## Phases E–I — what comes next, and why in that order

**E — back up the primary, off-host.** Use Home Assistant's own backup first —
it locks the recorder database for writes, which a raw `tar` cannot, and it can
exclude the database entirely. Then copy it **off** the host, because it lands
in `/config/backups` on the machine whose array is degraded. Add a byte-exact
`.storage` copy as a second line, taken **inside** the container: a host-side
`tar` silently skips root-owned `0600` files including `.storage/auth`, and
exits 0 with a plausible-looking archive that contains no credentials. This
project has already produced exactly that archive once. Stream it out of the
container instead, which runs as root:

```bash
ssh <primary> "docker exec <ha-container> tar -C /config -cf - ." \
  | zstd -T0 -o primary-$(date +%F).tar.zst
```

Verify it before trusting it: the archive must contain `.storage/auth`, and that
file must list your refresh tokens.

**F — the primary joins.** Same namespace (`prod` now, on both) and the
**identical** cluster secret. This is where the scratch namespace gets swapped
out on the standby.

**G — leadership.** Switch both to `leadership_source: lease`. Until now both
nodes write; from here only the lease holder does, and the split-brain guard is
actually engaged.

**H — the go-bag.** Enable fileset replication **on both nodes, primary first**.

The "primary only" instruction this runbook used to carry was wrong, and wrong
in the direction that quietly disables failover. Publishing is gated on holding
the lease, so a standby with the setting on and the lease elsewhere publishes
nothing — the risk the old wording guarded against does not exist once Phase G
has switched both nodes to `lease`. What the setting genuinely controls on the
standby is its **bundle**: with it off, `build_bundle` emits no `fileset_pull.py`,
no `cluster-fileset-swap.sh`, no `cluster-fileset.key` and no
`cluster_promoter.py`. A standby configured that way installs a bundle that can
neither receive the go-bag nor promote onto it, and every symptom of that is a
timer skipping quietly once a minute — AR-0040's shape exactly.

Turn it on at the primary first and confirm it is publishing, then at the
standby. This is still the destructive phase — see the warning at the top.

**I — the host bundle.** The promoter, its timer, the notify scripts, the
firewall rulesets. Only when the primary's RAID is healthy: the first tick takes
the lease, calls that a promotion, and runs `notify_master.sh`, which stops Home
Assistant, swaps `.storage` and starts it again.

> The promoter ships **inside** the integration you copied in Phase B, at
> `scripts/cluster_promoter.py`. It is inert there: `bundle.py` reads it as text
> and emits it for you to install deliberately, and nothing in a running Home
> Assistant can execute it. Copying the integration does not install the
> promoter.

---

## Two failures that look like success

**Different cluster secrets.** Both nodes come up, both report `Backend: on`,
both mirror happily — and neither can read the other's entries. `Entities
restored` is `0` after a promotion and the log says entries were skipped as
unreadable. Check the secrets match before you check anything else.

**Different namespaces.** Two perfectly healthy clusters of one node each. Every
entity reads green on both. `Cluster leader` naming *itself* on both nodes, with
`Backend: on`, is the tell.
