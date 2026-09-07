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

### 18a. It found a real one, then lied about the size of it

Not a drill. Deploying the sensor needed one Home Assistant restart on the
leader. Afterwards, every check this fleet has ever used said the radios were
fine — ports open, devices claimed in VirtualHere, container healthy,
integration loaded, nothing logged — and the new sensor reported a plausible,
steadily-rising silence:

```
sensor reads              : 60 s ... 120 s ... 191 s ... 360 s
distinct ages across 37   : 2          <- one batch write, no organic traffic
RFXtrx debug, 150 s       : 0 lines
serial fds held by HA     : 3 (ttyUSB0, ttyUSB1, ttyUSB4)
```

`reload_config_entry` on the three rfxtrx entries produced an immediate,
successful handshake — `Status [subtype=433.92MHz, firmware=28,
output_power=28]` — so the hardware was fine and the read loop came back.
**At that point I wrote that the sensor had caught an outage and the reload
had fixed it. Both halves were wrong, in opposite directions.**

The reload fixed nothing, because nothing had broken at 13:11. And the outage
was far bigger than a Home Assistant restart:

```
history, 30 hours, sensor.radiator_aircon_rssi_numeric:
    1 recorded state -> "unknown", set 2026-09-06 06:33 UTC

current states of the 37 watched entities : {'unknown': 37}
current states of the 59 rfxtrx event.*   : {'unknown': 57, 'unavailable': 2}
every Recv line in 28 hours of log        : a status/handshake packet
air packets received, ever, in that log   : zero
```

**The RFXtrx receivers had been deaf for at least thirty hours.** Not since my
restart — since the previous morning. That is the outage behind the kitchen
switch that "stopped working", and behind the light that appeared to come on by
itself: the wall switch transmits, nothing receives it, so the automation it
should trigger never runs.

#### And the sensor reported a healthy-looking number throughout

This is the part to keep. **Home Assistant stamps `last_reported` on an entity
whose state is `unknown` exactly as it does on a real reading.** All 37 watched
entities were `unknown`; every time Home Assistant rewrote them — a restore, a
reload, a restart — their timestamps advanced, and the sensor faithfully
reported the age of Home Assistant's own bookkeeping. 60 s. 120 s. 191 s.
Numbers indistinguishable from a working radio.

A sensor built specifically to stop "green everywhere, and the house did
nothing" reproduced it on its first day, inside itself.

**Fixed by excluding entities that carry no reading**, and by separating the
two cases that both have to read `unknown`:

| `status`     | means                                                |
|--------------|------------------------------------------------------|
| `ok`         | at least one radio has reported; the number is real  |
| `no_matches` | the globs match nothing — a configuration problem    |
| `no_reports` | entities matched, none has ever reported — **deaf**  |

`no_reports` is the loudest state this sensor has and it is deliberately *not*
a big number, because "never" has no age. **Alert on the attribute, not only on
the value** — a threshold on the value alone would have sat quietly through all
thirty hours of this.

Pinned by `test_an_unknown_entity_is_not_evidence_of_reception`, verified to
fail against the shipped behaviour.

#### The remediation automation is written and deliberately not installed

[`docs/examples-rfxtrx-reload-automation.yaml`](examples-rfxtrx-reload-automation.yaml)
replaces the inert `RFXtrx reload on failure`. It is **not** installed, because
while the receivers are actually deaf it would reload the three entries every
thirty minutes forever and fix nothing. Install it once the radios receive
again.

Two things in it are worth copying into any alert built on this sensor:

- **A `numeric_state` trigger cannot fire on `no_reports`**, because that state
  is `unknown`. A threshold on the value alone is exactly what sat quietly
  through seven days here. It needs a second trigger on the attribute.
- **Never remediate on `no_matches`.** The globs match nothing; reloading
  radios cannot fix a typo.

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
