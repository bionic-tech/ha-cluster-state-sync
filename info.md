# Cluster State Sync

**Keeps a second Home Assistant on standby, so losing a machine does not mean
losing your house.**

---

## What it does, in plain terms

You run Home Assistant. If the machine it runs on dies, your house stops
responding until you fix it — and a fresh second machine knows nothing: no
login, no settings, no idea what was on or off.

This puts a **second machine on standby**. Both talk to a small shared
noticeboard (Valkey or Redis). The active one keeps writing what everything is
doing; the standby keeps a copy of your logins, your settings and your
long-term history.

When the active machine dies, the standby notices and takes over — **working
again in about a hundred seconds**, with your accounts, integrations and last
known states already there.

That number is for losing the **machine**. Home Assistant failing on its own,
with the machine still up, takes about **ten and a half minutes** instead. That
is deliberate: a short wait would hand your house to the other box every time
Home Assistant was slow to restart.

---

## Before you install it

**Read these three things.** They are the ones people are surprised by.

1. **It is not a backup.** It is a replica: delete something on the active
   machine and it is deleted on the standby too. Keep taking backups.
2. **Both machines briefly run your automations** during a handover. Anything
   that sends a message, unlocks a door or costs money can happen twice.
3. **USB sticks plugged into a machine usually do not follow.** That is a
   plug, not software. If your Zigbee or 433 MHz radio is directly attached,
   read the radio guide **before buying anything** — there are ways round it,
   and they need hardware you may not have.

**Is this for you?** If you have never run Home Assistant, not yet — this
replicates whatever you have, mistakes included. If you run it in Docker on a
machine you can get a root shell on, and losing it for an evening would
genuinely bother you, then yes.

---

## What you get

| | |
|---|---|
| **Your state survives** | Entities come back as they were, restored *before* automations start, so nothing acts on stale readings |
| **Your logins survive** | The standby holds a sealed copy of `.storage` — you are not locked out of your own house |
| **Your long-term history survives** | Years of energy and climate data cross between nodes. Recent logbook detail does not — see below |
| **It tells you when it is unhappy** | Problems appear in **Settings → Repairs** with the action named, not buried in a log |
| **A cluster page** | One view of both nodes, with the controls that matter |

---

## The honest limitations

* **Recent history does not replicate.** Long-term statistics (the Energy
  dashboard, long graphs) do. The last ten days of detail do not — they churn
  about sixty times faster, and carrying them was measured at 4 GB a day.
  After a failover your long graphs are intact and the logbook starts fresh.
* **History replication needs one manual step, once** — a seed file you copy
  across yourself. The integration writes it and tells you the command.
* **If the shared noticeboard goes away, the cluster silently stops being able
  to fail over.** Both machines keep running normally. Watch
  `binary_sensor.<node>_backend` and alert on it.
* **There is no automation leader election.** During the overlap window both
  instances fire automations. This is deliberate and documented.
* **Radios follow only if they are on the network.** Over USB-over-IP they move
  with the failover; two of the three on the reference machine do. One plugged
  straight into a machine cannot, and that is a plug rather than software. Read
  the radio guide before buying hardware.

---

## Getting started

1. **Read the [radio guide](https://github.com/bionic-tech/ha-cluster-state-sync/blob/main/docs/GUIDE-radios.md)** if you have Zigbee, Z-Wave or 433 MHz hardware. It decides what is possible.
2. **Add the integration** and follow the wizard. It asks what you want in
   plain language and refuses combinations that cannot work.
3. **Follow the [installation runbook](https://github.com/bionic-tech/ha-cluster-state-sync/blob/main/docs/RUNBOOK-installation.md)** for the host side.

Every term used in the documentation is defined in the
**[glossary](https://github.com/bionic-tech/ha-cluster-state-sync/blob/main/docs/GLOSSARY.md)** — no prior Home Assistant, Docker or
clustering knowledge assumed.

---

## If something goes wrong

**[TROUBLESHOOTING.md](https://github.com/bionic-tech/ha-cluster-state-sync/blob/main/docs/TROUBLESHOOTING.md)** is organised by symptom — "the standby
promoted then demoted itself", "radios do not work after promotion", "history
is not replicating" — with the fix for each, including which of two
reasonable-looking fixes is the wrong one.

This project keeps a register of **27 traps it has actually fallen into**,
nearly all of which reported success while doing nothing. If you are changing
anything, read it first.

---

**Licence:** AGPL-3.0 · **Requires:** Home Assistant 2026.6.4+, Docker, and a
Redis-compatible store both machines can reach.
