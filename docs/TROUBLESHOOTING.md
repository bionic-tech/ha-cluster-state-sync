# Troubleshooting

Symptom first. Each entry is a real failure that happened on a live cluster —
`docs/GOTCHAS.md` has the full archaeology if you want to know how it was found.

**Before anything else, two commands.** Most confusing failures are one of these
two things wearing a disguise:

```bash
# Is Home Assistant restarting in a loop?  (a loop hides every other fault)
docker logs <ha-container> | grep -c "Starting Home Assistant"

# Do two integrations claim the same domain?
for d in config/custom_components/*/; do
  printf '%s ' "$(grep -o '"domain": *"[^"]*"' "$d/manifest.json" 2>/dev/null)"
done | tr ' ' '\n' | sort | uniq -d
```

---

## Home Assistant keeps restarting, but `docker ps` says it never restarted

**Symptom.** Uptime resets every minute or so. Integrations never finish
setting up. `docker inspect` reports `RestartCount=0` and an unchanged
`StartedAt`.

**Why.** `homeassistant.restart` exits the *process*; the container's
supervisor starts it again inside the same container. Docker never sees a
restart. Only Home Assistant's own log counts these.

```bash
docker logs <ha-container> | grep "Starting Home Assistant" | tail -20
```

**Common cause.** An automation triggered on `homeassistant` `event: start`
whose action restarts Home Assistant. Check the *order* of its condition — a
condition placed after the restart action guards nothing.

**Fix it first.** Nothing below a restart loop can be diagnosed; every symptom
will point at whatever you happen to be looking at. One real case cost two days
and was blamed in turn on the Zigbee daemon, USB-over-IP, a serial library and
a wall switch — all wrong.

---

## An integration is enabled and installed, but has no entities

**Symptom.** The config entry exists and is not disabled. The directory is
present in `custom_components/`. Automations targeting its entities log:

```
Referenced entities light.x, light.y are missing or not currently available
```

**Why.** Two directories declaring the same `domain`. Home Assistant scans
`custom_components/` by directory, not by name, so a "keep a copy" folder like
`localtuya old` is an ordinary candidate — and the broken copy can win.

**Fix.** Move the spare copy **out** of `custom_components/`. Renaming is not
enough: the manifest's `domain` identifies an integration, not the folder name.

**Cluster note.** The go-bag replicates `custom_components/`, so a shadowing
directory travels to the peer. Failing over does not escape it. Audit both
nodes.

---

## The standby promoted, then demoted itself a few minutes later

**Symptom.** Promotion succeeds, Home Assistant starts, then the node goes back
to `BACKUP` and stops its own container. Repeats on the next promotion.

**Why.** The D3 probe grace expired while Home Assistant was still booting. The
promoter judged a live-but-slow instance dead, released the lease and ran
`notify_backup.sh`.

**Check.** Compare boot time against the grace:

```bash
docker logs <ha-container> | grep -oE "initialized in [0-9.]+s" | tail -3
grep -oE '\-\-probe-grace [0-9]+' /etc/cluster-sync/cluster-promoter.sh
```

**Fix.** The grace must exceed the **slowest** cold boot on the **slowest**
node, not a typical one. Being wrong in this direction costs a full failover,
not a slow one.

---

## The two nodes keep swapping the lease (flapping)

**Symptom.** Leadership bounces between nodes every few minutes. Each swap
stops one Home Assistant and starts the other.

**Why.** The release hold-down is shorter than the time the standby needs to
become *useful*. A released leader takes the lease straight back when the
window expires — D3's probe gates renewal, never taking, so a node whose Home
Assistant is stopped can still take.

**Fix.** Lengthen `--release-holddown`. It is not a delay to minimise; it is
what keeps a broken leader out until someone repairs it. `--adopt` clears the
marker when you have fixed the node, so a long window is not a trap.

**While flapping, configuration is at risk.** The go-bag flows from whoever
leads. A brief accidental promotion makes a divergent standby's config
authoritative, and the next swap installs it over the good copy.

---

## After promotion, radios (Zigbee / 433MHz) do not work

