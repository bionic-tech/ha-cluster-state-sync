# Choosing what to replicate

Replicating more is not safer. This guide is the reasoning behind each verdict
in the domain picker, with the scenarios that make the answer flip.

**The short version:** replicate what Home Assistant is the only owner of.
Everything else either rebuilds itself, is reported by a device, or has no
state worth the name — and it is not free, because the flush writes the **full
map** whenever anything in scope changes. One churning domain turns an idle
cluster into a permanent write loop.

---

## The four questions

Ask them in order. The first "yes" settles it.

1. **Would losing this value change what an automation does?**
   This overrides everything below. See *the guard toggle* immediately.
2. **Does anything else know the value?** A device, an integration, or a
   calculation. If so, replication is cosmetic at best.
3. **Is there a value at all?** A button's state is the timestamp of its last
   press. There is nothing to carry.
4. **Does it change constantly?** Even a useful domain costs you if it keeps
   the full-map flush running permanently.

---

## The case that overrides everything: the guard toggle

A toggle whose only job is to stop an automation acting is the strongest
argument for replication in the whole system, and it is easy to miss because
the toggle does nothing on its own.

From a real estate, thirteen of them:

```yaml
# The washing machine is mid-cycle. Do not let the shutdown automation kill it.
condition:
  - condition: state
    entity_id: input_boolean.dishwasher_washing_machine_ignore_shutdown
    state: "off"
```

**If a failover resets that toggle, the automation shuts down a running washing
machine.** Nothing errors. Nothing logs a fault. The cluster reports a healthy
promotion and the house does the wrong thing.

The same shape recurs everywhere once you look:

| toggle | what its loss actually does |
|---|---|
| `*_ignore_shutdown` | shuts down the thing you told it not to |
| `network_maintenance_mode` | the watchdog acts during your maintenance |
| `network_watchdog_armed` | the watchdog stops acting when it should |
| `gpu_host_recovery_disable` | a recovery you disabled runs anyway |
| `alarm_test` / `alarm_debug` | a test alarm behaves like a real one |

🚨 **`input_boolean` is therefore not negotiable.** It is cheap — helpers change
rarely, so they cost almost nothing in flush traffic — and the failure mode when
it is missing is an automation acting on a house it has misread.

### The same argument, with a clock: cooldown timers

```
timer.gpu_host_recovery_cooldown
timer.firewall_cycle_cooldown
```

A running cooldown timer means *"do not try that again yet."* Lose it in a
failover and the standby retries **immediately** — the exact loop the cooldown
exists to prevent, at the exact moment the cluster is least stable.

`timer` is small, changes rarely, and its loss causes a retry storm. Replicate.

---

## Domain by domain

### Replicate — Home Assistant is the only owner

| Domain | Why it must cross | The scenario if it doesn't |
|---|---|---|
| `input_boolean` | Nothing else knows it | The washing machine case above |
| `timer` | A running timer is a promise about the future | Cooldown lost, recovery loops |
| `counter` | An accumulated total exists nowhere else | Your count restarts at zero |
| `input_number` | A setpoint you chose | Thermostat offset reverts to default |
| `input_text` | Free text you typed | A note, a code, a name — gone |
| `input_select` | A mode you picked | "Holiday" silently becomes "Home" |
| `input_datetime` | Dates you set | Holiday dates reset; schedules fire wrong |
| `automation` | On/off is HA's, **and it is actually applied** | An automation you disabled comes back on |
| `todo` (`local_todo`) | The list lives in `.storage`, not a service | Shopping list rolls back |
| `schedule` | Defined in HA, referenced by automations | Heating schedule reverts |

`automation` deserves its own note: it is the only domain applied by **calling a
service** rather than writing state, so its restore actually takes effect. Every
other domain here is seeded into the state machine, which is right for a helper
because nothing else will contradict it.

### Not needed — it rebuilds itself

| Domain | What rebuilds it | How long |
|---|---|---|
| `device_tracker` | Your trackers report again | Seconds |
| `person` | Recalculated from that person's trackers | Immediately after the above |
| `zone` | Defined in configuration | At startup |
| `sun` | Latitude and the clock | Instantly |
| `weather` | Re-fetched from the provider | One poll |
| `calendar` | Re-fetched from the calendar it mirrors | One poll |

