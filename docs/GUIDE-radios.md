# Radios and remote hardware

**Read this before buying hardware, and before believing a failover works.**

If your house is entirely Wi-Fi and cloud APIs — Tuya, Hue over the network, ESPHome, anything
reached by IP — you can skip this page. Nothing here applies to you, and your failover story is
much simpler than everyone else's.

If any of your devices are reached by a **USB stick plugged into a machine** — Zigbee (ConBee,
SkyConnect, Sonoff), 433 MHz (RFXtrx), Z-Wave — then **how that stick is attached decides whether
your cluster works at all**, and it is the least obvious part of this whole project.

---

## The one-paragraph version

A radio can only serve whichever machine is holding it. If it is plugged into node A and node A
dies, the radio dies with it — no amount of clustering moves a physical USB plug. To make a radio
follow a failover it has to be reachable **over the network**, either by USB-over-IP or by living
behind its own daemon that speaks IP. This project can move network-reachable radios
automatically. It cannot move the others, and it will tell you so rather than pretend.

---

## Which of these are you?

| How your radio is attached | Can it fail over? | What you need to do |
|---|---|---|
| **Plugged into one Home Assistant machine** | ❌ **Never** | Accept it, or move to one of the rows below. See *Option A*. |
| **USB-over-IP** (VirtualHere, `usbip`) | ✅ Yes, mostly | Works today. Read the whole page — especially *When radios don't follow*. |
| **Its own network daemon** (Zigbee2MQTT, deCONZ, ESPHome, Z-Wave JS) | ⚠️ Only if the daemon itself is highly available | The radio is fine; **the container in front of it is the problem**. See *Option C*. |
| **No radios at all** (Wi-Fi / cloud only) | ✅ Nothing to do | You are done. |

---

## Option A — a directly-attached USB stick

**This cannot fail over. It is not a limitation of this software; it is a plug.**

On the fleet this was built for, one of three RFXtrx transceivers is plugged straight into the
primary's USB bus. Two are on USB-over-IP and move on promotion. The third never will, and its
config entry is honest about that rather than silently disabled every failover.

What you can do about it, in increasing order of effort:

1. **Accept it.** Decide which devices genuinely matter during an outage. A transceiver that only
   power-cycles a modem does not need to fail over.
2. **Move it behind USB-over-IP** (Option B) — a small always-on box, a Raspberry Pi or similar,
   with the stick in it.
3. **Replace it with a network-native device** — a Zigbee coordinator with Ethernet, an ESPHome
   node, a 433 MHz gateway that speaks MQTT.

> **The pre-flight makes this visible rather than mysterious.** When a promoted node cannot see a
> radio, it *disables that config entry* and logs which one, so Home Assistant starts instead of
> failing setup three times over. You get a working instance with a named missing radio, not a
> boot loop.

---

## Option B — USB-over-IP

The radio lives in a small box on the network. Whichever node holds the *claim* gets the device;
the other sees nothing. On promotion the new leader claims it.

<details>
<summary><strong>How this project drives it</strong> (click to expand)</summary>

The promotion path runs, in this order, and **the order is load-bearing**:

```
notify_master.sh
  1. cluster-fileset-swap.sh      swap the go-bag into place
  2. pre-start.d/*                claim the radios          <-- hooks run HERE
  3. ha-device-preflight.py       disable absent hardware
  4. docker start <container>     start Home Assistant
```

Claiming must happen **before** the pre-flight, or the pre-flight disables the very radios that
were about to arrive. It must happen before `docker start`, because a container's `/dev` is a
snapshot taken when it starts.

The hooks are **examples, not installed by default** — see `examples/hardware-custody/`. Your
provider is your business; this project only supplies the moment at which to act. A VirtualHere
worked example ships because that is what the author runs.

</details>

<details>
<summary><strong>Set-up checklist for VirtualHere specifically</strong></summary>

- Install the client on **both** nodes; the server wherever the sticks are.
- Turn on **Auto-Use** for each radio you want to move — this is what makes the claim automatic.
  Verify it with the `*` in `vhclient -t LIST`, **not** with "In-use by you", which is true in both
  the working and broken cases.
- 🚨 **Never claim, unclaim or auto-use a "USB 10/100 LAN" device.** On this fleet that interface
  is PoE and carries the network the VirtualHere server is reached over. Claiming it takes the
  whole device off the network. (GOTCHAS §15.)