**Symptom.** Home Assistant is healthy on the promoted node, but serial-attached
integrations have no devices — or report `No such file or directory` for a path
you can see on the host.

**Check both views of `/dev`:**

```bash
ls /dev/ttyUSB* /dev/ttyACM*
docker exec <ha-container> ls /dev/ttyUSB* /dev/ttyACM*
```

**Why.** A container's `/dev` is a snapshot taken when the container starts.
A device that arrives or re-enumerates later never appears inside it, even for
a privileged container. `/dev/serial/by-id` symlinks stay correct — and point
at a node that does not exist on your side of the mount.

**Fix.** Restart the container; that is the only thing that rebuilds `/dev`.
**Set the maintenance hold first**, or stopping Home Assistant on the leader
triggers a failover.

**If your radios are USB-over-IP**, they must be attached to the promoted node
*before* its container starts. See **[GUIDE-radios.md](GUIDE-radios.md)**.

---

### The promoted node has NO radios at all, and `Attached serial devices: 0`

**Symptom.** The promotion log says:

```
timed out waiting for: usb-RFXCOM_RFXtrx433 usb-dresden_elektronik
Attached serial devices: 0
N config entr(ies) reference absent hardware -> Disabled N
```

Home Assistant is serving normally; it just cannot reach anything by radio.

**Why.** The old node is *still running* and still holding its USB-over-IP
claims. **No server reaps a claim from a client that is alive**, so the claim
hook waited its 45 seconds and gave up.

This is narrower than it looks. A full host loss releases the claims in ~17s and
works fine. A Home-Assistant-only failure also works — the old node's own demote
path stops its USB/IP client — but only after the 600s probe grace, so the
radios arrive about ten minutes late. **You only see `Attached serial devices: 0`
persist when the promoter itself stopped** while the machine kept running, so
nothing ran the release.

**Fix — on the OLD node**, release the claims:

```bash
sudo systemctl stop virtualhereclient.service     # or your USB/IP client
```

The new leader claims them within about 12 seconds. Then **restart Home
Assistant on the new leader** so its `/dev` picks up the arrivals (the snapshot
problem above), and re-enable any config entries the pre-flight disabled.

**Do not fix this by lengthening the claim deadline.** The claims are held
indefinitely, not slowly — a longer wait gives you a slower promotion that is
still radio-less. Background:
[ADR-009](adr/ADR-009-radio-custody-failure-modes.md), GOTCHAS §18d.

---

### My radios look dead but nothing is wrong

Before concluding a receiver has failed, **measure the baseline**. On this fleet
the normal gap between received 433 MHz packets reaches **nine hours** — every
watched entity belongs to a switch or remote, so a quiet house produces no
traffic at all and looks identical to a dead radio.

Two ways to get a real answer without waiting:

- **Enable the `undecoded` protocol** on the transceiver, then watch the debug
  log. It will then report *any* packet it can frame, including a neighbour's
  weather station — one such line proves the receiver works. Without it, a radio
  only reports protocols you configured, so ambient traffic is invisible.
- **Transmit from one radio and watch another** receive it. Reload each config
  entry first so every read loop demonstrably answers a status query — otherwise
  a silence afterwards proves nothing.

Full method and the incident that produced it: GOTCHAS §18a.

---

## Which process is holding my radio?

`lsof` and `fuser` are often absent on these hosts, and
`docker inspect .State.Pid` gives the container's **init**, not the
application — reading its file descriptors will tell you nothing is open when
three ports are. Ask the device instead:

```bash
for p in /proc/[0-9]*; do
  for f in $p/fd/*; do
    t=$(readlink "$f" 2>/dev/null) || continue
    case "$t" in /dev/ttyUSB*|/dev/ttyACM*)
      echo "$t <- ${p#/proc/} $(tr -d '\0' < $p/comm)";;
    esac
  done
done | sort -u
```

---

## A container says `(healthy)` but nothing works

**Symptom.** A daemon — Zigbee coordinator, bridge, proxy — reports healthy
while every device behind it is dead.

