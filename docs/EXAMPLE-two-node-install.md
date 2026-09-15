# Worked example: two machines to a proven failover

One install, start to finish, with every value filled in.

[`RUNBOOK-installation.md`](RUNBOOK-installation.md) is the reference — it
explains *why* each step is where it is, and covers the cases this example does
not hit. **This page is the other thing:** one concrete pass through it, in
order, with real commands and the output you should actually see.

Read the runbook's section 1 first to check this will work for you at all. Then
come here to do it.

**How long.** Four evenings, not one afternoon. Not because any step is long —
most take minutes — but because two of the waits are the point, and skipping
them is how people discover their store was misconfigured with a live standby
already attached to it.

---

## The estate in this example

**These machines are invented.** Substitute your own everywhere.

| Name | Address | What it is |
|---|---|---|
| `ha-alpha` | 192.168.1.10 | Your existing Home Assistant. Real config, real history, real radios. |
| `ha-beta` | 192.168.1.11 | A second machine. Nothing on it yet. |
| `store` | 192.168.1.12 | A small always-on box that is **neither of the above**. |

Both Home Assistant instances run in Docker, container name `homeassistant`,
config at `/opt/homeassistant/config` on the host.

> **Why `store` is a third machine** and not a container on `ha-alpha`: if the
> store lives on the primary, losing the primary loses the thing the standby
> needs in order to take over. A NAS, a router that runs containers, a Pi — any
> of them will do. It does not need to be fast.

---

## Evening 1 — the store, and a backup you can trust

### 1. Stand up Valkey

On `store`, `/opt/valkey/docker-compose.yml`:

```yaml
services:
  valkey:
    image: valkey/valkey:8.1
    command: >-
      valkey-server --requirepass 8Qv2sLmXn4tP9wRc
      --maxmemory 1gb --maxmemory-policy noeviction --appendonly yes
    ports: ["6379:6379"]
    volumes: ["./data:/data"]
    restart: unless-stopped
```

```console
$ docker compose up -d
$ docker exec -it valkey valkey-cli -a 8Qv2sLmXn4tP9wRc PING
PONG
```

🚨 **`noeviction` is not a preference.** Under memory pressure the default
policy drops keys, and the key it drops may be a blob the go-bag manifest still
references. The standby then stages a fileset that is missing a piece, and finds
out at promotion.

Generate your own password. `8Qv2sLmXn4tP9wRc` is in a public document.

### 2. Back up `ha-alpha` properly

**Settings → System → Backups → Create backup**, database excluded. Use Home
Assistant's own backup rather than `tar`: the recorder locks the database for
writes while it runs, and a `tar` of a live SQLite file can hand you a torn
database that looks fine until the day you need it.

Then get it off the machine you are about to modify:

```console
$ scp ha-alpha:/opt/homeassistant/config/backups/*.tar ./
```

And take the small byte-exact copy that works even when Home Assistant will not
start:

```console
$ ssh ha-alpha 'docker exec homeassistant tar -C /config -cf - .storage' \
    | zstd -o storage-2026-09-11.tar.zst
```

🚨 **That `tar` runs inside the container.** A host-side `tar` as a normal user
silently skips root-owned `0600` files — including `.storage/auth`, which holds
every refresh token — and exits `0` with a plausible archive. Verify before
trusting it:

```console
$ zstd -dc storage-2026-09-11.tar.zst | tar -tf - | grep '.storage/auth'
.storage/auth
.storage/auth_provider.homeassistant
```

Nothing listed means you have an archive with no credentials in it. Do it again
inside the container.

---

## Evening 2 — `ha-alpha` joins (Level 1)

The node with your real configuration goes first, always. The go-bag flows
leader → standby, so the machine with something worth copying must be the
established leader before anything can flow the other way.

```console
$ scp -r custom_components/cluster_state_sync \
    ha-alpha:/opt/homeassistant/config/custom_components/
$ ssh ha-alpha docker restart homeassistant
```

**Settings → Devices & Services → + ADD INTEGRATION → "Cluster State Sync"**

| Field | Value here | Note |
|---|---|---|
| Connection | **direct** | Not sentinel. Sentinel is for an existing HA Redis. |
| Host / port | `192.168.1.12` / `6379` | |
| Username / password | *(blank)* / `8Qv2sLmXn4tP9wRc` | |
| TLS | off | See below. |
| Database | **2** | The default. **Write it down.** |
| Namespace | `home` | **Write it down.** |
| Cluster secret | generate one | **Write it down.** `ha-beta` needs the identical string. |
| Node ID | leave the default | `ha-alpha-4f2c91` — unique by construction. |
| Fileset replication | **off** | It goes on at Evening 4. |