- Bind-mount `/dev/serial` into your Home Assistant container, on **both** nodes:
  ```yaml
  volumes:
    - /dev/serial:/dev/serial
  ```
  Without it the container cannot resolve `by-id` paths that only exist on the host, and every
  radio fails to open with `[Errno 2] No such file or directory`.

</details>

<details>
<summary><strong>Three traps that have bitten this project, with the fix</strong></summary>

**`STOP USING` silently clears Auto-Use (GOTCHAS §18b).** Unclaim a device by hand and the
automatic-claim flag is gone, with no error and no visible difference — the device still shows
`In-use by you`. The next failover then comes up with no radios and nothing explaining why.
Always re-arm afterwards and confirm the `*` came back:

```bash
vhclient -t "STOP USING,server.113"
vhclient -t "USE,server.113"
vhclient -t "AUTO USE DEVICE,server.113"   # <- the step everyone forgets
vhclient -t "LIST" | grep RFXtrx           # confirm the * is back
```

**A reclaimed radio can come back on a different device node (GOTCHAS §17, §18c).** The stable
`by-id` symlink is updated correctly, but it now points at, say, `/dev/ttyUSB3` — and **the
container's `/dev` was snapshotted when the container started**, so that node does not exist
inside it. Every open fails `Errno 2`. **Only a container restart rebuilds `/dev`.** Reloading the
integration re-reads the same absent path.

**A "healthy" container can be doing nothing (GOTCHAS §18).** A Zigbee daemon here livelocked at
78% CPU with no output for 32 minutes while `docker ps` reported `(healthy)`. Healthchecks
generally prove a port answers, not that the radio thread is alive.

</details>

---

## Option C — a radio behind its own daemon

Zigbee2MQTT, deCONZ, Z-Wave JS and ESPHome each put a network service in front of the radio. That
is good for Home Assistant, and it moves the problem rather than solving it: **the daemon is now
the thing that has to be highly available**, and this project does not fail over other people's
containers.

On this fleet deCONZ runs on the primary only. When the standby is promoted the ConBee II follows
(it is on USB-over-IP), but **nothing on the standby is listening to it**, so Zigbee is dead until
the primary returns. That is a known, accepted gap, not a defect.

Your options are the same shape as Option A: accept it, run the daemon on both nodes with the same
config (only one can hold the radio at a time, so the loser must fail gracefully), or move that
daemon to a third always-on machine that is not part of the failover pair at all. **The third
option is the cleanest and the one to reach for if you are designing from scratch.**

---

## 🚨 When radios don't follow — the gap you must know about

**This is the most important paragraph on the page.**

Radios follow the lease when the failed node genuinely goes away. They do **not** follow when only
a *service* on it dies, because the failed machine is still running its USB-over-IP client and
still holding its claims — and **no server reaps a claim from a client that is alive**.

| What failed | What releases the claims | Radios follow? |
|---|---|---|
| Host loss, power cut, kernel panic | the USB/IP **server reaps a dead client** (~17 s) | ✅ within the 45 s deadline |
| Home Assistant dies, machine alive | the node's **own demote path**, after the 600 s probe grace | ✅ **but ~10 minutes later** |
| **The promoter dies, machine and USB/IP client alive** | **nothing** | ❌ radios stay put |

The middle row is the one you will actually meet, and it is a **latency** problem, not a custody
one: your radios do move, about ten minutes after Home Assistant stops answering. That delay is the
probe grace — the same setting that lets a leader ride out an ordinary Home Assistant restart
without failing over. It is one trade-off with two faces.

The last row needs the promoter to stop while the machine keeps running. `systemd` normally
restarts it, so this is unusual — but it is exactly what the RTO test induced, which is how it was
found.

**Waiting longer at the claim step does not help** — the 45 s deadline only matters in the first
row, where the reap beats it comfortably.

**What to do about it today:** if a promotion happens and your radios are missing, the fix is to
stop the USB-over-IP client on the *old* node. Its claims are released, and the new leader picks
them up within seconds. Then restart Home Assistant on the new leader so its `/dev` picks up the
arrivals.

The full reasoning, and the three candidate fixes none of which has been chosen, are in
**[ADR-009](adr/ADR-009-radio-custody-failure-modes.md)**.