**Why.** A healthcheck proves what it measures, usually that a port answers.
That can be a different thread from the one doing the work.

**Check.** Silence plus CPU plus the consumer's view, together:

| logs | CPU | consumer | reading |
|---|---|---|---|
| silent | high | reconnecting | **livelocked** — restart it |
| silent | low | connected | idle, fine |
| flowing | any | connected | working |

```bash
docker logs --since 5m <container> | wc -l
docker top <container>
```

**Note for clusters.** The promotion probe asks *Home Assistant* whether it is
alive. A dead radio daemon behind a healthy Home Assistant triggers no failover
and marks nothing degraded.

---

## The restore restored nothing

**Symptom.** `Restored 0 entities from snapshot`, or a promoted node with empty
state.

**Check the skip reasons in the log line** — they say which gate rejected the
entries: `local-newer`, `own-node`, `oversized`, `unparsable`. `own-node` on
every entry means the snapshot was written by *this* node, so there was nothing
to learn from it.

**Also check snapshot age.** A quiet cluster stops advancing the timestamp
because `async_flush` skips when nothing has changed. An ageing snapshot on an
idle cluster is normal.

---

## Restarting Home Assistant on the leader caused a failover

**Expected, and there is a switch for it.** A stopped Home Assistant fails its
probe, so the promoter releases the lease and the peer takes over.

Before any planned maintenance:

```bash
sudo /etc/cluster-sync/cluster-hold.sh on "why you are holding"
# ... restart, upgrade, edit ...
sudo /etc/cluster-sync/cluster-hold.sh off
```

The hold makes the node renew-only: it keeps a lease it already has and never
takes a free one. Both nodes read the same flag file, from the container and
from the host.

---

## Everything looks fine, but is the cluster actually protecting me?

The failure worth knowing about is the quiet one: **Valkey is a single point of
failure for leadership, by design.** Every other component either moves the
house or raises an alarm. Valkey going away does neither.

If it is unreachable, both Home Assistants carry on exactly as before — lights,
automations, radios, all normal — and no node can take or renew the lease. A
host failure after that point promotes nobody. The protection is gone and the
dashboard looks the same.

**How to tell.** `binary_sensor.<node>_backend` is the signal. It is the only
entity that distinguishes "the cluster is healthy" from "the cluster is running
without any ability to fail over", and it is worth an actual notification
rather than a card you would have to be looking at.

**What to do.** Bring Valkey back; nothing on either node needs restarting, and
the promoter resumes on its next tick. If this happens often enough to matter,
the lease can run behind Sentinel — that is what the Sentinel option in the
wizard is for.

## History (long-term statistics) is not replicating

Every state below appears as a **repair** on the leader, under Settings →
System → Repairs. They are listed here because each one has a different fix and
two of them have a *wrong* fix that looks reasonable.

**First, the thing worth knowing before any of them:** only long-term
statistics cross between nodes — the years of energy and climate data behind
the Energy dashboard and long-range graphs. The recent logbook and the last ten
days of detail do **not**. After a failover your long graphs are intact and the
logbook starts fresh. That is by design (ADR-010), not a fault to report.

### "Standby has no history to replicate into" (`not_seeded`)

**What it means.** The standby has nowhere to put the statistics being
published. Six and a half million existing rows cannot arrive at half a
megabyte a day, so the standby needs a one-off seed you copy across yourself.

**What to do.** On the **leader**, press **Write statistics seed** (Settings →
Devices & Services → Cluster State Sync). It writes a file and shows a
notification naming it and the command. Copy it to the same filename in the
standby's Home Assistant config directory. On this kind of estate it is about
500 MB and a few seconds over a wired network.

You do not need to tell anything that the copy has finished. The standby checks
the file with SQLite's own integrity check on its next pull, adopts it if it is
whole, and refuses it if the copy was still running — so running the copy and
the pull at the same time is safe.

### "Standby's history has a gap that will not close" (`gap`)

**What it means.** The standby was out of contact for longer than the
replication window reaches back. The stretch in between is on neither node, and
**no future update will contain it**, because each update only carries the last
N days.

