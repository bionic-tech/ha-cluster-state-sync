# What replicates, what doesn't, and how Home Assistant behaves on its own

Answers four questions, in order of how much they change the design:

1. How does Home Assistant behave natively after a crash or a clean shutdown?
2. Given that, can our restore fire your automations?
3. Are we replicating every domain, and which ones have problems?
4. What is all this machinery, in plain terms?

The governing principle for this integration: **stay in line with what Home
Assistant already does.** Everything below was read out of Home Assistant's own
source and off the live nodes, not assumed.

---

## 1. What Home Assistant does natively

Home Assistant already has a state-restore mechanism. It is
`homeassistant.helpers.restore_state`, it persists to
`.storage/core.restore_state`, and any entity whose class inherits
`RestoreEntity` participates.

### When it writes

| Trigger | What survives |
|---|---|
| `EVENT_HOMEASSISTANT_STOP` — a clean shutdown or restart | everything, written on the way out |
| every **15 minutes** (`STATE_DUMP_INTERVAL`) | a periodic snapshot |
| a crash, a `kill -9`, a power cut | **only the last periodic dump — up to 15 minutes stale** |

So Home Assistant's own answer to "the host died" is *lose up to a quarter of an
hour*. A clean `docker restart` loses nothing; a crash loses whatever changed
since the last dump.

There is also `STATE_EXPIRATION = 7 days`: stored state older than a week is
discarded rather than restored.

### When it reads — and this is the important part

The startup sequence, from `HomeAssistant.async_start()`:

```
CoreState.not_running     integrations set up, entities added,
                          RestoreEntity reads core.restore_state
CoreState.starting
      EVENT_HOMEASSISTANT_START        <-- cluster_state_sync restores here
CoreState.running
      EVENT_HOMEASSISTANT_STARTED      <-- automations attach their triggers here
```

`AutomationEntity` decides whether it is on the same way:

```python
if state := await self.async_get_last_state():
    enable_automation = state.state == STATE_ON     # from core.restore_state
else:
    enable_automation = DEFAULT_INITIAL_STATE
if self._initial_state is not None:
    enable_automation = self._initial_state          # initial_state in YAML wins
```

and then, crucially, decides *when to start listening*:

```python
async def _async_enable(self) -> None:
    self._is_enabled = True
    if self.hass.state is not CoreState.not_running:
        self._async_detach_triggers = await self._async_attach_triggers(False)
        return
    # HomeAssistant is starting up -- wait
    self.hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STARTED, self._async_enable_automation
    )
```

**During startup, automations do not attach their triggers.** Home Assistant
holds them back until everything has finished starting. That is native
behaviour, and it exists for exactly the reason you would expect: a booting
system generates hundreds of state changes that are not events, they are just
the system finding out what is already true.

---

## 2. So — can our restore fire your automations?

**At boot: no. It cannot.** Our restore runs on `EVENT_HOMEASSISTANT_START`.
Automations attach on `EVENT_HOMEASSISTANT_STARTED`, which fires afterwards.
We write into a state machine that no automation is listening to yet.

This is not a guard we added. It is a consequence of restoring at the same point
Home Assistant restores, which was the design goal.

Three honest caveats:

- **A trigger that explicitly asks to fire at startup still fires** —
  `platform: homeassistant, event: start`. That is the automation asking for it,
  and it fires on a normal restart too. Nothing to do with us.
- **Adding the integration to an already-running Home Assistant is different** —
  there, `CoreState` is `running`, triggers are attached, and seeding dozens of
  entities would fire everything at once. We skip the restore in that case
  (AR-0012). It only runs at boot.
- **The overlap window is a genuinely different problem.** During a failover
  both instances can be live for a few seconds, and *both* run their automations.
  That is not restore-triggered firing; it is two houses briefly having two
  brains. There is no leader election for automations, by design — see
  `KNOWN-LIMITATIONS.md`. If you want an automation to be safe in that window, make it
  idempotent, not conditional on being the leader.

### Where we improve on native behaviour

| | Home Assistant alone | with cluster_state_sync |
|---|---|---|
| clean restart | loses nothing | loses nothing |
| **crash / host loss** | **up to 15 min stale** | **up to 5 s stale** (snapshot interval) |
| **failing over to the other node** | the other node has *its own* `core.restore_state`, which knows nothing about the house | the peer's snapshot, seconds old |

