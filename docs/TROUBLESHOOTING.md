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
*before* its container starts. See the hardware-custody design in
`docs/superpowers/specs/`.

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

## Getting help

Include: `cluster-hold.sh status` and `/run/cluster-sync/vrrp-state` from both
nodes, `docker logs <ha-container> | grep -c "Starting Home Assistant"`, and the
promoter's recent output (`journalctl -u cluster-promoter --since "10 min ago"`).