> **Three values must match on both nodes exactly:** database, namespace,
> cluster secret. Get any of them wrong and both nodes report `Backend: on`,
> write to different places, and never see each other. There is no error for
> this. Put all three somewhere you will find them.

> **On TLS:** off is defensible on a wired VLAN you control and indefensible
> over anything else. [`GUIDE-infrastructure.md`](GUIDE-infrastructure.md)
> covers turning it on, including the integration generating its own CA. Do it
> now if you are going to — retrofitting means editing both nodes.

### The one reading that matters

**Settings → Devices & Services → Cluster State Sync → entities**

```
Backend             on
Cluster leader      ha-alpha-4f2c91
Entities tracked    284
```

`Backend: on` proves four things at once: the host resolves from inside the
container, TLS verifies if you enabled it, the credentials work, and the
integration loaded.

### Now stop for a day

**This is the wait that is the point.** You have one node doing something
useful and nothing irreversible has happened. If the store is wrong, the
namespace is wrong, or the container cannot reach `192.168.1.12`, you want to
find out with one node in play.

Come back tomorrow and check `Last snapshot age` is under 20 minutes.

---

## Evening 3 — `ha-beta` joins, then leadership

### 1. Install Home Assistant on `ha-beta`

Work through the onboarding wizard quickly. Create any account.

> **What you create here is temporary and will be destroyed.** At Level 3 the
> go-bag replicates `.storage`, which includes `auth` — so the first promotion
> replaces this node's users, passwords and refresh tokens with `ha-alpha`'s.
> Afterwards you log in here with your **`ha-alpha`** credentials. That is the
> feature working correctly, and it is not obvious from the wizard in front of
> you.
>
> Do **not** "restore from backup" during this onboarding. That gives you two
> instances claiming one identity before anything exists to arbitrate.

### 2. Add the integration

Same as Evening 2, with the same store, **same database `2`**, **same namespace
`home`**, **same cluster secret**. The node ID default differs already
(`ha-beta-8d17e3`) — leave it.

Fileset replication **off** on this node too, for now.

### 3. Check they can see each other

On either node:

```
Cluster members     2
Cluster leader      ha-alpha-4f2c91
Clock skew          0.4 s
```

**`Cluster members: 1` on both nodes is the failure this catches.** It means
they are both talking to a store and not to the same place in it. Check
database, namespace and secret, in that order.

`Clock skew` above a couple of seconds wants fixing now rather than later — the
lease is time-based, and two machines that disagree about the time disagree
about when a lease expired.

### 4. Switch both to `lease`

Both nodes default to `leadership_source: always`, meaning both write. Once both
report `Cluster members: 2`, reconfigure **both** to `lease`.

From here only the lease holder writes, and the split-brain guard is doing
something rather than being declared.

```
Is leader           on        (ha-alpha)
Is leader           off       (ha-beta)
```

---

## Evening 4 — the go-bag, and the promoter

🚨 **This is the destructive evening.** Everything before it is reversible.
From here, enabling the go-bag with leadership pointing the wrong way
overwrites your real node with your empty one.

Before you start, confirm `ha-alpha` reads `Is leader: on` and `ha-beta` reads
`off`. Look at it. Do not assume it, because the check costs five seconds and
the mistake costs your configuration.

### 1. Fileset replication on — `ha-alpha` first

Reconfigure `ha-alpha`, turn fileset replication **on**. Wait until
`Fileset age` shows a number rather than `unknown`.

Then turn it on at `ha-beta`.

> **"Primary only" is wrong**, and used to be in this project's own runbook. The
> setting has two jobs: it gates *publishing*, which is leader-only at runtime
> anyway — and it gates *what the bundle emits*. Leave it off on the standby and
> the next step installs a bundle that can neither receive the go-bag nor
> promote onto it, with no symptom beyond a timer skipping quietly, once a
> minute, forever.

### 2. Generate and install the bundle, on both hosts

The bundle is written **inside** the Home Assistant container, and it is
**per-node** — nine of its files carry that node's identity. Installing
`ha-alpha`'s bundle on `ha-beta` makes `ha-beta` claim to be `ha-alpha`.

Generate it from the integration's reconfigure flow. It prints the exact
`docker exec … tar` command for getting that node's bundle out; run the printed
command rather than composing your own, then on each host:

```console
$ sudo mkdir -p /etc/cluster-sync && sudo cp -r ./bundle/* /etc/cluster-sync/
```