**What to do — and this is the one with a wrong answer.**

* If the missing stretch **matters**, write a fresh seed and copy it across, as
  above. This is the only action that recovers the missing history.
* If it does not, **widen the window** (Settings → reconfigure → *How far back
  each update reaches*) so it will not happen again, and accept the hole.

Widening the window **does not** recover history that has already been missed —
it only prevents the next one. Choosing it because it is the easier button is
how the gap becomes permanent. Sizing guide: 30 days costs about 4.5 MB per
update, 90 days about 12.5 MB.

### "Standby cannot store history: recorder schema mismatch" (`schema_mismatch`)

**What it means.** The two nodes are running different Home Assistant versions,
so their recorder databases have different schemas. The standby refuses the
data rather than guessing, because writing rows shaped for one schema into
another corrupts history in a way that opens cleanly and is wrong.

**What to do.** Bring both nodes to the same Home Assistant version, then
**start the standby's Home Assistant once** so its recorder performs its own
migration. Replication resumes on the next pull. Nothing is lost in the
meantime beyond the gap, which the `gap` advice above covers if it grows long.

### "Standby's history replication has gone quiet" (`statistics_stalled`)

**What it means.** The standby has not reported for several publish intervals.
The message distinguishes two very different causes — read which one it says:

* **"the standby is up — its promoter is beating"** — the machine is fine and
  the replication itself has stopped. Check the timer on that host:
  `systemctl status cluster-statistics-pull.timer` and
  `journalctl -u cluster-statistics-pull.service -n 50`.
* **"no peer promoter heartbeat is present"** — the standby is most likely
  switched off. Nothing else is implied about the cluster.

This alarm exists because silence used to be indistinguishable from success:
the status expires, the warnings clear themselves, and replication that had
stopped looked exactly like replication that was working.

### The pull log says the seed was refused

Expected, and not an error, if the copy was still running: a partially copied
SQLite file is a valid-looking database that is missing history, so it is
refused rather than adopted. It will be picked up on the following pass once
the copy finishes. If it persists after the copy has definitely completed, the
file is genuinely damaged — copy it again.

### Nothing appears at all, and there are no repairs

Statistics replication is **off by default**, including after an upgrade.
Reconfigure the integration; the question appears only when you have said that
history matters and that each node keeps its own database. On a shared
Postgres or MariaDB both nodes already read the same history and none of this
is needed.

## Getting help

### Start with the diagnostics file

**Settings → Devices & Services → Cluster State Sync → ⋮ → Download diagnostics.**

It carries the version, the config entry version, which node holds the lease,
snapshot age and size, what was restored at the last start, the replication
scope, and which alert conditions are on — which is most of what any question
here turns out to be about.

**You get a file, not an upload.** That is deliberate: open it, read it, then
decide whether to attach it. Nothing leaves the house without somebody having
looked first.

**What it withholds**, so you know what you are reading:

| | |
|---|---|
| Never included | passwords, the cluster secret, TLS material |
| Never included | hostnames, addresses, file paths, your ingress URL |
| Never included | entity IDs, and the names of your notify services |
| Shown as a count | excluded entities and devices — *"1 item(s), values withheld"* |
| Shown as an alias | node names, as `node-9c4e17` |

The aliases are stable and not reversible. The same node always gets the same
one, so *"the leader is node-9c4e17 and this node is node-9c4e17"* still reads
correctly, without publishing a hostname.

🚨 **Config values are allowlisted, not denylisted.** Anything the integration
does not explicitly mark safe appears as `**REDACTED**`. A setting added in a
later release is therefore withheld by default rather than published by
accident — which is the failure that cannot be undone once it is in a public
issue.

### Then the two things it cannot see

The integration can only report on itself. The promoter is a host-side timer, so
add from **both** nodes:

```bash
/etc/cluster-sync/cluster-hold.sh status
cat /run/cluster-sync/vrrp-state
journalctl -u cluster-promoter --since "10 min ago"
```

⚠️ **And check your logs before pasting them.** Home Assistant's logs are *not*
redacted for you — they carry entity names, and occasionally tokens. The
diagnostics file is cleaned; the log is not.

