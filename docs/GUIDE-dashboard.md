# The Cluster dashboard

Everything here appears on its own after you install the integration. There is nothing to paste,
no file to edit, and no entity name to look up.

---

## Where it is

**A "Cluster" entry in the Home Assistant sidebar**, with a server icon. It is created the first
time the integration finishes setting up.

* **Admin only.** It names hosts and shows which machine is in charge — operator information, not
  household information.
* **It appears after a restart, not immediately.** Home Assistant loads integration code at
  startup, so a freshly-installed integration gets its panel on the next start. Reloading the
  integration is *not* enough; a reload re-runs setup from the code already in memory.
* **Not in the sidebar?** Go straight to `/cluster-status`. If it renders, the panel is fine and
  your sidebar is just cached or has hidden it — long-press the sidebar header to check.

## Where your entities are

The integration files its device under an area called **Cluster**, so all fifteen entities land
together instead of loose among your lamps.

**If you move them, they stay moved.** Change the area on the device page and the integration will
not put it back. It only files a device that has no area at all.

---

## Reading the page

One card per node. Your own node always appears; a peer appears only if its entities are visible to
this instance.

### Leadership

| Row | What it means |
|---|---|
| **Is leader** | Whether *this* node is currently writing the shared snapshot. |
| **Cluster leader** | Who the whole cluster agrees is in charge. Should match the above on the leader. |
| **Members seen** | How many nodes have checked in recently. On a two-node cluster, `1` means the peer is not talking. |
| **Maintenance hold** | A button. See below. |
| **Hand over to peer** | A button, on the leader only. See below. |

### Freshness — could the standby take over *right now*?

| Row | What it means |
|---|---|
| **Our snapshot** | How long since this node wrote state to the shared store. |
| **Shared snapshot** | How old the snapshot a promoted node would restore from is. |
| **Config fileset** | How old the replicated configuration copy is. |
| **Fileset degraded** | `on` means the config copy is not trustworthy. |

**An ageing snapshot on a quiet cluster is normal**, and this trips people up. The flush is skipped
when nothing has changed, so an idle house stops advancing the timestamp. It is only worth
investigating if the cluster *should* have been busy.

### Coverage — what would actually survive

| Row | What it means |
|---|---|
| **Entities mirrored** | How many entities are being replicated. |
| **Restored at last start** | How many were restored the last time this node started. |
| **Config refs NOT replicated** | Files your configuration points at that the go-bag will not carry. **Above zero means a promoted node may fail to parse its own configuration.** |
| **Clock skew** | Difference between this node's clock and the shared store's. |

**Restored at last start is the number that matters after a promotion.** Zero against a non-empty
snapshot means every entry was skipped — the log line gives the reason (`own-node`, `local-newer`,
`oversized`, `unparsable`). `own-node` for everything simply means the snapshot was written by this
same node, so there was nothing to learn.

---

## The buttons, and what the colours mean

Colour is not decoration. It is the risk tier.

| Colour | Tier | Means |
|---|---|---|
| plain | **safe** | Repeatable. No effect on who leads. |
| amber | **caution** | Changes how the cluster behaves until you change it back. |
| red | **moves the house** | Leadership changes machine. Something will be briefly unavailable. |

### Maintenance hold — amber

**On = automatic failover is suspended on this node.**

**Use it before any planned work on the leader**: restarting Home Assistant, upgrading, editing
configuration, restarting a container it depends on.

> Restarting Home Assistant on the leader **without** this is a full, permanent failover. The
> health probe fails, the promoter hands the lease to the peer, and this node's container is
> stopped and *not* restarted. This has happened on a live system. This button is the fix.

Turn it on, do the work, turn it off. While it is on the node keeps a lease it already holds and
never takes a free one — so a restart does not become a handover.

**When not to use it:** never leave it on. A held node cannot fail over, so it is not protected. If
you walk away with it on, you have no cluster — just two computers.

### Hand over to peer — red

**Deliberately gives the cluster to the other node.**

Only appears on the leader; a standby has no lease to give away. Press it and, within about ten
seconds, this node releases the lease, stops its Home Assistant, and the peer promotes.

**Use it for** planned maintenance on the leader that a hold cannot cover — a host reboot, a disk
change, anything that takes the machine away — and to test that failover genuinely works.

**Before you press it, know what the standby cannot do.**

* **USB radios** move only if hardware-custody hooks are installed *and* the standby's container
  mounts `/dev/serial`. Without either, Zigbee, Z-Wave and 433MHz go dead until you hand back.
