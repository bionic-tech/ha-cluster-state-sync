# Glossary

Every term this project uses that you might reasonably not know. **No prior Home Assistant,
Docker or clustering knowledge assumed.** If a doc uses a word that is not here, that is a bug in
the docs — please say so.

Ordered by when you are likely to meet it, not alphabetically.

---

## The basics

**Home Assistant** — the home automation software this integration plugs into. Runs your
automations, talks to your devices, serves the dashboard you look at.

**Integration** — Home Assistant's word for a plug-in. `cluster_state_sync` is one. It goes in a
folder called `custom_components/` and shows up under *Settings → Devices & Services*.

**Entity** — one thing Home Assistant knows a value for: `light.kitchen`, `sensor.outside_temp`,
`switch.kettle`. An entity has a **state** (`on`, `21.4`, `home`) and **attributes**.

**Domain** — the bit before the dot. `light`, `sensor`, `switch`, `climate`. This integration lets
you choose **which domains to mirror**, because copying a temperature reading is useful and
copying 500 diagnostic sensors is noise.

**`.storage`** — a hidden folder inside your Home Assistant config directory. **This is your
instance's identity**: your user accounts, your passwords, your login tokens, which integrations
you have set up and their settings. Not your automations (those are YAML files) and not your
history (that is a database). When these docs say "identity", they mean `.storage`.

**Config entry** — one configured instance of an integration, stored in `.storage`. If you have
three RFXtrx transceivers, you have three config entries for the `rfxtrx` integration.

**HACS** — the Home Assistant Community Store, a popular add-on for installing custom
integrations from GitHub. Not required here; you can copy the folder by hand.

---

## Containers and hosts

**Docker** — software for running applications in isolated boxes called containers. Home
Assistant is very commonly run this way.

**Container** — one running instance of an application under Docker. Your Home Assistant is a
container; so are Valkey, deCONZ and friends.

**Docker Compose** — a file (`docker-compose.yml`) describing containers and how to run them, so
you say `docker compose up -d` instead of a long command. Some setups also use **profiles** to
group services.

**Host** — the actual machine (or VM) that runs Docker. "Host-side" means *outside* the container,
on the machine itself.

**Bind mount** — making a folder on the host visible inside a container, written
`host/path:container/path`. This is how Home Assistant sees your config, and how a container sees
`/dev/serial`.

---

## The cluster

**Node** — one of your Home Assistant machines. This project assumes two.

**Primary** — the node with your real setup, the one you have been using.

**Standby** — the second node, waiting to take over. Also called the *secondary* or *peer*.

**Active-passive** — only one node does the work at a time; the other waits. (As opposed to
*active-active*, where both work at once. This project is **not** that, deliberately.)

**Failover** — the standby taking over because the primary stopped.

**Promotion** — a standby becoming the active node.

**Demotion** — an active node stepping down.

**Leader** — whichever node is currently active. This project decides leadership with a *lease*.

**Lease** — a claim on leadership, stored in Valkey with an expiry (a **TTL**). The leader keeps
renewing it. If the leader dies, it stops renewing, the lease expires, and the standby can take
it. This is the split-brain guard — the claim is granted *atomically inside Valkey*, so two nodes
asking at once get different answers.

**TTL (time to live)** — how long the lease survives without renewal. Here, **30 seconds**.

**Split-brain** — both nodes believing they are the leader at once. The thing a cluster most
needs to avoid, because both then act on the house.

**Fail closed** — when something cannot be established, assume the unsafe answer is *no*. A node
that cannot reach Valkey does **not** assume it leads. Every leadership path here fails closed.

**RTO (recovery time objective)** — how long a failover is allowed to take. This project's budget
is **2.5 minutes**; the measured cold-boot time is **78.8 seconds** (host loss).

---

## This project's own words

**Valkey** — an open-source fork of Redis: a fast in-memory key-value store. This project uses it
as the shared noticeboard both nodes read and write. **Redis 7+ works identically.** You need one
somewhere; it should **not** live on either Home Assistant machine (see the installation runbook).

