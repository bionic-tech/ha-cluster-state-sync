# Roadmap

What is being worked towards, what is decided but unbuilt, and what is known to
be missing.

**No dates.** This is one person's project and a promised date would be a guess
dressed as a commitment. Items are ordered by how much they matter, not by when
they will arrive.

Things that are **true, unfixed and deliberate** are not here — they are in
[`docs/KNOWN-LIMITATIONS.md`](docs/KNOWN-LIMITATIONS.md), because a limitation
listed on a roadmap reads like a promise to fix it, and some of these will not
be fixed.

---

## Next

### Package the promoter as a Home Assistant add-on

The one host-side component is a systemd timer, which means this cannot be
installed on Home Assistant OS at all — and the project's own runbook said
automatic failover was "not possible" there.

**That claim was wrong.** The Supervisor API documents `POST /core/stop`,
`/core/start`, `/core/restart`, `/host/reboot`; an add-on can request
`hassio_api` with `hassio_role: homeassistant`.

This is the difference between a project two people can run and one anybody can
install. It is the largest single item here.

### Give the promoter a voice when it releases the lease

Today the most consequential thing it does — handing your house to the other
machine — is visible only in a journal. The integration can say when it was
promoted; the promoter cannot say why it let go.

### Make the failover budget claim honest in the interface

A lost machine is measured at around 102 seconds to a working standby. Home
Assistant dying on its own takes closer to ten minutes, by design. The
documentation says so; the panel does not, and a number shown without that
distinction is the wrong number.

---

## Decided, not built

Designs that are settled. The reasoning is done; the work is not.

- **Radios on promotion** — the device-claim ordering was settled in design and
  never implemented as specified.
- **Let a standby inherit the cluster half of its settings** — today every
  cluster-wide value is typed twice, once per node, and a mismatch is silent.
- **Re-home the Bluetooth adapter on promotion** rather than disabling it.
- **A node registry, instead of a fixed cap on standbys** — the current limit is
  a constant, not a decision.

---

## Known gaps

Not deliberate. Not yet fixed. **Listed because they are known and tracked** —
every one of these was found by running the thing rather than reported by
somebody it surprised, and none is waiting quietly to be discovered by you.

A roadmap that lists only pleasant futures is one nobody believes.

- **A dead radio daemon is invisible to the failover design.** A radio can stop
  receiving while every health check stays green. Partially addressed — and the
  partial fix does not work on every installation, which was the more useful
  finding.
- **The promoter can die while the machine stays up.** The lease expires, the
  peer promotes, and you have two live instances rather than a handover.
- **The options flow reaches only part of the configuration.** Some settings are
  editable only by hand-editing the config entry.
- **`ha_config_path` is trusted rather than validated** in the wizard, so a typo
  surfaces much later as something that looks unrelated.
- **TLS material is read on the event loop in one remaining place.** Home
  Assistant warns about it. Fixed for the main connection path in 0.5.2.

---

## Documentation

- **No physical or topology diagram.** The only diagram is a data flow, which
  does not help somebody working out what to plug in where.
- **Sizing guidance stops at 200 entities.** The reference installation runs
  several times that, so the guidance is untested at the size it is aimed at.
- **No playbook for double-firing automations** on a large installation. The
  overlap window is documented; what to actually do about it is not.

---

## Further out

Ideas with a case but no design.

- **A second radio on the standby**, with a way to map one onto the other. The
  hard part is that USB paths are not stable and device IDs differ between
  physical units, so neither obvious approach works.
- **Matter over Thread as the way out of radio custody entirely.** Raised
  independently by two readers on 2026-09-11: redundant Thread border routers
  plus Matter multi-admin would make the hardwired-stick problem moot, because
  there would be no stick. Not designed, and it only helps devices that speak
  Matter — but it is the first suggestion that removes the constraint rather
  than working around it.
- **Presence detection via ESP32 Bluetooth proxies**, so presence survives a
  failover without depending on either machine's own adapter.
- **A shared Postgres for the recorder**, so history is genuinely continuous
  rather than replicated in windows.
- **Turn the bundle step into a real install assistant** rather than a
  generator that hands you files and a list of commands.

---

## How this list is kept

It is derived from a longer internal list that also tracks work specific to the
installation this was built on — that half is not useful to anybody else and is
not published.

If something here matters to you, say so on an issue. What people actually hit
reorders this list faster than anything else does.
