# Gotchas — traps this project has actually fallen into

Not a list of things that might go wrong. Every entry here **happened**, on this
fleet or in a rehearsal, and most of them were silent: the system reported
itself healthy while doing nothing.

That is the pattern worth internalising. This project's founding incident
(AR-0040) was a restore that had never once worked while 305 tests passed at
93% coverage, and its only symptom was a log line. Nearly everything below is
the same shape. **Read "no error" as "no evidence", never as "it worked".**

Each entry says what it looked like, why it happened, and the rule that
prevents a repeat.

---

## 1. `docker inspect .Source` lies for named volumes

**Looked like:** the config directory was on the wrong disk — under the Docker
images pool rather than the persistent data disk.

**Actually:** `.Source` reports Docker's *bookkeeping* mountpoint
(`…/volumes/<name>/_data`). For a `local` volume declared with `o=bind` the
real location is `Options.device`, which was the correct disk all along.

**Why it matters far more than tidiness:** that bookkeeping path exists **only
while a container is using it**. Docker mounts it on start and unmounts it on
stop. Measured on node-b:

```
WHILE RUNNING   mount | grep _data  ->  1 line
docker stop
WHILE STOPPED   mount | grep _data  ->  0 lines
```

The go-bag swap runs **while Home Assistant is stopped**. Pointed at that path
it would have written the promoted `.storage` into an empty directory on the
wrong filesystem, then started Home Assistant, which re-binds the real volume
over the top. Successful promotion, clean log, nothing restored.

**Rule:** read `.Mounts.Type` first. `bind` → `.Source` is the answer.
`volume` → ask `docker volume inspect` for `Options.device`. Then verify the
answer holds **with the container stopped**, because that is when it is used.
`.storage` is the marker: whichever directory directly contains it is correct.
`install.sh` now pre-flights this.

## 2. Container paths reaching host-side scripts

**Looked like:** nothing. The promoter would simply never have promoted.

**Actually:** the wizard collects the TLS CA as a path inside the container
(`/config/ca.crt`) — correctly, because that is the only filesystem the
integration can validate against. But `cluster-promoter.sh` runs `python3` on
the **host**, and `cluster-fileset-pull.sh` bind-mounts the file with
`docker run -v`, which also resolves on the host. Neither tiger has a
`/config`.

Untranslated, `load_verify_locations` raises `FileNotFoundError`, `run()`
catches it as an `OSError`, every tick exits 1, and the lease is never taken.
Worse for the pull: Docker handed a `-v` source that does not exist **creates
an empty directory** and mounts it, so TLS verifies against a directory.

**Rule:** any value that crosses from the integration to a generated host
artefact needs translating. `bundle._host_path` does it; the pre-flight checks
the result is readable from the host.

## 3. The bundle belongs to ONE node

**Looked like:** a reasonable feature request — "generate a script to copy the
bundle from host A to host B".

**Actually:** nine of the 24 files differ between nodes. `cluster-promoter.sh`
embeds `NODE_ID`; the notify scripts and the swap embed this host's config
directory and container name. Installed on the peer, the bundle makes it
present **this** node's id to the lease — and the lease renews on identity, so
both nodes renew the same one and both believe they lead. That is AR-0025's
identity collision, reached by `scp`.

INSTALL.md was actively inviting it: *"copy these to `/etc/cluster-sync` on
**both** hosts"*.

**Rule:** the shared values (namespace, database, cluster secret, TLS) travel
between nodes. The **files never do**. Each node runs its own wizard.

## 4. `restore_max_age` was applied per-entity, so a quiet cluster restored nothing

**Looked like:** on node-b, during an ordinary restart:

```
Restored NOTHING from a snapshot that held 28 entries.
Skipped: 0 local-newer, 28 too-old, ...
```

**Actually:** the cutoff compared each entry's own `last_updated`, not the
snapshot's age. `async_flush` deliberately skips when nothing has changed, so
on an idle cluster the entries age together — and after thirty minutes of
quiet, *every* entry was "too old".

This inverted the intent. The states most worth carrying across a failover are
the **stable** ones: a setpoint that has held all day, a boolean nobody has
touched since Tuesday. Those were discarded, while a sensor that flickered ten
seconds ago sailed through.

The companion warning made it worse, reporting that the peer had stopped
writing and its flush loop needed investigating — about a node that was
perfectly healthy.

**Rule:** the age gate belongs to the **snapshot**, which is what
`restore_max_age` says it is. The per-entity comparison that stays is
`existing.last_updated > entry_updated` (do not clobber fresher local state) —
a different question that was never broken.

## 5. Arming the promoter is itself a promotion

**Looked like:** installing the bundle would be a neutral act.

**Actually:** `decide()` fires on a **change** of the lease outcome, and a
fresh install has no `vrrp-state`. The first tick reads `previous == ""`,
calls the status quo a transition into it, and runs the matching notify
script. On the node that already leads that is `notify_master.sh`: a fileset
swap and a device pre-flight rewriting `.storage` underneath a Home Assistant
that never stopped.

**Rule:** `cluster_promoter.py --adopt` observes the lease with `GET`, writes
the matching state, and runs nothing. `install.sh` does this **before**
enabling the timer. Adopt never evaluates the lease script, and an unheld lease
reads `BACKUP` — a node that claims leadership because it could not establish
otherwise is the split brain.

## 6. A leader restart was a permanent failover

**Looked like:** clicking *Restart* in the leader's UI.

**Actually:** three individually-correct mechanisms cooperated to move the
cluster permanently and leave the primary stopped.

1. `_on_stop` hands the lease back so the peer need not wait out the TTL —
   right for a shutdown, wrong for a restart, and indistinguishable from
   inside.
2. The standby's `_from_lease` calls take-**or**-renew, so its own Home
   Assistant claims the freed lease within seconds and starts publishing its
   near-empty `.storage` as the cluster go-bag.
3. The leader's promoter then fails its D3 probe and demotes, which is a
   `docker stop` that `unless-stopped` will not undo, with a 900s hold-down
   behind it.

**Rule:** the maintenance hold (`hold.py`) suspends all four behaviours at
once — the fourth being the standby's promoter, which must also refuse a free
lease. Any one left armed leaves a hole big enough to lose the cluster
through, which is why they are enumerated in one module rather than each
guarding itself. `cluster-hold.sh on|off|status`.

## 7. The swap assumed the container was stopped, and never checked

**Looked like:** nothing, until a promotion.

**Actually:** the cold model's promotion path is swap → pre-flight →
`docker start`, and every step rewrites `.storage` on the premise that nothing
holds it open. On node-b that premise was false for days: its Home Assistant
had been left running after setup. A running Home Assistant holds those files
in memory and rewrites them on **its** shutdown (AR-0034), so the swap lands
and is then silently undone.

**Rule:** `cluster-fileset-swap.sh` refuses and marks itself degraded if the
container is running. D4 already accommodates a degraded promotion and records
the reason; swapping under a live process is data loss.

## 8. An unfaithful fake made a new guard a no-op

**Looked like:** every test passing, in both directions, regardless of the rule.

**Actually:** `FakeBackend.write_snapshot` never set `last_snapshot_at`, though
the real backend always does. The new snapshot-age gate read `None` and did
nothing, so the tests proved nothing at all.

**Rule:** when adding a guard that reads a field, check the fake **writes**
that field. A fake that omits it does not fail the test — it removes the test.
This project has been bitten by unfaithful stubs before (a notify-script stub
that recorded invocations without performing the write under test).

## 9. Silence is not success

**Looked like:** after fixing #4, the standby logged no restore line at all.
The tempting reading was "no warning, so it worked."

**Actually:** node-b had **no `logger:` block**, while node-a sets
`default: info`. The restore's success path logs at INFO. So on the standby —
the node where a restore is the whole point — the single most important event
in the system was unobservable.

**Rule:** before concluding from an absent log line, confirm that line *could*
have appeared. Check the node's log level. Both tigers now enable INFO for
`custom_components.cluster_state_sync`.

## 10. Home Assistant's loop protection cannot be satisfied through `redis`

**Looked like:** a warning naming our integration and inviting a bug report,
persisting after the blocking read was genuinely removed.