**Namespace** — a label that keeps one cluster's data separate from another's inside the same
Valkey. Both your nodes must use the **same** namespace; two different clusters sharing a Valkey
use different ones.

**Snapshot** — the mirrored entity states written to Valkey, so a promoted standby can restore
them instead of starting blank.

**Go-bag (fileset)** — a replicated copy of the primary's *identity and configuration* —
`.storage`, `custom_components`, your YAML — staged on the standby so that after a promotion you
do not have to log in again or re-add every integration. Named for the bag you keep by the door.

**Cluster secret** — a shared password used to *sign* every snapshot entry, so a node can tell a
genuine entry from a forged or tampered one. **Both nodes need the identical string.**

**The host bundle** — generated shell scripts, systemd units and the promoter, which you install
on each machine. It is what actually stops and starts Home Assistant. The integration *writes*
it; it does not run it (see ADR-005).

**The promoter** — `cluster-promoter`, a small program run by a systemd timer every 10 seconds. It
takes or renews the lease and runs the notify scripts when leadership changes. It replaced
Keepalived.

**Notify scripts** — `notify_master.sh`, `notify_backup.sh`, `notify_fault.sh`. The promoter runs
these on a leadership change; they do the stopping, starting and swapping.

**Hold-down** — after a node releases the lease, it refuses to take it straight back for 15
minutes, to stop a promote-fail-release loop. Cleared with `--adopt`.

**Maintenance hold** — a switch that suspends automatic failover on a node, so you can restart
Home Assistant without triggering one. **On the leader, a Home Assistant restart is a failover
unless you set this first.**

**Probe / D3** — the promoter asks Home Assistant's HTTP API whether it is alive before *renewing*
the lease. It never uses the probe to decide whether to *take* one — "the probe gates renewal,
never taking".

**Probe grace** — how long a leader keeps renewing after its Home Assistant stops answering,
before giving up. **600 seconds**, so a Home-Assistant-only failure takes ~10.5 minutes to fail
over, by design.

**Pre-flight** — a check run before starting Home Assistant on a promoted node. It disables config
entries whose hardware is absent, so Home Assistant starts instead of failing setup.

---

## Radios and remote hardware

**Radio** — here, any USB stick or device that talks a home-automation protocol: Zigbee
(ConBee, SkyConnect), 433 MHz (RFXtrx), Z-Wave.

**Serial / tty device** — how the operating system presents a USB radio: `/dev/ttyUSB0`,
`/dev/ttyACM0`. These numbers **change**, which is why everything uses stable `by-id` paths
instead.

**`/dev/serial/by-id/`** — stable names for serial devices based on their make and serial number,
rather than the order they were plugged in. Always use these.

**USB-over-IP** — running a USB device over the network so a machine that is *not* physically
attached can use it. **VirtualHere** is one implementation; the Linux kernel's `usbip` is another.
This is what lets a radio move between nodes.

**Claim** — one machine taking exclusive use of a USB-over-IP device. Only one at a time.

**Auto-Use** — a VirtualHere setting that makes a client claim a device automatically as soon as
it becomes free. This is what makes radios follow a failover without anyone typing anything.

**Reap** — the USB-over-IP server noticing a client has died and releasing its claims (~17
seconds here). **A server never reaps a claim from a client that is still alive** — which is why a
partial failure does not move the radios (ADR-009).

---

## Testing and evidence

**AR-0040** — this project's founding incident: the restore had *never once worked*, while 305
tests passed. Nearly every rule here traces back to it.

**Rehearsal** — `tools/rehearsal/`, a harness that performs a real failover against real
containers. It is what found AR-0040 when the test suite could not.

**Adversarial review** — structured reviews written from hostile personas (a security engineer, an
unethical AI, a product owner doing espionage) kept in an internal review corpus.

**GOTCHAS** — `docs/GOTCHAS.md`, a numbered register of traps this project actually fell into.
Nearly all of them reported success while doing nothing. Read it before changing the restore, the
swap or the bundle.