**Read the generated `INSTALL.md`.** It is written for your values, not for this
example's, and it names the files this example cannot know you have.

If the wizard could not determine your Docker network mode it generates all
three firewall variants plus a dispatcher, `apply-leader.sh` and
`apply-follower.sh`, with one line to fill in:

```console
$ docker inspect -f '{{.HostConfig.NetworkMode}}' homeassistant
host
$ sudo $EDITOR /etc/cluster-sync/apply-leader.sh    # NETWORK_MODE=host
$ sudo $EDITOR /etc/cluster-sync/apply-follower.sh  # the same, on both hosts
```

Left unset it refuses to guess and logs so, which is the right behaviour and an
easy line to miss. Rules that load cleanly and match nothing would be worse.

### 3. Enable the timers — while you are in front of the machines

```console
$ sudo systemctl enable --now cluster-promoter.timer
$ sudo systemctl enable --now cluster-fileset-pull.timer
```

The first tick takes the lease, calls that a promotion, and runs
`notify_master.sh` — which **stops Home Assistant, swaps `.storage` and starts
it again**. Not on a Friday, and not over SSH from somewhere else.

```console
$ journalctl -u cluster-promoter -n 20 --no-pager
cluster-promoter: took the lease -- promoting
cluster-promoter: notify_master.sh -> swapped fileset, started home assistant
```

---

## Proving it — the drill

**A drill with no differing probe proves nothing.** If both nodes already agree,
"restored your state" and "did nothing at all" produce identical output, and the
first time you can tell them apart is during a real outage.

So make them disagree first.

### 1. Plant the probe

On `ha-alpha` — the leader — create a helper with a value you will recognise:

**Settings → Devices & Services → Helpers → + Create helper → Number**, named
`drill probe`, set to **42**.

Wait for `Last snapshot age` to reset, or call the service by hand:

```yaml
service: cluster_state_sync.flush_snapshot
```

### 2. Confirm `ha-beta` does not have it yet

`ha-beta` has never seen this helper. That asymmetry is the whole test.

### 3. Pull the plug

Physically, or:

```console
$ ssh ha-alpha sudo systemctl poweroff
```

**Pull the power or stop the machine — do not just stop the Home Assistant
container.** They are different failures with different budgets. Losing the
machine is measured at around **102 seconds** to an initialised standby. Home
Assistant dying while the machine stays up takes closer to **ten and a half
minutes**, on purpose, because a short grace hands your house to the other
machine every time Home Assistant is slow to restart.

### 4. Watch `ha-beta`

```console
$ journalctl -u cluster-promoter -f
cluster-promoter: lease expired (holder ha-alpha-4f2c91) -- taking it
cluster-promoter: promoted -- running notify_master.sh
```

Then in `ha-beta`'s log:

```
Restored 284 entities from snapshot (snapshot age at restore: 43s, max 1800s)
```

### 5. Read the probe

`input_number.drill_probe` exists on `ha-beta`, and it reads **42**.

That is the only line in this document that proves the system works. `Backend:
on`, a promotion in the journal, and a green health check are all true of a
cluster that restores nothing — that exact combination once passed on this
project while **305 tests and 93% coverage** agreed, and the restore had never
worked at all.

### 6. Fail back

Bring `ha-alpha` up. It does **not** take the lease straight back — there is a
hold-down, deliberately, so a flapping machine cannot take the house hostage.
Wait it out rather than fighting it.

---

## What you now have — and what you do not

**You have:** state and configuration mirrored continuously; a standby that
comes up initialised rather than empty; automatic promotion on host loss, at
around 102 seconds.

**You do not have** — and these are not bugs, they are the shape of the design:

- **Leader election for automations.** During the overlap window *both*
  instances fire automations. Anything that toggles rather than sets, or
  increments, or sends a message, can happen twice. Write `turn_on`, never
  `toggle`.
- **Your radios**, unless you arranged for them to move. A radio plugged into
  `ha-alpha` is attached to `ha-alpha`.
- **Anything in a separate container on `ha-alpha`.** A Zigbee coordinator, an
  MQTT broker or a database on the failed host does not move, and `ha-beta`
  starts cleanly and cannot reach any of it.

Read [`KNOWN-LIMITATIONS.md`](KNOWN-LIMITATIONS.md) in full before you rely on
this. It is short, everything in it is true, and every item was found the
expensive way.

## When it does not go like this

[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) first. Then
[`GOTCHAS.md`](GOTCHAS.md), which is the register of traps this project actually
fell into — nearly all of them silent.