**Actually:** HA exempts `load_verify_locations` only when `cadata` is the
**sole** keyword argument. `redis`'s `RedisSSLContext.get` unconditionally
calls it with `cafile=` **and** `cadata=`, even when the first is `None`, and
there is no `ssl_context` parameter to pass a pre-built context instead. It
also calls `ssl.create_default_context()` on the loop, which reads the system
trust store.

**Rule:** we pass `ssl_ca_data` and no path, so OpenSSL opens nothing — the
blocking is real-world gone. The remaining warning is a false positive we
cannot silence from here; the fix belongs upstream. Recorded so nobody
re-investigates it as ours.

## 11. The frontend renders from the schema; the translations can lie

**Looked like:** a crib sheet listing fields on the wrong screen.

**Actually:** four labels for fields that had moved to another step were still
sitting in `strings.json`, and `redis_username` had **no label at all** — the
form showed the raw key on every install ever done. The frontend renders by
schema and was right; the document, written from the translations, was wrong.

**Rule:** a test asserts the schema's fields and the translation's labels match
exactly, in both directions, for every step.

## 12. A bounded integer renders as a value-less slider

**Looked like:** you could not tell what the Valkey database was set to without
grabbing the slider and moving it.

**Actually:** `vol.All(int, vol.Range(min=…, max=…))` serialises to a bare
bounded integer, which the frontend draws as a naked slider. Every field that
must match across nodes was unreadable at a glance — and a database mismatch
leaves both nodes reporting `Backend: on` while writing to different databases
and never seeing each other.

**Rule:** `NumberSelector`, with `vol.Coerce(int)` after it (a NumberSelector
validates to **float**, which would put `2.0` in the config entry) and `max`
omitted rather than `None` for open-ended fields (a null maximum raises during
serialisation and surfaces as "unknown error occurred").

## 13. Generated shell is shell, and needs a linter

**Looked like:** a plausible `find … -exec cp … ;`.

**Actually:** the unquoted `;` terminated the calling wrapper rather than the
`find`. Caught by `shellcheck`, not by review.

**Rule:** generated scripts are checked with `bash -n` in the suite, and with
`shellcheck` before shipping a generator change.

## 14. (omitted)

This entry covers access arrangements specific to the author's own hosts
and is not published. Nothing in it affects how the integration behaves.

## 15. 🚨 Never claim a VirtualHere "USB 10/100 LAN" device

**This one is not a near miss. It happened, on 2026-09-04, and it took the
owner's Zigbee coordinator off the air.**

**Looked like:** a safe test target. The device list showed six devices across
two servers; four were `In-use by` node-a and two — both `USB 10/100 LAN` —
were free. Claiming a free one looked like a way to prove the claim path
end-to-end without touching a radio.

**Actually:** those adapters are the **servers' own PoE network links**. They
were unclaimed *because they must never be claimed*. A VirtualHere claim
performs a USB device reset — visible in `dmesg`:

```
vhci_hcd: vhci_device speed not set
r8152-cfgselector 9-1: reset full-speed USB device number 2 using vhci_hcd
```

Reset the adapter a server is talking to the network through and the server
drops off the network, taking every device attached to it with it.

**Consequence:** `HouseAnt` went unreachable on both its addresses. node-a
lost the **ConBee II** (the Zigbee coordinator — every Zigbee device
unavailable) and the **RFXtrx433 `A1Z5EIPE`**. Home Assistant retried with
exponential backoff up to 320 seconds. The `OfficeAnt` hub survived, but its
`RFXtrx433 A1K33YZ` was reset as collateral and re-enumerated from `ttyUSB3` to
`ttyUSB4`. Recovery required physically power-cycling the HouseAnt unit; it did
not self-heal.

**Never touch these:**

| Address | Device | Why |
|---|---|---|
| `officeant.114` | USB 10/100 LAN | OfficeAnt's own network link |
| `ctuhouseant.114` | USB 10/100 LAN | HouseAnt's own network link |

**Rules, in order of how much they would have helped:**

1. **"Free" means "not currently in use". It does not mean "safe to use".** On
   shared hardware, ask why something is unclaimed before claiming it. The
   answer here was that claiming it breaks the thing serving it.
2. **A USB-over-IP claim is a write, not a read.** It resets the device. There
   is no read-only way to "try" a claim.
3. Enumerate with `LIST`, `DEVICE INFO` and `SERVER INFO` — all genuinely
   read-only, and all of which were available and sufficient to design the
   failover without claiming anything at all.
4. On a live fabric you did not build, the safe default is to change nothing
   and ask.

## 16. `pgrep -f <name>` matches your own shell

Twice in one session, `pgrep -f vhclientx86_64` reported the VirtualHere client
"running" on node-b when it was not: the pattern matched the `bash -c`
command line of the check itself, because that command line contained the
string being searched for.

The first misreading led to the wrong conclusion (that node-b was already
claiming devices); the second nearly did again.

**Rule:** use `pgrep -x <exact-name>` or `ps -C <name>` when asking whether a
named process is running from inside a script that names it.

## 17. A container's `/dev` is a snapshot; a re-enumerated radio never appears in it

**Looked like:** Home Assistant reporting `No such file or directory` for a path
that plainly existed on the host.

```
connect failed: could not open port
  /dev/serial/by-id/usb-RFXCOM_RFXtrx433_A1K33YZ-if00-port0:
  [Errno 2] No such file or directory
```

**Actually:** two different views of `/dev`.

```
host:      ttyUSB0  ttyUSB1  ttyUSB2  ttyUSB4
container: ttyUSB0  ttyUSB1  ttyUSB2  ttyUSB3   <- stale
```

`/dev/serial` is bind-mounted, so the **symlink** is live and correctly points
at `../../ttyUSB4`. The container's `/dev` is not: Docker populates it when the
container starts and does not track later device arrivals, even for a
privileged container. The radio had re-enumerated from `ttyUSB3` to `ttyUSB4`
after its USB-over-IP server was power-cycled, so the target node never existed
inside the container.

The by-id path is *supposed* to make enumeration order irrelevant — and it does,
right up until the thing it points at is missing on your side of the mount.

**Consequence:** a radio that drops and returns is unusable by a *running*
Home Assistant, indefinitely, with a retry backoff that reaches 600 seconds and
an error naming a file you can see with your own eyes.

**Rule:** after any USB-over-IP server restart, compare the two views:

```bash
ls /dev/ttyUSB* /dev/ttyACM*
docker exec <container> ls /dev/ttyUSB* /dev/ttyACM*
```

If they differ, restart the container — that is the only fix, and it rebuilds
`/dev` from the host. **Set the maintenance hold first**, or stopping Home
Assistant on the leader triggers a failover (see §6).

**Why promotion is not affected:** the promoter claims devices and *then* runs
`docker start`, so a promoted node's `/dev` snapshot is taken after the devices
exist. This trap only bites the node that was already running — which is to say
the leader, during normal operation.

**Measured for free while fixing it:** VirtualHere re-claimed all four
auto-share devices **24 seconds** after its server came back, with no
intervention. That is the reconnect half of the radio-failover design working
by itself.

## 18. 🚨 A Docker healthcheck says `healthy` while the process is livelocked

**Looked like:** nothing. The kitchen lights simply did not come on.

```
$ docker ps --filter name=deconz
deconz   Up 40 minutes (healthy)
```

**Actually:** deCONZ had stopped doing any work 32 minutes earlier and was
spinning on a core the whole time.

```
last log line : 19:00:17 UTC
current time  : 19:32:07 UTC     <- 32 minutes of silence
CPU           : 78%              <- 32 min CPU burned in 41 min wall
```

The only trace anywhere was one line in Home Assistant's log, repeating:

```
Reconnecting to deCONZ (192.168.1.87) failed, retrying in 15 seconds
```

The healthcheck passed because it proves the HTTP port answers, not that the
ZCL thread is still processing. Those are different threads, and only one of
them had stopped.

**Consequence:** every Zigbee device in the house was dead, with a container
reporting `healthy`, a radio correctly attached, VirtualHere connected, and
Home Assistant up and serving. Restarting the container fixed it instantly —
CPU 78% → 5%, Home Assistant reconnected within 10 seconds.

**The failover design would not have caught this, and still will not.**
The leader passed its D3 HTTP probe throughout, because Home Assistant *was*
healthy; it was a downstream radio daemon that had died. Promotion is gated on
the probe, so no amount of radio outage triggers one. This is the AR-0040 shape
in new clothing: green everywhere, and the house did nothing.