The second row is the one that matters, and the third is the reason the project
exists. `core.restore_state` is per-node and is in `DEFAULT_FILESET_EXCLUSIONS` —
it is deliberately *not* copied between nodes, because it is a node's private
memory of its own last run. The shared snapshot is the cross-node channel.

---

## 3. Are we replicating every domain?

**No, and deliberately not.** Here is what is actually on the live estate, from
`core.restore_state` on node-a — i.e. the entities Home Assistant itself
remembers across a restart:

| domain | entities | replicated? | why |
|---|---:|---|---|
| `automation` | 124 | **yes** | HA owns the on/off. Applied via `automation.turn_on/off`, not by writing state |
| `input_boolean` | 13 | **yes** | HA owns the value outright. Nothing else knows it |
| `input_datetime` | 4 | **yes** | same |
| `timer` | 2 | **yes** | same |
| `input_number` | 1 | **yes** | same |
| `input_text` | 1 | **yes** | same |
| `sensor` | 201 | no | the device reports it; a restored value is overwritten within seconds |
| `button` | 185 | no | stateless — the value is "when last pressed" |
| `update` | 112 | no | the integration recalculates it |
| `switch` | 86 | no | **device-backed — see below** |
| `binary_sensor` | 76 | no | the device reports it |
| `light` | 11 | no | **device-backed — see below** |
| `person`, `device_tracker` | 16 | no | derived from trackers, and location data — opt-in, not default |
| `cover`, `siren`, `remote`, `number`, `text` | 14 | no | device-backed |
| `script` | 5 | no | state means "currently running", which should not survive a move |
| `alarm_control_panel` | 1 | no | Alarmo keeps its own state in `.storage`, which the fileset *does* carry |

Also in our allowlist but with no entities on this estate, so inert:
`counter`, `input_select`, `vacuum`, `humidifier`, `water_heater`.

`climate` is the interesting one, and it is a trap for exactly the reasoning
above. The seven climate entities here are `homekit_controller`, they appear in
`core.restore_state` **zero** times, and the device reports the truth on
connect — so on this estate replicating it does nothing.

That is not grounds for removing it from the shipped default. **Seven Home
Assistant core integrations use `RestoreEntity` for climate** — `modbus`,
`shelly`, `plugwise`, `screenlogic`, `switchbot_cloud`, `teslemetry`, and
`generic_thermostat`, which is the commonest DIY thermostat in Home Assistant.
For those users HA genuinely owns the state and replicating it is real work.
`DEFAULT_INCLUDE_DOMAINS` ships to every HACS user; measuring it against one
estate is how you break other people's setups to tidy your own.

If anything in the default list is genuinely inert it is `vacuum` and
`water_heater`: **no** core integration uses `RestoreEntity` for either. Both
are harmless, so neither is worth removing.

### 🚨 The real gap: `automation` is not in the default

`DEFAULT_INCLUDE_DOMAINS` ships eleven domains and **`automation` is not one of
them.** This estate replicates it only because it was added to `options` by
hand. That is backwards: it is the highest-value domain we carry — HA owns the
truth, 124 entities here, and it is the one domain applied by *calling the
service* rather than writing state, so it is the one that actually takes
effect. Every fresh install is missing the thing that works best.

### The rule that falls out of this

> **Replicate exactly what Home Assistant itself restores AND nothing else
> knows.**

Two conditions, both required. `sensor` passes the first and fails the second —
HA remembers it, but the device will tell you within seconds anyway. The
`input_*` helpers pass both: HA is the only thing in the world that knows what
you set that slider to.

### 🚨 The one that surprises people

**For `light`, `switch`, `cover` and friends, the restore does not do what it
looks like it does.** It writes Home Assistant's state machine. It does not
command the device. So a restored "on" is a lamp that Home Assistant *believes*
is on, and the integration corrects it to `off` on the next poll — usually
within seconds.

That is a cosmetic flicker in the UI, not a light that comes on. It is also why
adding these domains is not the safety problem the old wizard text claimed —
the problem is that it does not work, not that it works dangerously.
`automation` is the exception, and only because we apply it by *calling the
service* rather than writing state.

---

## 4. The machinery, in plain terms

**The lease** is the "who is on call" board. One node writes its name with a
short expiry and keeps re-writing it. If it stops re-writing — because it died —
the name expires and the other node writes its own. There is no vote, no
negotiation, and no way for both to hold the pen at once, because the write is
conditional on the board being empty.