Lines worth finding: `ALERT raised`, `Restored … from snapshot`,
`Leadership change`, `Not restoring`, `Refusing to restore`.

### Where to send it

A bug goes to the issue tracker, which asks for exactly the above.

🚨 **A security problem does not.** This integration runs scripts as root on two
machines and decides which one controls your house. Read `SECURITY.md` and use
the private advisory form — a public issue cannot be taken back.

---

## Diagnostics

Every way this integration fails is quiet by design, so it exposes its own
health as entities. Backend errors are deliberately swallowed — a Redis outage
must never crash Home Assistant — which makes these the *only* place that
failure becomes visible.

<details>
<summary><strong>All fourteen diagnostic entities, grouped by what they tell you</strong> (click to expand)</summary>

**Replication health**

| Entity | What it tells you |
|---|---|
| **Entities tracked** | How many entities this node is mirroring. Compare against what you expect; this is the number that makes a broken filter obvious. |
| **Last snapshot age** | Seconds since this node's last *successful* write. Climbs and never resets if the flush loop stalls. Reads *unknown*, never zero, if nothing has ever been written. |
| **Shared snapshot age** | Age of what is actually in the store — the peer's writes included. This is what a promotion would restore *from*. |
| **Entities restored** | How many entities the last restore actually seeded. **A promotion that restored zero is the failure this integration exists to prevent.** |
| **Backend** | Connectivity to Valkey/Redis, polled every 60s. |

**Cluster shape**

| Entity | What it tells you |
|---|---|
| **Is leader** | Whether this node currently holds the lease. |
| **Cluster leader** | Which node does — by name, from the store, so both nodes agree or visibly do not. |
| **Cluster members** | How many nodes have checked in recently. |
| **Clock skew** | The spread between members' clocks. The restore's max-age window is meaningless once this is large, so it warns well before that cliff. |
| **Maintenance hold** | Whether failover is currently suspended here. |

**Go-bag (config replication)**

| Entity | What it tells you |
|---|---|
| **Fileset age** | How stale the replicated config is. |
| **Fileset degraded** | The go-bag was stale or missing at promotion. It marks degraded and **never blocks the promotion** (D4) — a standby with old config still beats no standby. |
| **Unreplicated config references** | Files your `configuration.yaml` includes that would *not* cross to the peer. This exists because a promoted node once came up in recovery mode behind a promotion that reported success at every step. |

**Radios**

| Entity | What it tells you |
|---|---|
| **Radio silence** | Seconds since anything you called a radio was last heard from. Off until you configure the globs. |

🚨 **Read the `status` attribute, not only the value.** It has three states, and
two of them read `unknown`:

| `status` | means |
|---|---|
| `ok` | at least one radio has reported — the number is real |
| `no_matches` | the globs match nothing — a configuration problem |
| `no_reports` | entities matched, none has ever reported — **the radio is deaf** |

`no_reports` is the loudest condition the sensor can find and it is deliberately
not a large number, because "never" has no age — a value threshold alone can
never fire on it.

⚠ **This measurement needs a device that transmits unprompted.** If every
entity you watch belongs to a switch or a remote, you are measuring human
activity, not radio liveness: after a restart it reads `no_reports` until
somebody presses something. Check your own longest normal gap between packets
before setting any threshold — on the fleet this was built for it is **nine
hours**.

🚨 **Watch one radio's signals per list.** The sensor reports the *freshest*
match, which is right within a radio and wrong across radios: a Wi-Fi RSSI
sensor reporting every 60 seconds will hold the number near zero through a
completely dead Zigbee or RF radio. Watching more entities looks safer and is
the exact opposite.

It also reads near-zero for the first few minutes after a Home Assistant
restart — Home Assistant writes every entity's state as it starts, so a low
value there says nothing about whether a packet has actually arrived. Set any
alert threshold longer than your instance's boot time.

</details>