**Rule:** never read `(healthy)` as evidence that a container is working. For
any daemon that is supposed to be chatty, liveness is *recent log output*, not
an open port:

```bash
docker logs --since 5m <container> | wc -l   # silence: necessary, NOT sufficient
docker top <container>                       # is it burning CPU while silent?
```

**Silence alone is not the tell, and saying so cost nothing but nearly cost a
false alarm an hour later.** The same container, healthy, logged **0 lines in
60 seconds** simply because the house was asleep and no Zigbee device had
anything to say. What separates the two cases is a second, independent signal:

| logs | CPU | consumer's view | reading |
|---|---|---|---|
| silent | ~78% | reconnecting | **livelocked** — restart it |
| silent | ~1% | connected | idle, and fine |
| flowing | any | connected | working |

Always pair the silence with CPU *and* with what the consumer thinks — here,
whether Home Assistant's websocket is connected:

```bash
docker logs --since 20m homeassistant 2>&1 | grep -c "Reconnecting to deCONZ"
```

**Suspected trigger, unproven:** one Zigbee device was flooding — 398 of the
last 2000 log lines came from `0x70AC08FFFE6900A1` (cluster `0x0102`, window
covering), reporting roughly once per second, indefinitely. Two other devices
managed 36 and 11 between them.

**Since v0.3.1 the integration measures this.** The wizard's *Entities that
prove a radio is receiving* field takes globs — on this fleet
`sensor.*_rssi_numeric`, 39 entities that update on every RFXtrx packet
received — and produces **`sensor.<node>_radio_silence`**: seconds since the
freshest of them last changed. Freshest, not oldest, deliberately: one still
reporting means the path is alive, and taking the oldest would alarm on the one
dead battery sensor in a working house.

Three things it deliberately does **not** do:

- **It does not gate promotion.** Failing over because a radio died would move
  the house onto a node whose radios may be no better — and on this fleet, whose
  deCONZ is not running at all. It makes the silence visible; the operator
  decides.
- **It does not decide what "too long" is.** Only the operator knows their own
  traffic. A quiet house at 4am legitimately produces no RF for a long while.
  What is diagnostic is a number that *used* to move and has stopped.
- **It does not report `0` when it matches nothing.** A typo in the glob, a
  renamed entity, an integration that failed to load — each leaves it watching
  nothing, and `0` would render as "heard something just now". It reports
  `unknown`, and `entities_matched` in the attributes is the number that catches
  the silent misconfiguration.

🚨 **Watch ONE radio's signals per list.** Freshest-wins is right within a radio
and wrong across radios. On this fleet the tempting wider glob `sensor.*_rssi`
sweeps in a WLED, two Sonoffs and a Konnected — **Wi-Fi** RSSI sensors — beside
the 39 RFXtrx ones:

| glob | matches | what it measures |
|---|---|---|
| `sensor.*_rssi_numeric` | 39, all `rfxtrx` | the RF radio |
| `sensor.*_rssi` | 4 — `wled`, `sonoff`×2, `esphome` | Wi-Fi, i.e. nothing about RF |

A Wi-Fi chip reporting every 60 seconds holds the number near zero through a
completely dead RFXtrx. **Watching more entities looks safer and is the exact
opposite.** Nothing in the code can catch this — only the operator knows which
entity belongs to which radio — so it is pinned by a test
(`test_a_chatty_wifi_chip_would_mask_a_dead_rf_radio`) rather than a guard.

**And it keys on `last_reported`, not `last_changed`.** The question is "did we
hear a packet", and a radio re-reporting the same RSSI *has* been heard from —
`last_changed` does not move for an identical value, so a radio in continuous,
healthy reception would show a steadily rising silence. A false alarm costs the
sensor exactly the trust it exists to earn.

On this fleet the two agree — **0 of 37 entities diverge**, checked
2026-09-07 — which is a property of one integration on one installation and not
of Home Assistant. That is the reason to check rather than to assume, and it is
pinned by `test_a_radio_repeating_itself_is_not_a_silent_radio`, verified to
fail against `last_changed`.

**It reads ~0 for a while after every Home Assistant restart, and that number
means nothing.** Home Assistant writes every entity's state as it starts, so
`last_reported` for all 37 is the boot time and the sensor reports near-zero
silence whether or not a single packet has been received since. Observed
directly: 0 s at 22 seconds after a restart, on a node whose radios had not yet
sent anything.

Consequence for any alerting built on this: **the sensor is blind for the first
few minutes of an instance's life**, and an alert with a threshold shorter than
that window will never fire during it. Do not read a low value straight after a
restart as evidence the radios came back — that is §9 again. Use the fd count
instead, which is a fact rather than an inference:

```bash
PID=$(docker exec homeassistant sh -c 'pgrep -f "python.*homeassistant" | head -1')
docker exec homeassistant sh -c "ls -l /proc/$PID/fd" | grep -c ttyUSB   # expect 3 here
```