⚠️ **The edge case that flips `device_tracker`.** Presence automations. As
trackers re-report after a promotion, some go `unknown → home`, and an automation
watching for that transition can fire — a "welcome home" scene at three in the
morning. The boot restore itself cannot cause this (it runs before automations
attach triggers), but the re-reporting afterwards can.

If you have transition-triggered presence automations, the fix is a condition on
`for:` duration or on `from: not_home` specifically — **not** replicating 1,436
entities that will be overwritten within seconds anyway.

### Cosmetic — the device is the authority

`light` · `switch` · `fan` · `cover` · `valve` · `lock` · `climate` ·
`media_player` · `camera` · `sensor` · `binary_sensor` · `number` · `text` ·
`select` · `siren` · `remote` · `vacuum` · `humidifier` · `water_heater`

**The restore writes Home Assistant's state machine. It does not command the
device.** So a restored "on" is a lamp Home Assistant *believes* is on, and the
integration corrects it on the next poll — usually within seconds.

This is not dangerous. It is simply ineffective, and it is expensive for the
churning members: `media_player` and `camera` update constantly, which keeps the
full-map flush running.

🚨 **The edge case that flips `sensor`.** Some sensors are *not* device-backed:
`utility_meter`, `integration`, `derivative`, `statistics`, `history_stats`,
`trend`. These **accumulate**, Home Assistant owns them, and losing one is real
data loss — your energy dashboard restarts from zero.

**Do not turn on the whole `sensor` domain for these.** That drags in every
device sensor you have. Name them individually in **include entities**:

```
sensor.electricity_daily        # utility_meter
sensor.gas_monthly              # utility_meter
sensor.solar_energy_total       # integration
```

That is precisely what the include-entities field is for, and it is the single
most useful thing in this guide for most people.

### Nothing to carry — there is no state

`button` · `event` · `scene` · `notify` · `stt` · `tts` · `wake_word` ·
`conversation` · `assist_satellite` · `ai_task` · `image` · `tag`

Their "state" is a timestamp of when they last fired. Replicating it copies a
clock reading between machines.

⚠️ **The edge case.** A template that reads `last_pressed` or `last_triggered` —
*"if the doorbell was pressed in the last five minutes"* — evaluates differently
after a promotion, because the timestamp is gone. It is a narrow case and the
answer is still not the whole domain: name that entity in **include entities**.

### Not needed — something else already restores it

| Domain | Who owns it |
|---|---|
| `script` | **`on` means *currently running*.** Carrying that over tells the standby a script is running when it is not |
| `alarm_control_panel` | Alarmo and similar keep their own file in `.storage`, which the go-bag already carries in full |
| `update` | Recalculated by comparing versions again |

🚨 **The edge case that flips `alarm_control_panel`.** The built-in `manual`
alarm panel has no storage of its own — its armed state is Home Assistant's, and
losing it disarms your alarm on failover. If your panel is `manual` rather than
Alarmo, replicate it. If it is Alarmo, the fileset has already done the job and
replicating the entity is redundant.

---

## What this costs, so the trade is visible

The flush writes the **full map** — not a delta — whenever anything in scope has
changed. It skips only when nothing has.

Measured on a real estate, 2026-09-10:

| Scope | Entities | Snapshot | Flush behaviour |
|---|---:|---:|---|
| Defaults + `automation` | ~190 | ~70 KB | Skipped most ticks; a quiet house writes nothing |
| 38 domains | 2,158 | ~0.9 MB | **Never skips** — `device_tracker` alone guarantees a change every tick |

At a five-second interval that is roughly 15 GB a day of TLS write traffic to
Valkey, to replicate 1,970 entities that cannot benefit from it.

**The "quiet cluster skips the flush" optimisation is the first thing you lose**
by ticking a churning domain, and you lose it completely rather than gradually.

---

## If you are about to open an issue

Check the domain against the picker's own label first — it states the verdict and
the reason next to every option, with your entity count.

Then the three questions that resolve most reports:

1. **"It restored but the device went back."** Device-backed domain. Working as
   designed: the restore writes state, the integration reports truth. See
   *Cosmetic* above.
2. **"My automation ran when it shouldn't after a failover."** Check whether a
   guard toggle was in scope. If `input_boolean` is unticked, tick it.
3. **"The snapshot is enormous / Valkey is busy."** Count your `device_tracker`,
   `media_player` and `button` entities. The full-map flush is the mechanism.

Related: [`GUIDE-what-replicates.md`](GUIDE-what-replicates.md) for how Home
Assistant behaves natively, and why the boot restore cannot fire your
automations.