Radio silence is the answer to a gap the failover design does not close and
still will not: Home Assistant can be perfectly healthy while every radio behind
it is dead. On this fleet a Zigbee daemon livelocked — 78% CPU, no output for 32
minutes, its healthcheck reporting `healthy` — and the whole house lost Zigbee
with every signal the cluster watches staying green.

It **does not trigger failover**. Promoting because a radio died would move the
house onto a node whose radios may be no better. And it does not pick your
threshold: only you know your own traffic, and a quiet house at 4am legitimately
produces no RF for a long while. What is diagnostic is a number that *used* to
move and has stopped. Full treatment in
GOTCHAS §18.

### Alerting — built in since v0.4.2

Exposing the sensors is only half the job. Every way this integration fails is
quiet by design: the backend goes away and the flush loop just stops, the peer
stops writing and the snapshot ages, a promotion restores nothing at all and
logs one line about it. Nothing in the house changes, so nobody looks — and
every surface listed above needs you to be *at home, looking at a screen*,
which is the one situation this product is not built for.

So the integration now tells you itself. **The wizard's "Who hears about it"
step is optional and submitting it untouched is a complete setup:** four
conditions, delivered as a persistent notification that every administrator
sees. Change it later under **Settings → Devices & services → Cluster State
Sync → Configure → Alerts**.

| Condition | Default | |
|---|---|---|
| **Failover** — the house moved to the other node | push | a 21-second failover is otherwise invisible |
| **Recovery** — a condition below cleared | push | an alarm that stops without a word is one you cannot trust |
| **Valkey unreachable** | push | the house keeps running; it can no longer **fail over** |
| **Promoted with an incomplete configuration** | push | the cluster promotes anyway (decision D4) — this is the only thing that says so |
| Statistics: schema mismatch, gap, not seeded, stalled | card only | real, but they can wait until Saturday |

Add any `notify.` service — the companion app, Discord, Slack, Telegram, email
— and those pushes reach you when you are out. Home Assistant already holds
those credentials; this integration stores only the service name.

Unticking a condition silences the **push** only. The repair card and the
diagnostic entities are raised regardless, so nothing is hidden — you are
simply not interrupted.

> [!NOTE]
> **Alerting cannot be more available than the thing it runs on.** These
> notifications come from Home Assistant, on a node that is up. They will not
> reach you if the whole house is down, and *"Valkey unreachable"* is reported
> by the very component that needs Valkey. Something outside the cluster should
> watch the cluster — see GUIDE-ingress.md.

### The blueprint — for thresholds and actions of your own

The built-in alerting covers the conditions the integration knows it is in. A
blueprint also ships, for the ones only you can define — a snapshot-age limit
you pick, or an action that is a light rather than a notification:

[![Open your Home Assistant instance and show the blueprint import dialog with a specific blueprint pre-filled.](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fbionic-tech%2Fha-cluster-state-sync%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fcluster_state_sync%2Ffailover_readiness.yaml)

Or **Settings → Automations & scenes → Blueprints → Import blueprint** and paste:

```
https://github.com/bionic-tech/ha-cluster-state-sync/blob/main/blueprints/automation/cluster_state_sync/failover_readiness.yaml
```

It watches three things, because they fail differently:

| Trigger | What it means | Also built in? |
|---|---|---|
| **Backend unreachable** (past a grace period) | Nothing is being written. A promotion now restores whatever was last saved, or nothing. | **Yes** — you will get two alerts if you enable both |
| **Snapshot age above a limit** | The backend is *up* and the flush loop stopped anyway — the harder failure to spot, and the one the Backend sensor cannot see. | No — the threshold is yours |
| **Restored zero entities** | This node promoted cold. Nothing came across from the peer. | No |

That third one is the reason the blueprint exists rather than a line in the
docs saying "alert on snapshot age". Restoring nothing was a real defect for the
entire life of this project, it was found by running the thing rather than by
testing it, and its only symptom was an INFO line nobody read.

If you run both, untick **Valkey unreachable** in the integration or drop the
backend trigger from the automation. Two alerts for one fault is how people
learn to ignore both.

Moved here from the README on 2026-09-11, where it was 159 lines of a
document nobody was finishing.