* **Separate containers Home Assistant depends on do not move at all.** A Zigbee coordinator
  daemon (deCONZ, zigbee2mqtt), an MQTT broker, a Z-Wave JS server — each is its own container
  with its own configuration and its own database, and none of that is replicated. See
  [known gaps](#known-gaps-before-you-rely-on-this).
* **Network devices keep working** — Tuya, Meross, Sonos, anything over MQTT.
* **Remote access** is separate again: see
  GUIDE-ingress.md.

**It shows "REQUESTED" until the promoter acts.** Click again to withdraw while it is pending. If
it stays requested for more than a tick or two, the promoter is not running.

**To come back**, press hand over on the *new* leader.

> ### 🚨 Do not hand back within fifteen minutes
>
> A node that gives up the lease **refuses to take it back for fifteen minutes**. That is
> deliberate — it is what stops a sick node flapping the cluster between machines.
>
> Hand over twice in quick succession and **both** nodes are holding themselves out. Neither takes
> the lease, neither runs Home Assistant, and the house has nothing until the window expires. This
> has happened on a live system, and it took five minutes to notice and recover.
>
> **If you need to come back sooner**, clear the hold-down on the node you want to lead, then hand
> over from the other one:
>
> ```bash
> sudo /etc/cluster-sync/cluster-promoter.sh --adopt
> ```
>
> `--adopt` re-reads the cluster's real state and clears the marker. It is the supported way out;
> deleting the file by hand works too but tells you nothing about what the cluster thinks.
>
> **Check both nodes before a planned handover.** A node still inside a window from an earlier
> handover cannot accept the cluster, so handing to it strands you.

### Flush snapshot now — safe

Writes the shared snapshot immediately instead of waiting for the interval.

**Use it** before a planned handover, so the standby starts from the freshest possible state, or
when "Our snapshot" looks older than you expect and you want to see it move.

**It is refused on a follower**, and that is correct: a follower writing the shared snapshot is the
split-brain this integration exists to prevent.

### Acknowledge degraded go-bag — caution

Only appears when something is actually degraded.

**It does not repair anything.** It clears the marker and the repair notice, so you stop being told
about a problem you already know about. The next promotion is what proves the configuration copy is
healthy again, and the marker comes straight back if the underlying condition persists.

---

## A five-minute check that everything is well

1. Exactly one node says **Is leader: on**, and **Cluster leader** agrees.
2. **Members seen** equals your number of nodes.
3. **Maintenance hold** is off on both, unless you are mid-maintenance.
4. **Config refs NOT replicated** is zero.
5. **Fileset degraded** is off.
6. Snapshot ages are minutes, not hours — unless the house has genuinely been idle.

If all six hold, a failover should work. The one thing this page cannot tell you is whether the
*radios* will follow; that depends on hardware-custody hooks, which are not installed by default.

---

## Known gaps — before you rely on this

**Separate containers are not replicated, and this is accepted rather than solved.**

This integration replicates Home Assistant's state and configuration. It does **not** replicate the
other containers Home Assistant talks to. The clearest case on the fleet it was built for is
**deCONZ**: a Zigbee coordinator daemon in its own container, with its own database of paired
devices, running only on the primary.

So a promoted standby can hold the ConBee stick and still have no Zigbee, because the daemon that
speaks to it is not there. Claiming a radio gets you a device node; it does not get you a working
network.

The same shape applies to anything Home Assistant depends on that lives beside it:

| Dependency | What also has to move |
|---|---|
| deCONZ / Phoscon | The container, and `/opt/deCONZ` — the pairing database |
| zigbee2mqtt | The container, its `data/` (device database), and the MQTT broker |
| Z-Wave JS UI | The container and its network keys + node cache |
| Mosquitto / MQTT | The broker, its persistence file, and its ACLs |
| ESPHome dashboard | Only for editing; devices keep running without it |
| Frigate, Node-RED, AppDaemon | Each its own container and its own configuration |

**What to do today:** run those on a host that is *not* one of the failover pair, or accept that
they are unavailable while the standby leads. On this fleet the second is the accepted answer for
deCONZ.

**A note on stopping a dependency's radio.** When the release hook stopped VirtualHere, deCONZ —
still holding the ConBee — exited with SIGSEGV. If you do run such a daemon beside Home Assistant,
stop it in `post-stop.d/` *before* the radios are released, and start it in `pre-start.d/` *after*
they are claimed. Ordering hooks by filename is what the numeric prefixes are for.

**Possible later, not planned:** replicating a dependency's configuration the same way this
replicates Home Assistant's. It is a genuine stretch goal, not a commitment, and nothing about the
current design assumes it.
