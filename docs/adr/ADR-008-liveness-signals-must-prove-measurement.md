# ADR-008: A liveness signal must prove it measured something

**Status:** Accepted — implemented
**Date:** 2026-09-07
**Deciders:** mmanning (project owner)

## Context

This project exists because of AR-0040: a restore that had never once worked while 305 tests
passed and the only symptom was a log line nobody read. Every rule in `docs/GOTCHAS.md` is a
variation on the same theme — **a signal that looks healthy while measuring nothing**.

On 2026-09-07 the integration shipped `sensor.<node>_radio_silence`, built specifically to close
that class of gap for radios. Within one day it reproduced the failure *inside itself*, twice, in
opposite directions.

**First, it reported a healthy-looking number while measuring nothing.** Home Assistant stamps
`last_reported` on an entity whose state is `unknown` exactly as it does on a real reading. All 37
watched entities were `unknown`; every time Home Assistant rewrote them — a restore, a reload, a
restart — their timestamps advanced, and the sensor faithfully reported the age of Home
Assistant's own bookkeeping. 60 s. 120 s. 191 s. Numbers indistinguishable from a working radio.

**Then it was read as proof of a fault that did not exist.** Its silence was reported as a
seven-day RFXtrx outage. The measurement behind that claim sampled two entities — both long-dead
battery devices — and generalised to thirty-seven. The recorder, queried properly over `event.*`
entities whose state advances only on a genuinely received packet, showed the last packet 8.9
hours earlier against a **4-day maximum normal gap of 9.3 hours**. Nothing was wrong. The cost was
an unnecessary restart of the live leader and two VirtualHere reclaims on a running house.

**And a third, in the promoter.** The M3 hold-down branch printed `NOT taking the free lease`
while the peer demonstrably held it. The branch fires on `previous != "MASTER"` and the hold-down
marker alone; it never looks at the holder. The word "free" asserted something the code had never
checked, and sent an operator hunting a split-brain that did not exist.

Three defects, one shape: **a signal asserting more than it established.**

## Decision

### 1. Absence of a reading is never reported as a reading

A diagnostic that cannot compute its value reports `unknown`. It never reports `0`, and never
reports a plausible-looking number derived from something other than the thing it claims to
measure.

Concretely, `RadioSilenceSensor` excludes entities whose state is `unknown` or `unavailable`
before computing an age. Home Assistant's own state writes are not evidence of reception.

### 2. The reasons for "unknown" must be distinguishable

`unknown` covers two very different situations and an operator has to tell them apart, so the
value is not the whole signal — a `status` attribute carries the distinction:

| `status` | meaning | operator action |
|---|---|---|
| `ok` | at least one source has reported; the number is a real age | read the number |
| `no_matches` | the configured globs match nothing | fix the configuration |
| `no_reports` | sources matched, none has ever reported | **investigate the hardware** |

`no_reports` is the loudest state the sensor has and is deliberately **not** a large number,
because "never" has no age. **A `numeric_state` threshold can therefore never fire on it**, which
is why any alerting built on this must trigger on the attribute as well as the value. A threshold
on the value alone is silent in exactly the worst case.

### 3. A log line may not assert a fact the code did not establish

The hold-down message now says what it knows — that this node is holding down and is not
attempting a take — and explicitly does **not** claim the lease is free, because that branch never
looks. It points at `--adopt`, which does read and print the real holder.

This is a general rule for this codebase, not a one-off fix: if a message names a state, the code
emitting it must have observed that state.

### 4. Silence is only evidence once the baseline is known

Before reporting that something has gone quiet, establish the longest *normal* quiet period for
that signal. One query answers it. Without it, a quiet signal and a broken signal are
indistinguishable, and the wrong conclusion is as expensive as no conclusion.

This is why `sensor.radio_silence` deliberately **does not pick a threshold**: only the operator
knows their own traffic. What is diagnostic is a number that used to move and has stopped.

### 5. The measurement must suit the estate, and the docs must say when it does not

"Time since last packet" requires a source that transmits **unprompted**. On the fleet this was
built for, every watched entity belongs to a switch, remote or cover — devices that transmit when
a person operates them — and the Oregon sensors that would have reported on a schedule are dead.
There, the sensor measures *human activity*, not radio liveness, and after any restart it reads
`no_reports` until somebody presses something.

That is a limitation of the measurement, not a bug, and it is documented as a precondition rather
than discovered by each new operator.

## Consequences

**Good.** The sensor now fails toward saying "I do not know", which is the only honest answer when
it cannot measure. The three cases are separable without reading source. The promoter's log no
longer sends operators after imaginary faults.

**Accepted cost.** `no_reports` cannot be alerted on with a simple numeric threshold, so any
blueprint or automation built on this sensor is slightly more complex than "value > N". That
complexity is the point: the simple version was silent through the case that mattered.

**Accepted cost.** On an estate with no unprompted transmitter, this sensor cannot do its job at
all. It is off by default and documented as needing a periodic source. Closing that properly needs
an *active* prober — reload each entry to prove every read loop is alive, then transmit and watch
the other radios — which is specified in TODO and not built.

**Regression guards.** `test_an_unknown_entity_is_not_evidence_of_reception`,
`test_the_three_states_are_distinguishable`, and
`test_the_holddown_message_never_claims_the_lease_is_free` were each verified to fail against the
behaviour they replace. A test that passes both ways guards nothing.

## Related

- `docs/GOTCHAS.md` §18, §18a — the full evidence, including the retraction
- [ADR-006](./ADR-006-lease-promoter.md) — the hold-down this corrects the message for
- [ADR-009](./ADR-009-radio-custody-failure-modes.md) — the other half of the radio story