**The promoter** is the person watching that board. It has one job: when the
name on the board changes to mine, do the promotion checklist. It is a timer
that reads a key, not a cluster manager.

**The snapshot** is a note on the fridge, rewritten every five seconds: "boiler
timer running, guest mode off, holiday dates set to the 12th." It is not a
backup of the house. It is the handful of things you could not work out by
looking around.

**The restore** is reading the fridge note *before* opening the post, not after.
That ordering is the whole product. Home Assistant reads its own note at the
same moment, which is why we picked that moment.

**The restore gate (AR-0065)** was the fridge note getting overwritten with a
blank one by the node that just woke up, before the node had read it. It wrote
"here is what I know" while knowing nothing. The gate says: you may not write to
the fridge until you have read it.

**The go-bag (fileset)** is the packed bag by the door — the files that are not
state and would take an afternoon to recreate: your dashboards, your Alarmo
config, your registries. Sealed, because it contains credentials.

**Cluster-wide vs per-node config** is the difference between "the alarm code"
and "which drawer *this* house keeps it in". The first must be identical on both
nodes or they disagree about reality. The second must differ, or node two tries
to open node one's drawer. Getting a field in the wrong list is silent — which
is why there is now a test that fails when a 60th field appears without being
classified.

**The alert router** is the smoke alarm, with the deliberate property that it
does not sound while you are cooking dinner on purpose. It holds its tongue
until Home Assistant has finished starting, only speaks on a *change* of
condition, and refuses to chatter.

**The ingress probe** is checking that the front door bell still rings after you
have moved house. The failover moved the family and the furniture; nobody had
checked whether visitors could still find the address.

**The maintenance hold** is the "engineer on site" sign. It stops the promoter
reacting to something you are doing on purpose.

**The radios** are the door keys — physically one set. They move to whoever is on
call, and they move *before* the person arrives, because a container's view of
`/dev` is a photograph taken at the moment it starts. One key is cut for one
door only and never moves: the RFXTRX wired to tiger1.

**The preflight** is checking the new house has the same sockets before you plug
anything in.

**AR-0068** was a doorbell wired to the fuse box: every time you adjusted a
setting, the house failed over. Changing an option released the lease, because
the code path that applies options is the same one that unloads the integration.
Fixed by marking a reload as a reload.

---

## Open items this raises

Two of the three items first filed here were **wrong**, and both were wrong the
same way: I claimed the product lacked something without reading the product.
They are kept visible rather than deleted, because the failure mode is the
interesting part.

- ~~`climate` should come out of the default allowlist.~~ **Withdrawn.** Seven
  HA core integrations restore climate, `generic_thermostat` among them. It is
  inert on *this* estate only. See the correction above.
- ~~We have no equivalent of `STATE_EXPIRATION`.~~ **Withdrawn.**
  `CONF_RESTORE_MAX_AGE` has always existed, defaults to **1800 seconds**,
  refuses the whole snapshot past that (`__init__.py:1903`), is covered by
  `tests/test_restore.py`, and is set to 1800 on the live estate. Ours is
  **240× stricter** than HA's seven days — the right direction for a failover
  cluster, where restoring a week-old picture of a house would be a bug rather
  than parity. Nothing to do.
- 🚨 **`automation` is missing from `DEFAULT_INCLUDE_DOMAINS`** — the one real
  gap, and it was found only by checking the two claims above. No fresh install
  replicates the highest-value domain we carry.
- 🚨 ~~`initial_state` overrides everything, ours included.~~ **Wrong, and
  backwards — this is the one that turned out to be a real bug.** Pinned
  automations *are* restored, and that is the problem. HA applies the pin in
  `async_added_to_hass`, where it beats `async_get_last_state()`. We restore
  later, on `EVENT_HOMEASSISTANT_START`, by *calling a service* — and a command
  lands after a pin. So an operator who wrote `initial_state: false` to opt an
  automation out of restore gets it re-enabled at every promotion. Proven in
  `tests/test_initial_state.py`, and **fixed the same day**: the restore now
  skips any automation whose config pins its startup state, in both directions,
  and counts the skip in its summary. Honouring it required reading the private
  `_initial_state` — a deliberate exception to this repo's public-APIs-only
  rule, because HA exposes `initial_state` on no public API at all, with a test
  that fails loudly if the attribute ever moves.