(and §19: `docker inspect .State.Pid` gives you the container's init, not this.)

### 18a. The sensor's first finding was not a finding, and I published it twice

**Retracted.** This section previously said the RFXtrx receivers had been deaf
for at least seven days, and then that VirtualHere had been exonerated as the
cause. There was no outage. Both write-ups are kept in the git history; what
follows is what the data actually said.

**What I measured, and what was wrong with it.** I queried Home Assistant's
history for two entities — `sensor.temp_inside_temperature` and
`sensor.radiator_aircon_rssi_numeric` — found zero real values in seven days,
and generalised to all 37 watched entities. Those two are long-dead battery
devices. Sampling two and concluding about thirty-seven is not a measurement.

**What the recorder actually holds**, queried properly over `event.*` entities,
whose state is a timestamp that advances only on a genuinely received packet
and which do *not* restore on restart:

```
last received packet : 2026-09-07 06:22:10 UTC  -> event.kitchen_middle
gap at time of writing:  8.9 h and still open
longest gaps in the previous 4 days:
     9.3h   ending 09-06 08:41 UTC
     7.5h   ending 09-05 07:53 UTC
     7.3h   ending 09-07 05:41 UTC
```

**8.9 hours against a 4-day maximum of 9.3 hours.** The current gap is inside
the normal range. Nothing is established as broken. The radios were receiving
continuously for the whole recorder window and may be receiving now — the house
simply has not transmitted since breakfast.

#### The real defect is the watch set, and it is instructive

Every `sensor.*_rssi_numeric` on this fleet belongs to a **switch, remote or
cover** — devices that transmit when a person operates them. Not one 433 MHz
device here reports on a schedule; the Oregon sensors that would have are dead.
Ranked by how many distinct hours they appear in, the busiest entity covers 35
hours out of 96.

So on this house the sensor is not measuring radio liveness. **It is measuring
human activity.** After any restart every watched entity is `unknown` until
somebody presses something, which the baseline says can be nine hours — so
`no_reports` after a restart is the expected reading, not a diagnosis.

`sensor.*_rssi_numeric` is the wrong watch set here, and the sensor's premise —
"time since last packet" — needs a device that transmits *unprompted*: an
Oregon or similar periodic sensor, a Zigbee LQI or last-seen, a coordinator's
own diagnostic. **Where nothing transmits on a schedule, this measurement
cannot distinguish a dead radio from a quiet house, and configuring it anyway
produces a red light every morning.**

#### The lesson is the one this file exists for, aimed the other way

§18 and AR-0040 are about reading absence as success. This was absence read as
*failure* — the same error with the sign flipped, and it cost an unnecessary
container restart, two VirtualHere reclaims on a live house, and two confident
write-ups of an outage that never happened.

**Before calling silence a fault, measure the baseline.** One query for the
longest normal gap would have stopped this at the first step, and it was the
same query I eventually ran. §18's own text says a quiet house at 4am
legitimately produces no RF for a long while. I wrote that sentence and then
did not apply it.

The one-second test that settles it, and which no amount of log analysis
replaces: **press a switch and see whether the event fires.**

## 18b. 🚨 `STOP USING` silently clears a device's Auto-Use flag

**Looked like:** a clean unclaim and reclaim. Both commands returned `OK`, and
`LIST` showed the devices `In-use by you` again.

**Actually:** the `*` was gone.

```
before:   *--> RFXtrx433 (officeant.113) (In-use by you)     <- * = Auto-Use
after:     --> RFXtrx433 (officeant.113) (In-use by you)     <- claimed, NOT auto
```

Identical in every respect an operator checks — claimed, present, working —
except the one that matters on the day the cluster fails over. **Auto-Use is
what the promotion hooks rely on to take custody of a radio.** Without it the
device is claimed by *this* node and will not be claimed by the peer, so the
first failover after any manual unclaim comes up with no radios and no error
explaining why.

**Rule: after any `STOP USING`, re-arm Auto-Use before you do anything else.**

```bash
vhclientx86_64 -t "STOP USING,officeant.113"
vhclientx86_64 -t "USE,officeant.113"
vhclientx86_64 -t "AUTO USE DEVICE,officeant.113"   # <- the step that is easy to miss
vhclientx86_64 -t "LIST" | grep RFXtrx              # confirm the * came back
```

Verify with the `*`, not with `In-use by you` — the second is true in both the
working and the broken case.

🚨 And never re-arm, claim, or unclaim the **USB 10/100 LAN** devices
(`officeant.114`, `ctuhouseant.114`). See §15 — that interface is PoE and
carries the network the VirtualHere server itself is reached over.

**The shipped example hooks do not have this bug, and that is deliberate.**
`examples/hardware-custody/virtualhere-release.sh` stops the whole
`virtualhereclient.service` rather than issuing `STOP USING` per device, so the
claims drop without any per-device flag being rewritten and Auto-Use survives.
The trap is the manual command an operator reaches for, not the automation —
which is the harder kind to notice, because the automation keeps working and
teaches you the operation is safe.

## 18c. §17 confirmed live: a reclaimed radio came back on a different node

Reclaiming the Office RFXtrx over VirtualHere moved it, and the container could
not follow:

```
before unclaim   host: ...A1K33YZ... -> ttyUSB4    container /dev: 0 1 2 4   ✅
after reclaim    host: ...A1K33YZ... -> ttyUSB3    container /dev: 0 1 2 4   ❌
```

The by-id symlink is correct and live — `/dev/serial` is bind-mounted, so it
tracks the host — but it now points at `/dev/ttyUSB3`, and **the container's
`/dev` is a snapshot taken when the container started**, holding 0, 1, 2 and 4.
The link resolves to a node that does not exist inside the container, and every
open fails `Errno 2`.

Linux hands out the lowest free number, so a reclaim usually *does* return the
same one — which is exactly why this is a trap rather than a rule. It worked
for the House radio in the same operation and failed for the Office one.

**A container restart is the fix and there is no lighter one.** Reloading the
config entry re-reads the same absent path. Set the maintenance hold first if
the node is the leader: the restart is a failover otherwise.

## 18d. 🚨 A partial failure promotes a standby with no radios

**Measured 2026-09-07**, while timing cold-boot RTO on the real pair.

Killing Home Assistant and stopping the promoter on the leader looks like a
dead node from the lease's point of view — the peer promoted in 78.8s, well
inside budget. It came up with **no radios at all**:

```
19:58:37  timed out waiting for: usb-RFXCOM_RFXtrx433 usb-dresden_elektronik
19:58:37  Attached serial devices: 0
19:58:37  3 config entr(ies) reference absent hardware -> Disabled 3
```

The pre-flight did the right thing — disabling entries whose hardware is absent
is what lets Home Assistant start at all — but the house had nothing to talk to.

**The cause is the failure mode, not the code.** The promotion hook
`10-virtualhere-claim.sh` waits `DEADLINE=45s` for the radios to appear. They
never did, because **the "failed" node was still running its VirtualHere
client** and still holding its USB claims. Nothing reaps a claim from a client
that is alive.

Proven by finishing the simulation: stopping `virtualhereclient.service` on the
failed node had the peer claim the radios via Auto-Use in **~12 seconds**.

> 🚨 **CORRECTED, same day.** This first said radios do not follow when "Home Assistant dies, host
> alive". Wrong, and wrong the same way as §18a — generalised from a simulation that did not
> reproduce the case it claimed to. The simulation stopped the **promoter** too, and that is what
> prevented the release.
>
> Verified by reading the installed chain rather than inferring it: `cluster_promoter.py:661` runs
> `notify_backup.sh` on demotion → it stops the container, then runs `post-stop.d/` →
> `90-virtualhere-release.sh` stops the USB/IP client. **That hook is installed on both nodes.**

| Failure | What releases the claims | Radios follow? |
|---|---|---|
| Host loss / power cut | the server **reaps a dead client** (~17s) | ✅ inside the 45s deadline |
| Home Assistant dies, host alive | the node's **own demote path**, after the 600s grace | ✅ **~10 min late** |
| **Promoter dies, host and USB/IP client alive** | **nothing** | ❌ the real gap |

So the case a cluster most obviously exists for — one service dying while the
machine stays up — is exactly the case where the radios do not move. A longer
deadline does not fix it; the claims are held indefinitely, not slowly.

**Unfixed.** Three candidate approaches, none built: have the demote path
force-release the claims; have the promoting node ask the VirtualHere *server*
to reap a named claim; or accept it and document that partial failures need an
operator. See TODO.

**Do not "fix" this by lengthening `DEADLINE`.** It would convert a fast,
honest, radio-less promotion into a slow one that is still radio-less.

## 18e. 🚨 A single-connection integration flaps when two nodes run it

**Looked like:** a kitchen light turning itself on, repeatedly, including twice after midnight.
Reported as a ghost. Home Assistant was blamed, then the radios, then the cluster.

**Actually:** none of those. The context triple settles it — six of the last ten activations of
`light.cooker_new` had **no user, no parent, and shared context with nothing**, which is
device-originated. One was the genuine automation (context shared with
`automation.lights_main_kitchen_lights_on_2` and its three sibling lights); three were a **person**
pressing it three times in four seconds, which is what fighting an unresponsive light looks like.

**The number that matters is the state histogram:**

```
light.cooker_new, 202 recorded states
   unavailable : 153      <- offline most of the time
   off         :  31
   on          :  18      <- five of these arrive 1-3s after reconnecting
   platform    : localtuya
```

**LocalTuya permits exactly one local connection per device.** So does the ESPHome native API, and
so do some Z-Wave sticks. Where a *cloud* integration degrades politely when two nodes both
connect, these **actively evict each other** — and a device that is being kicked off and
reconnecting will report whatever state it powers up in.

**Which makes this a cluster hazard class, not a device fault.** The README's original wording —
"both nodes will try to connect to the same Hue bridge, Sonos, Plex" — understates it, because
those degrade. These do not.

**But do not blame the cluster before checking the dates.** On this fleet the flapping was:

```
08-31  1     09-04  60   <- failover tests
09-01  1     09-05  69   <- a 127-restart loop, and a duplicated localtuya component (§20)
09-02  2     09-06   6
09-03  3     09-07  11   <- more testing
```

**84% of all disconnects fell on the two days of restart storms and failover testing** — our own
work, not an ongoing two-node fight. The standby's Home Assistant had run for *minutes*. The
tempting story (both nodes competing) was checked against the timeline and did not survive it.

**Rules:**

1. Before calling a device haunted, read the **context triple** on its state changes. `user_id`
   names a person; `parent_id` names an automation; neither means the device said so itself.
2. Keep the overlap window short if you run a single-connection integration. It is genuinely
   destructive there, not merely untidy.
3. A device that reconnects into an unexpected state is reporting, not being commanded. The fix is
   at the device (power-on behaviour) or wherever else can reach it — a cloud scene still runs even
   when the cloud integration is disabled in Home Assistant.

## 19. `docker inspect .State.Pid` is the container's init, not your application

**Looked like:** Home Assistant holding no serial ports at all, which would have
meant all three RFXtrx radios were disconnected.

```bash
P=$(docker inspect -f '{{.State.Pid}}' homeassistant)   # -> 1535119
sudo readlink /proc/$P/fd/* | grep tty                  # -> nothing, 7 fds total
```

**Actually:** that PID is the container's `init`/supervisor. The application is
a child of it, and it held every radio:

```
/dev/ttyUSB0  <- pid 1535202  python3
/dev/ttyUSB2  <- pid 1535202  python3
/dev/ttyUSB4  <- pid 1535202  python3
/dev/ttyACM0  <- pid 2010552  deCONZ
```

Seven file descriptors should have been the tell: a running Home Assistant has
hundreds.

**Consequence:** a diagnosis exactly inverted from the truth — "the radios are
all disconnected" when all three were open and receiving. Acting on that would
have meant restarting Home Assistant on the leader to fix a problem that did
not exist, which on a node holding the lease means an outage (§6).

**Rule:** to find which process holds a device, ask the device, not the
container. `fuser`/`lsof` are usually absent on these hosts, so scan `/proc`:

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

This also answers "is anything *else* holding my radio", which the container
view cannot.

## 20. Two `custom_components/` directories, one domain — the broken copy wins

**Looked like:** kitchen lights that would not come on, through eighteen hours of
radio debugging. The switch transmitted, the radio decoded it, the automation
fired. Then:

```
Referenced entities light.cooker_new, light.kitchen_sink_3,
light.left_front_shelf, light.front_cabinet_right_2,
light.right_cabinet_shelves_back_ty, light.right_cabinet_shelves_middle_ty
are missing or not currently available
```

**Actually:** two integrations claiming the same domain.

```
custom_components/localtuya       -> "domain": "localtuya"
custom_components/localtuya old   -> "domain": "localtuya"     <- stale copy
```

Home Assistant scans `custom_components/` by directory, not by name, and a
directory called `localtuya old` is a perfectly ordinary candidate. Both
manifests declared `localtuya`; the stale one shadowed the working one. That
boot: **0 `localtuya: Setup completed`, 16 errors from the old copy.** The
integration never loaded, so its entities never existed, so every automation
targeting them fired into nothing.

**Consequence:** an integration that is enabled, not disabled, has a valid
config entry, and is *present on disk* — yet has no entities. The config entry
looks healthy in `.storage`. Nothing says "you have two of these".

**Rule:** a "keep a copy before upgrading" directory must live **outside**
`custom_components/`. Renaming it is not enough; the name is not what
identifies an integration, the manifest's `domain` is. To audit:

```bash
for d in custom_components/*/; do
  printf '%s -> ' "$d"
  grep -o '"domain": *"[^"]*"' "$d/manifest.json" 2>/dev/null || echo '(none)'
done | sort -t'>' -k2 | uniq -d -f1
```

**Why it matters here:** the go-bag replicates `custom_components/`. A shadowing
directory does not just break one node — it is faithfully copied to the peer,
so failing over does not escape it.

## 21. `RestartCount=0` with a moving uptime means the process, not the container

**Looked like:** Home Assistant "restarting" while Docker insisted it had not.
Read as Docker being unreliable, and it cost most of a day.

```
docker inspect -f '{{.RestartCount}} {{.State.ExitCode}}'  ->  0 0
   ...while HA's own log showed startup after startup
```

**Actually:** `homeassistant.restart` exits the Python process; the container's
supervisor starts it again *inside the same container*. The container never
restarted, so `RestartCount` never moved and `StartedAt` never changed. Only
Home Assistant's own log counts these:

```bash
docker logs <container> | grep -c "Starting Home Assistant"
```

Measured on this fleet: **127 core starts in 62 minutes**, one every ~35
seconds, against `RestartCount=0`.

**Consequence:** every fault below the loop is invisible and every symptom
looks like the thing you happen to be staring at. That restart loop was blamed
in turn on deCONZ, on VirtualHere, on pyRFXtrx's reset timing, and on a failing
wall switch — all wrong, all disproved later, and none of it reachable while
the loop ran.

**Rule:** when uptime resets but `RestartCount` does not, stop and count core
starts before diagnosing anything else. A restarting instance cannot be
debugged; fix the loop first, then look again — the real fault will still be
there, and it will finally be visible.

**The loop's own cause is worth recording too.** An automation triggered on
`homeassistant` `event: start` whose first action was `homeassistant.restart`.
Its guarding condition sat *after* the restart, so it only ever gated the
notification. Ordering a condition after the action it is meant to guard is not
a subtle bug, but it is invisible until something reads the automation.

## 22. Trust the address a proxy routes FROM, not the one it answers on

**Looked like:** a new Traefik front end returning **HTTP 400** for every remote
request, while the same host served every other site it fronts perfectly.

```
ERROR [homeassistant.components.http.forwarded]
      Received X-Forwarded-For header from an untrusted proxy 192.168.1.164
```

**Actually:** the proxy host is multi-homed. It answers on `192.168.1.86` —
the address in DNS, in the tunnel config, in every diagram — and reaches the
Home Assistant nodes from a *second* address on the same subnet:

```bash
$ ip route get 192.168.1.87
192.168.1.87 dev eno1 src 192.168.1.164
```

`trusted_proxies` is matched against the **source** address of the connection,
so trusting `.86` trusts an address that never appears.

**Consequence:** it fails only for remote clients, only through the new path,
and with a status code that suggests a malformed request rather than a policy
refusal. Local access is unaffected, so it looks like a networking fault.

**Rule:** before adding any proxy to `trusted_proxies`, ask the route:

```bash
ip route get <ha-node-ip>     # trust the `src` address
```

List every address the proxy could route from. A routing change that alters
the source address breaks remote access silently and only remotely.

## 23. A Traefik entry point that does not exist is not an error

**Looked like:** a correct-looking router returning 404 for its own hostname,
with **nothing** in Traefik's log — no error, no warning, no rejected config.

**Actually:** the entry point was named `websecure`, and on that host the HTTPS
entry point is called `https`. Traefik accepts a router referencing an unknown
entry point, loads the file happily, and simply never binds it.

Entry point names are per-installation. On this fleet:

| host | HTTPS entry point |
|---|---|
| node-a | `websecure` |
| node-nas | `https` |

Copying a working rule between hosts is exactly how this happens.

**Two neighbours with the same shape:**

* `{{env "DOMAINNAME0"}}` unset renders ``Host(`home.`)`` — a valid rule that
  matches nothing, forever. Check whether the host's Traefik actually sets the
  variable before using a template that worked elsewhere.
* A health check against another *Traefik* needs `hostname:` set. Without a Host
  header the probe hits the peer's default router, 404s, and marks a healthy
  backend DOWN — silently pinning traffic to the fallback.

**Rule:** confirm the entry point name from the running config before writing a
rule for a host you have not written one for before:

```bash
docker inspect traefik --format '{{json .Config.Cmd}}' \
  | tr ',' '\n' | grep -i 'entryPoints\..*address'
```

**And test with `--resolve`, never `-H "Host:"`.** Over TLS, Traefik routes on
SNI; a Host header against an IP sends no SNI and yields a 404 that means
nothing:

```bash
curl -k --resolve host.example.com:443:127.0.0.1 https://host.example.com/
```

## 24. A corrected file, served correctly, that the browser never receives

**Looked like:** a bug in freshly-deployed JavaScript. The panel rendered its
headings and every row was missing.

**Actually:** three separate caches, discovered one at a time, none of which
showed up in any server-side check.

```
on the server:   curl 127.0.0.1:8123/...panel.js   -> NEW file, verified 3x
in the browser:  page renders values only the OLD parser could produce
```

The give-away was in a screenshot: a node heading reading `node-a` where the
corrected parser yields `node-a_f5d04f`. **The build that is running
identifies itself, if you look for something only one version could produce.**

Three layers, in the order they bit:

1. **The browser's ES module cache.** A hard refresh frequently does not clear
   it. Incognito ruled this out — and the symptom persisted, which is what
   pointed further out.
2. **An edge cache** (Cloudflare, in front of this fleet). Confirmed by
   appending `?v=2`: the new file appeared instantly. That also proves the
   cache is keyed on the full URL.
3. **`customElements.define` throwing.** Once the URL finally changed, Home
   Assistant imported a *second* module defining an element name the page had
   already registered. It threw `NotSupportedError`, the frontend caught it,
   and reported `Unable to load custom panel` — which reads exactly like a
   download failure.

**Rule:** when a deployed fix does not appear, establish **which build is
running** before debugging the code. Ask for something only one version could
produce; a value on screen beats any number of `curl`s from the server, because
`curl` from the server tests the one hop that was never in question.

Then fix it at the source rather than by clearing caches:

* Put a **content hash in the URL** (`?v=<sha256[:10]>`). A changed file becomes
  a URL nothing has ever cached. `cache_headers=False` alone is not enough.
* **Guard `customElements.define`** with `customElements.get(...)`. Home
  Assistant is a single-page app, so any module whose URL changes gets imported
  alongside the one already loaded.
* **Syntax-check shipped JavaScript in CI.** A panel that fails to parse and a
  panel that fails to download produce the identical message. `esprima` is a
  pure-python parser and needs no JS runtime.

## 25. A statistics database that opens perfectly, and is not a recorder

`statistics_meta` and `statistics` are the only tables long-term statistics live in, so the
obvious seed is a file containing those two tables. That file opens in SQLite, answers every
query you throw at it, and holds every row you wanted.

Home Assistant's recorder queries **thirteen** tables on startup. Given a file missing eleven of
them it does not report a bad seed — it attempts a migration, or the recorder dies. A house whose
recorder is down looks entirely normal until somebody opens a graph.

**Rule:** a seed carries the **whole schema** and only the rows worth carrying. Copy every
`CREATE TABLE` and `CREATE INDEX` from `sqlite_master` (skipping `sqlite_%`, which SQLite owns and
rejects), then populate only what you mean to. The check that proves it is not "does it open" but
**"does its schema match the source's, table for table and index for index"** — measured on the
live pair: 13/13 tables, 20/20 indexes, and the recorder in the running image reporting schema 53,
current, no migration needed.

Two related traps in the same area:

* **`statistics.metadata_id` is local to each database.** Copying rows with their numeric ids
  attaches one node's readings to another node's sensors. Everything must key on `statistic_id` —
  the stable string — and resolve the number on arrival. The resulting graph looks fine and is
  wrong, which is the worst failure shape there is.
* **A half-copied 484 MB file is a valid SQLite database.** It opens, and it is missing history.
  `PRAGMA quick_check` catches the structural damage a truncated copy leaves, without spending
  minutes re-reading every index the way `integrity_check` does.

## 26. A replica that is healthy, current, and permanently missing a year

A rolling window is the right shape for a cold standby: republish the last N days every pass,
`INSERT OR IGNORE` on arrival, and a standby that has been off for a fortnight catches up from one
fetch. The failure is what happens when it has been off for *longer* than N days.

It fetches the window. It applies every row. Every row it applied is correct. Its newest row is
today's. It reports success, and the stretch between where it stopped and where the window begins
exists on neither node — no later window will ever contain it, because later windows only move
forward.

**Rule:** a windowed payload must state **the floor it covers**, not just the rows it carries.
The receiver compares that floor against its own newest row and says so when there is a hole.
Without it, "nothing new" and "a year is missing" are the same observation.

## 27. A blank panel that is nobody's bug, because an auth proxy answered instead

The cluster panel went blank — a black page, no error visible. A panel had just
been changed, so the change was the obvious suspect. It was not.

Everything server-side was correct, and all of it was checked: the file parsed
as an ES module, `customElements.define` was present with the right name, the
integration still registered the panel, Home Assistant served it on
`127.0.0.1:8123` as `200 text/javascript`, and the content hash in the module
URL matched the deployed file exactly.

**The request never reached Home Assistant.** An authenticating reverse proxy
sits in front, and a request whose session it does not accept gets a `302` to a
login page. The browser receives HTML where it asked for a module, so
`customElements.define` never runs, the element is never defined, and Home
Assistant renders an empty panel. On a dark theme that is a black page.

It resolved on a hard reload.

**Rule:** a blank panel has three distinct causes that look identical —
a module that fails to parse, a module that never downloads, and **a module
that downloads something that is not JavaScript**. Before suspecting the code:

* Fetch the module **from inside the host**, bypassing the proxy. A `200
  text/javascript` there and a blank page in the browser means the proxy or the
  session, not the file.
* Check the **status code and Content-Type the browser got**, not the one the
  origin serves. A `302` to an HTML login page is the signature.
* Remember the panel is a **subresource**. The SPA around it can be perfectly
  authenticated while one module request is not — which is why the rest of Home
  Assistant looks fine.

Related: §24 is the same shape with a different cause (a cached stale module).
The content hash fixed that one and cannot fix this one, because the URL is
new — it is the *answer* that is wrong, not the request.

## 28. Restarting Home Assistant on the leader is a failover

A deploy on 2026-09-10 moved the house without meaning to, and the sequence is
worth having in front of you because every step of it is correct behaviour:

```
16:27:14  docker restart homeassistant   (leader, node-a)
16:27:24  promoter tick — cannot reach HA — RELEASES the lease,
          drops 4 VirtualHere radios, writes release-holddown (900s)
16:27:27  the restart command returns.  Three seconds too late.
~16:28    node-b takes the free lease, promotes, claims the radios
```

**The promoter probes Home Assistant every 10 seconds and demotes when it cannot
reach it.** That is the entire point of it — an HA that has died must not keep
the lease. It has no way to distinguish "died" from "you are restarting me on
purpose", and it should not guess.

🚨 **So the maintenance hold is not optional politeness before a leader-side
restart — it is the only thing standing between a deploy and a failover.**
`/etc/cluster-sync/cluster-hold.sh`, or the hold switch in the panel. Earlier
deploys in that same session set the hold and stayed MASTER; the one that
skipped it did not.

**Two things make this worse than a normal failover:**

* **The demoted node's HA can come back up as a BACKUP.** `docker restart`
  brought Home Assistant back *after* the demotion had run, so both instances
  were live — the double-automation-firing window, held open indefinitely rather
  than for the usual few seconds.
* **node-a owns the Local RFXTRX and it never fails over.** That is the
  transceiver the firewall RF-recovery watchdog uses, so an accidental promotion
  of node-b silently removes the fleet's last-resort firewall recovery path.

### The failback, which is a documented path and takes about 45 seconds

Order matters, and getting it backwards leaves the cluster with no eligible node:

1. **Demoted node first:** `cluster-promoter.sh --adopt` — clears its
   `release-holddown` and records its role, running no notify script.
2. **Current leader:** write a reason into
   `<config>/.cluster_sync_handover_request`. Its next tick releases the lease,
   stops HA there, and consumes the request. A handover deliberately does *not*
   depend on Home Assistant being unwell — it is the one release that happens
   while everything is working.
3. The target takes the free lease on its next tick and promotes; radios follow.
4. **`--adopt` on the node that handed over**, or its own 900s hold-down leaves
   the cluster with no standby.

Related: §the hold exists for exactly this, and ADR-006 on why the lease — not a
second election — decides who is leader.

## 29. 🚨 A held node that reboots returns as a second live instance

**Looked like:** a clean planned reboot. The hold was set from the dashboard,
the promoter acknowledged it every 10 seconds, node-b promoted on cue, and
node-a came back. Both then answered on `:8123` for four hours.

**Actually:** observed 2026-09-18. Every step is correct behaviour, and the
hold is the thing that makes it unrecoverable:

```
09:05:16  hold written on node-a from the dashboard
          → the promoter's held branch runs RENEW_ONLY, then `return 0`
          → it never reaches decide(), so it never demotes, so
            notify_backup.sh never runs, so `docker stop` never runs
10:48     node-a reboots. Renewal stops. The lease lapses at its 30s TTL.
10:49:16  node-b (no hold) finds it free, takes it, promotes
10:51:27  node-a boots → /run is tmpfs → systemd-tmpfiles writes BACKUP
10:53:40  Docker's `unless-stopped` restarts homeassistant — nothing had ever
          explicitly stopped it, so the policy correctly restores it
10:53:41+ the promoter resumes, still held: previous="BACKUP",
          holds_lease=False → decide() returns None → no transition
```

**The hold did not merely fail to stop the container — it prevented the
demotion that would have.** §28 reaches two live instances through a leader
restart; this reaches the same place through a *hold plus a reboot*, and unlike
§28 it cannot self-heal.

🚨 **Removing the hold does not stop the returned node's Home Assistant.**
`decide()` acts on a *change*, and after the reboot there is none left to make:
`/run/cluster-sync/vrrp-state` is tmpfs, `cluster-sync-tmpfiles.conf` resets it
to `BACKUP` at boot, and so the promoter believes the node had already demoted.
The state file and the container have diverged, and nothing reconciles them.
`BACKUP-but-running` is a third state in a check that only has two.

**The hold is per-node, and only the shell path says so.** `cluster-hold.sh`
carries the warning — *"🚨 Set it on BOTH nodes before planned work"* — but the
panel switch does not, `binary_sensor.maintenance_hold` has no description, and
the two paths are not the same file:

| node | hold path |
|---|---|
| node-a | `/mnt/docker_data/homeassistant/`**`config/`**`.cluster_sync_hold` |
| node-b | `/mnt/docker_data/homeassistant/.cluster_sync_hold` |

The promoter then prints fleet-wide phrasing for a node-local fact — *"Failover
is SUSPENDED until it is removed"* — when it is suspended on **that node only**.
`hold.py` and §6 both say the hold suspends four behaviours "at once", and the
fourth is *the standby's promoter refusing a free lease*, which a file on the
leader cannot reach.

**Radio custody then splits, silently.** `post-stop.d/90-virtualhere-release.sh`
runs only on an orderly demotion, so the returned node's VirtualHere client is
never stopped. Both clients run with byte-identical `[AutoShare]` rules and
custody divides between them — observed here as the ConBee II migrating *back*
to the returned node at 12:36, hours after the live node claimed it at
10:49:16. Neither node reports this.

**What the hold does still protect.** The returned node reads as a *follower*:
`leadership.py` routes a held node through `renew_leadership` rather than
`acquire_leadership`, and the snapshot flush is leader-gated, so it does not
publish over the live node's go-bag. Its fileset pull writes only to
`.cluster_sync_staged/`. The damage is confined to radios and automations —
`gate_automations` defaults to off by design, so both instances run their
automation engines. *(Inferred from configuration and a live `200`; neither
logbook was read.)*

**Rule (corrected 2026-09-18, and the correction matters):** the original rule
here said *"set the hold on both nodes"*. **That instruction could not be
followed.** In the cold model only one node runs Home Assistant — the standby's
is deliberately stopped — so the dashboard the operator reaches is the only one
in existence, and there is no second switch to press. The only route to the
peer was `cluster-hold.sh` on the host, which is the host-side path this
project pushes work away from. Blaming the operator for a one-sided hold was
blaming them for the mechanism.

**Fixed in 0.6.0:** the hold now has two halves — the local file, which keeps
working when the store does not, and a key in the shared store, which reaches
the peer. Either suspends failover; setting or clearing from either node does
both. The promoter names the scope and the origin: *"CLUSTER-WIDE maintenance
hold, set by node-a"* rather than the old node-local message that read as a
cluster-wide guarantee it could not give.

🚨 **Both nodes must be on 0.6.0 or later.** A node on an older version neither
publishes nor reads the shared key, so a mixed pair silently behaves exactly as
described above.

**The second half, also fixed in 0.6.0 — the reconcile.** A node that rebooted
under a hold came back with its container running and its state file reset to
`BACKUP`, and the promoter had no transition to act on, so nothing ever stopped
it. `decide()` acts on a *change*; there was none left to make. The promoter now
detects `BACKUP`-but-serving and runs the demotion that never happened —
expressed as a transition into `BACKUP` so it goes through `notify_backup.sh`
and its post-stop hooks, rather than a second, separately-maintained way to
demote.

🚨 **Cold deployments only, and the condition is the whole safety of it.** On a
warm standby a running Home Assistant is the *correct* state — that is what warm
means — so firing there would stop the standby's house on every tick and turn
the fix into the outage. `bundle.py` emits `--reconcile-backup` from the
deployment's own topology; the promoter never infers it.

Three more conditions, each load-bearing: the lease must not be ours (a node
taking the lease this tick is *promoting*, and its container should be running);
the state file must be older than 180s (`notify_backup.sh` writes `BACKUP`
*before* `docker stop -t 120`, so a demotion in flight looks identical for two
minutes); and an unanswerable `docker inspect` is never treated as a fault.

**The escape hatch is the hold.** To run Home Assistant on a standby
deliberately, set the hold — the held branch returns long before the reconcile
is reached.

### The failback from this state, which §28's four steps do not cover

§28 assumes the demoted node released and its Home Assistant is down. Neither
holds here: there is no `release-holddown` on either node, because the hold
prevented the release that writes it — and the returned node's container is
**running**, which makes `cluster-fileset-swap.sh` refuse (§7) and mark
`container_running`. Promoting it in that state is a degraded promotion onto
its own stale `.storage`, discarding everything the live node has done since.

So stop Home Assistant *before* promoting it, not after:

1. `docker stop` on the returned node — the demotion that never ran. This is
   what reconciles the state file with reality and closes the double-firing
   window.
2. Stop its `virtualhereclient.service`, so it releases the devices it has
   re-claimed.
3. Clear the hold on it, and confirm the peer has none.
4. **Current leader:** write a reason into `<config>/.cluster_sync_handover_request`.
   Its next tick releases the lease, stops HA there, and consumes the request.
5. The target takes the free lease on its next tick and now swaps cleanly.
6. **`--adopt` on the node that handed over**, per §28 step 4, or its own
   hold-down leaves the cluster with no standby.

Stopping at steps 1–2 is a legitimate resting place: it ends the split and
leaves the cluster on the current leader, at the cost of any node-local
capability until you do fail back (§18d, and the Local RFXTRX in §28).

Related: §28 is the same destination by a different route; §6 for the four
behaviours the hold suspends; §7 for why the swap refuses under a live process;
§17 and §18c for radios re-claimed onto the wrong node.

## 30. 🚨 `install.sh` refused to install on a cold standby, for being a cold standby

**Looked like:** the bundle would not install on node-b. Pre-flight, hard fail:

```
!!! the D3 probe target http://127.0.0.1:8123/ does not answer from this host.
    The promoter would read that as a dead Home Assistant, release the lease and
    run notify_backup.sh -- which stops the container -- for 900s at a time.
```

**Actually:** found 2026-09-20, deploying 0.6.0. Every word of that message is
true on a **warm** node and inapplicable on a **cold** one. A cold standby's
Home Assistant is *deliberately stopped* — that is what cold means — so the
probe can never answer, so the pre-flight could never pass, on the one node
that most needs the bundle installed.

The outage it warns about cannot occur there either: D3's release branch
requires `previous == "MASTER"`, which a warm node reaches and a cold standby
never does. So the check was not merely inconvenient, it was **guarding against
a branch that is unreachable in that topology**.

🚩 **Same shape as §29.** A mechanism written for the model where both nodes
run Home Assistant, applied to the model whose entire premise is that one of
them does not. The hold could not reach the peer for the same reason this could
not install on it.

**Rule:** a reading that is a FAULT in one topology and the CORRECT STATE in
another cannot carry the same verdict. The check now stays fatal on warm and
becomes a note on cold, and the note says *why* it is continuing — a warning
the operator cannot evaluate is worse than no warning.

⭐ **The near-miss is the lesson.** The first instinct was to edit the `fail`
out of the staged copy and carry on with the deploy. That would have shipped a
working cluster and left the bug in the generator for the next person, who
would have hit it with no context. Fixing the generator cost twenty minutes and
a test; the workaround would have cost somebody an evening.

## 31. 🚨 A node reported the PEER's missing radios as its own, for six hours

**Looked like:** `devices_disabled — Promoted without 2 device(s)` on node-a,
which held all five transceivers with every config entry enabled.

**Actually:** found 2026-09-20. The alarm was describing **node-b**, six
hours earlier.

```
/config/.storage/.cluster_sync_preflight.json
  disabled_at: 20260918T104916   <- node-b's promotion
  file mtime:  2026-09-18 14:32:38   <- node-a's fileset swap
  lists:       A1K33YZ, 07VYAMCJ     <- absent on tiger2, PRESENT on tiger1
```

**The go-bag carries `.storage` wholesale**, marker included. So a promotion
installs the peer's record of the peer's hardware, and the promoted node reads
it as local truth.

🚩 **The clearing logic existed and was correct.** `_write_marker` removes the
file for an empty plan, and its docstring promises exactly this:

> Written on EVERY apply, including one that disabled nothing -- the file is
> then removed. Otherwise a marker from a previous promotion outlives the
> problem and the operator is told about radios that came back, which is the
> quickest way to make somebody stop reading the alert.

**It was never reached.** `_write_marker` is called from `apply_plan`, and
`main()` returns early on the clean path:

```python
if not plan.to_disable:
    out.write("Every referenced device is present. Nothing to disable.\n")
    return 0          # <- never reaches apply_plan, so never clears the marker
```

node-a's own pre-flight printed that exact line at 14:33:04 and left the
peer's marker standing.

**Rule:** a promise in a docstring is not a guarantee in the code. This one was
written, correct, tested in isolation — and unreachable on the path that
mattered. ⭐ **When a function documents "on every X", check the callers reach
it on every X**, because the early return that skips it reads like an
optimisation rather than a bug.

Related: §29 and §30 are the same family — state or assumptions belonging to one
node applied on the other. Here the go-bag is the carrier.

## 32. 🚨 The reconcile shipped as dead code, and 1,442 green tests said otherwise

**Looked like:** nothing. `--reconcile-backup` was armed on both nodes, the
code was deployed, the flag was passed, the promoter ran clean and silent every
ten seconds. Exactly what a healthy cluster looks like.

**Actually:** found 2026-09-22 by deliberately creating the fault on node-b —
starting Home Assistant on a node reading BACKUP — and watching the promoter do
nothing at all.

`_docker_inspector` does not return raw `docker inspect` JSON. It shells out
with a `--format` template and returns a **flat** dict:

```python
{"Restarting": bool, "Running": bool, "ExitCode": int | None, "StartedAtEpoch": float}
```

`_backup_is_serving` read it as nested:

```python
return bool((info.get("State") or {}).get("Running"))   # -> bool(None) -> False, ALWAYS
```

So the branch whose entire job is to stop a second live Home Assistant could
never fire. It shipped that way and ran in production for two days.

🚩 **The tests passed because the fake agreed with the bug.**

```python
def _inspect(running):
    return lambda: {"State": {"Running": running}}   # the shape the CODE expected
```

The fake was written from the same wrong assumption as the code, so it
confirmed it. This is §8 exactly — *an unfaithful fake made a new guard a
no-op* — in the one branch where the consequence is the four-hour outage of
§29.

⭐ **The "verify it bites" check did not catch it either, and that is the part
worth internalising.** The fix was reverted at the CALL SITE to prove the test
failed without it. Both states were consistent with a reader that never worked,
so the test failed for the right reason and the wrong cause. **Proving a test
fails without your change does not prove it tests reality** — only that it
tests your change.

**Rule:** where a fake stands in for a parser, pin them together with a contract
test that drives the REAL parser into the REAL consumer. `test_the_fake_
inspector_matches_the_real_one` feeds a canned `docker inspect --format` line
through `_docker_inspector` and asserts `_backup_is_serving` sees a running
container. Three tests now fail against the shipped bug; none did before.

**And the larger rule, which this project already knew:** AR-0040 was 305 green
tests hiding a restore that had never worked. This is the same lesson at 1,442.
⭐ **A suite cannot audit its own assumptions. Only hardware can.** The
controlled fault on a live standby cost ninety seconds and found what the whole
suite could not.

Related: §8 (unfaithful fake), §29 (the outage this branch exists to prevent).

## 33. 🚨 One live radio hides every dead one, because the watch pools them

**Looked like:** a healthy radio-silence sensor. Rising and falling normally,
reporting fresh readings, status `ok`.

**Actually:** found 2026-09-22, deciding where to point the new radio-silence
alert after deCONZ had been dead nine days (§32's sibling).

`RadioSilenceSensor` answers one question across every configured glob:

```python
return max(0, int(now - max(stamps)))      # max() across ALL matched entities
```

So the **freshest** radio in the house sets the number. On this fleet the watch
is `sensor.*_rssi_numeric` — **525 RFXtrx entities against 6 deCONZ ones.** RF
traffic alone keeps that reading healthy indefinitely.

🚩 **So the obvious remedy was worse than the gap.** The instinct after the
deCONZ outage was "add a Zigbee glob to `radio_watch`". That would have pooled
six Zigbee entities in with five hundred RF ones, produced a sensor that reads
`ok` while Zigbee is stone dead, and — because the watch now *mentions* Zigbee —
looked like coverage to anyone auditing the configuration afterwards.

**A blind spot with a label on it is worse than a blind spot.**

**Rule:** where a signal aggregates independent sources, the aggregate cannot
report any single source failing. `_radio_watch_fault` therefore judges **each
pattern separately** and names the one in fault:

```
`sensor.*_battery` matched 2 entities and not one has a usable value
 — that radio is not being heard.
```

The sensor still pools, deliberately — it is one number on a dashboard and that
is what it is for. The *alert* is the thing that has to distinguish, because it
is the thing that wakes somebody.

⭐ **The general shape:** this is standard #3 — *an empty field is not a zero
value* — one layer up. A pooled maximum is not a statement about any member of
the pool, and reading it as one is how a dead radio passes a green check for
nine days.

Related: §18 (the livelocked daemon the sensor was built for), §32 (the alert
that could not fire), and the dead-radio entry in the roadmap's known gaps.

## 34. 🚨 The cold standby had no voice, so three readouts said "unknown" forever

**Looked like:** a cluster status panel with three blanks on a perfectly healthy
pair — `Members seen 1`, `Clock skew unknown`, `Config fileset unknown`. Noticed
by the owner asking whether that was *supposed* to be like that.

**Actually:** found 2026-09-23. One root cause behind the first two, and a
second of the same family behind the third.

### The registry is written by the integration, and the standby's is stopped

`cluster_members` and `clock_skew` both read `nodes:*`, refreshed by
`backend.register_node` — which runs **inside Home Assistant**. On a cold
standby Home Assistant is deliberately stopped, so that node never registers,
never appears, and:

* `Members seen` reads **1 on a healthy two-node cold cluster and 1 on a broken
  one** — absence and failure sharing a signal, standard #4;
* `clock_skew` needs two offsets and can therefore **never** be computed on a
  cold deployment. It was not transiently unknown; it was unknowable.

🚩 **ADR-009 had already recorded both facts, and named the fix.** It added the
promoter heartbeat in 2026-09-08 precisely because *"the promoter is the only
process running, so it is the only possible reporter"* — and wrote:

> `cluster_members` reads 1 on a *healthy* two-node cold cluster and would read
> 1 on a broken one too.

**Nothing ever read the heartbeat.** `read_promoter_nodes()` had exactly one
caller — the *statistics-stalled* alert, asking a different question. So the
answer sat in the store, refreshed every ten seconds by both nodes, for two
weeks, and no surface consulted it.

⭐ **A signal written and never read is not observability, it is exhaust.** The
ADR's chosen mitigation was "harden and make it visible"; it made it *written*.
The distance between those two words is the whole defect.

**Rule:** the promoter now carries its clock offset in the heartbeat it already
writes — measured the same way `register_node` does, around a `TIME` call — and
`node_count` counts a node seen by **either** route. A node reporting through
both is counted once.

⚠️ **And the reader tolerates the old payload.** Promoters before this wrote a
bare timestamp; newer ones write JSON. During a rolling upgrade one node writes
each, and a reader that understood only the new shape would report the
un-upgraded node as *absent* — turning an upgrade into an apparent node loss, at
the moment an operator can least tell the difference.

### The fileset age meant two opposite things

`last_success_at` lives in the publisher's memory, so a restart or an options
save reset it and the panel read `unknown` on a leader whose go-bag was
perfectly current — and would read exactly the same if publishing had stopped
dead. Two opposite meanings, one blank.

Now: one publish twenty seconds after setup (delayed, so a scan of several
thousand files does not compete with Home Assistant's boot), and a `status`
attribute that distinguishes `awaiting_first_publish` from `refused:too_large`.

⭐ **The measurement that nearly fooled me.** The follower's staged
`status.json` carries a *fresh* timestamp every minute — which proves the
follower pulled, not that the leader published. I read it as proof the leader
was publishing and had to be corrected by watching the generation counter
actually advance. **A fresh timestamp on the consumer says nothing about the
producer.**

Related: §32 and §33, both the same week, both signals that existed and reported
to nobody.
