# Home Assistant compatibility — what an upgrade can break, and how we hear first

**Short version:** upgrading Home Assistant is low risk for this integration
*except* for one thing — a **recorder schema bump stops history replication
until every side agrees**, and on a cold standby nothing migrates the standby's
store, so it needs a fresh seed. Everything else fails loudly at startup rather
than quietly in the background.

---

## 1. How this integration touches Home Assistant

Three surfaces, three failure modes.

| Surface | What we use | If Home Assistant changes it |
|---|---|---|
| **Public API** | entity base classes, config entries, helpers, issue registry | `ImportError` at setup — loud, immediate, obvious |
| **Recorder SQLite schema** | read directly: `schema_changes`, `statistics_meta`, `statistics` | 🚨 **Silent.** Replication refuses and stops; nothing is corrupted |
| **Generated host artefacts** | systemd, docker, nftables | Unaffected by Home Assistant entirely |

**We use no private Home Assistant API.** No underscore-prefixed imports, no
monkey-patching, no core fork — checked, and it is the whole point of
[the design](../README.md#what-it-does-not-do). The nineteen symbols we
do import are listed in `scripts/ha_compat_check.py` and verified weekly.

---

## 2. The recorder schema, which is the one that matters

Home Assistant records its recorder schema version in the `schema_changes`
table. This project is tested against **schema 53** (Home Assistant 2026.6.4).

`statistics_sync.apply_payload` **refuses** to apply rows when the two sides
disagree. That is deliberate: rows shaped for one schema written into another
corrupt history in a way that opens cleanly and is wrong. A gap beats a corrupt
database.

### What actually happens when you upgrade

| Scenario | Result |
|---|---|
| **Both nodes upgraded, both recorders migrated** | Replication resumes on its own. Nothing to do |
| **Leader upgraded, standby's Home Assistant still stopped** (the cold model) | 🚨 The standby's **store is migrated by nothing**. Replication stops and raises `statistics_schema_mismatch`. **A fresh seed is required** |
| **Warm standby, both running, staggered upgrade** | Replication pauses between the two upgrades, then resumes |
| **You never enabled statistics replication** | A schema bump changes nothing for you |

### The cold-standby case, spelled out

This is the one that surprises people. On a cold standby:

* `home-assistant_v2.db` is not open, because Home Assistant is stopped.
* `.cluster_sync_statistics.db` — the replicated store — is **not a database
  Home Assistant is managing**. No migration ever touches it.

So after a leader upgrade that bumps the schema:

1. The leader publishes windows at the new schema.
2. The standby refuses them and reports `schema_mismatch`.
3. The leader raises a repair naming exactly that.
4. **Fix:** press *Write statistics seed* on the leader and copy it across
   again. The new seed carries the new schema.

Starting the standby's Home Assistant once does **not** fix it — that migrates
`home-assistant_v2.db`, not the store.

---

## 3. How we find out before you do

### The weekly canary

`.github/workflows/ci.yml` runs a `canary` job every Monday which installs the
**newest** Home Assistant harness — everything else stays pinned — then:

* reports the Home Assistant version it pulled,
* runs `scripts/ha_compat_check.py`, which verifies every API symbol we import
  still exists **and** compares the recorder schema against the tested one,
* runs the whole test suite against it.

It is `continue-on-error`, deliberately: a red canary is a fortnight's warning,
not an outage, and it must never block work on a release nobody has adopted.

> **This job did not exist until 2026-09-09.** The schedule's comment had
> promised a canary since the file was written, while every job installed the
> pinned requirements — so the weekly run re-tested the same Home Assistant and
> could never have found anything. Found while asking how to get ahead of
> releases rather than behind them.

### hassfest

Home Assistant's own integration validator runs against **current** Home
Assistant on every push, catching manifest errors and declarations deprecated
out from under us.

### Run it yourself

```bash
python scripts/ha_compat_check.py
```

Exit code 1 if anything moved.

---

## 4. Upgrading a live cluster — the order that works

1. **Check the canary** (or run the compat script) against the release you are
   moving to. If it is green, nothing below changes.
2. **If the recorder schema moved**, plan the re-seed before you start.
3. Upgrade Home Assistant on **the leader** first and let it settle.
4. Upgrade the standby. On a cold standby that means updating the image; its
   Home Assistant stays stopped.
5. **If the schema moved**, write a fresh statistics seed on the leader and
   copy it across. The `statistics_schema_mismatch` repair will clear itself
   on the next pull.

The go-bag, the lease, the promoter and the radios are unaffected by a Home
Assistant version change — they are host artefacts and do not import Home
Assistant at all.

---

## 5. Version floors, and why they are where they are

| Thing | Value | Why |
|---|---|---|
| `hacs.json` `homeassistant` | 2026.6.4 | The version the suite actually runs against |
| `manifest.json` `requirements` | `redis>=5.0.0,<7` | Upper bound so a major bump cannot arrive unannounced |
| Python | ≥ 3.14 | Home Assistant's own floor, not ours |

`pytest-homeassistant-custom-component` pins an exact Home Assistant version,
and **that pin is the tested target**. Moving it is how the target moves; keep
it in step with the Python version in CI.