---

## Can I tell whether my radios are actually receiving?

Partly, and the honest answer matters more than a green light.

The integration can expose **`sensor.<node>_radio_silence`** — seconds since the freshest of a set
of entities you nominate was last heard from. Configure it in the wizard's *Entities that prove a
radio is receiving* field.

<details>
<summary><strong>Why it is off by default, and when it will not work for you</strong></summary>

"Time since last packet" needs a device that transmits **unprompted** — a temperature sensor on a
schedule, a Zigbee coordinator's own diagnostic. If every entity you watch belongs to a *switch or
a remote*, you are measuring **human activity, not radio liveness**: after any restart it reads
"nothing heard" until somebody presses something, and on the fleet this was built for the normal
gap between packets reaches **nine hours**.

Read the `status` attribute, not just the number:

| `status` | meaning |
|---|---|
| `ok` | something has reported; the number is a real age |
| `no_matches` | your globs match nothing — a configuration problem |
| `no_reports` | entities matched, none has ever reported — **investigate** |

**`no_reports` reads as `unknown`, not as a big number**, because "never" has no age. So a
`numeric_state` alert on the value alone is silent in the worst case. Trigger on the attribute too.

**Watch one radio's signals per list.** The sensor reports the *freshest* match, so mixing a Wi-Fi
RSSI in with your RF sensors lets the Wi-Fi mask a completely dead radio. Watching more entities
looks safer and is the exact opposite.

**Measure your own baseline before setting any threshold.** One query for the longest normal quiet
period separates "gone quiet" from "always was". Skipping that step produced a confident,
published, wrong report of a seven-day outage on this fleet — see GOTCHAS §18a.

</details>

<details>
<summary><strong>Proving a receiver works when nobody is home</strong></summary>

Two techniques, both used on this fleet:

**Ambient traffic.** At debug level the RFXtrx library logs every packet it pulls off the air,
including from devices Home Assistant has never heard of — a neighbour's weather station, a
doorbell, a car remote. One such line proves the receiver works. But by default an RFXtrx only
reports protocols it is configured to decode, so **enable the `undecoded` protocol** first or you
will see nothing and wrongly conclude the radio is deaf.

**Transmit and listen.** If you have more than one transceiver, send a command via one and watch
whether the others log receiving it. Prove every read loop is alive first — reloading each config
entry makes each device answer a status query — so that a subsequent silence means something.

Both beat waiting: silence only becomes evidence once you have shown the instrument would have
spoken.

</details>

---

## Related

- **[ADR-009](adr/ADR-009-radio-custody-failure-modes.md)** — the decision record for all of this
- **[GOTCHAS.md](GOTCHAS.md)** §15, §17, §18, §18a–d — the incidents behind every warning here
- **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** — "After promotion, radios do not work"
- `examples/hardware-custody/` — the worked VirtualHere hooks, installed by nobody


---

## The radio that never moves, and why it is the one that matters

🚨 **Found on a real failover, 2026-09-09.** Worth stating on its own, because
it is the case where the general rule bites hardest.

A radio that is **physically plugged into one machine** does not follow a
promotion. That is not a limitation of this software — it is a plug. The rule
appears throughout this guide, but here is what it costs in practice.

On the fleet this integration was built for, one 433 MHz transceiver is
hardwired to the primary node and by design never transfers. It is the
transmitter that a **firewall RF-recovery watchdog** depends on. So a failover
— working perfectly, doing exactly what it was asked — **silently removes the
fleet's last-resort path for recovering the firewall.**

Nothing reported a fault. Nothing could: every device that *can* move had
moved, and the cluster has no way to know that the one that stayed behind was
the important one.

### What to do about it

1. **List every radio and ask, for each: can this follow?** Directly attached
   USB cannot. Reachable over IP (VirtualHere, ser2net, a network-attached
   coordinator) can, given the hooks.
2. **For each that cannot, ask a harder question:** would I need this *during*
   the failure this cluster exists to survive? A doorbell is an inconvenience.
   A transmitter that recovers your network is a circular dependency.
3. **Write down the answer where the person on call will find it** — not here,
   in your own runbook. The cluster cannot warn you about this, because it does
   not know which of your radios matters.

If the answer to (2) is yes for any device, that device wants either its own
redundancy or a recovery path that does not depend on it.
