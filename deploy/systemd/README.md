# systemd units — generated, not authored

**Do not edit these files.** They are produced by
`custom_components/cluster_state_sync/bundle.py` and reproduced here so the
units that run as root on every cluster host are visible, reviewable and
version-controlled without having to run the wizard to see them.
`tests/test_systemd_units.py` regenerates them and fails if these copies drift,
so they cannot silently fall behind the generator.

To change a unit, change the function that emits it — `_promoter_service`,
`_promoter_timer`, `_fileset_pull_service`, `_fileset_pull_timer`,
`_statistics_pull_service`, `_statistics_pull_timer` — then run:

```bash
python -m tests.regenerate_systemd_units   # or just: pytest tests/test_systemd_units.py
```

## Why they live here rather than in a fleet-config repo

A fleet-wide audit on 2026-09-08 found 72 of 92 host-local systemd units
existed on one host and in no repository at all, and mirrored eight of ours
into the homelab repo so they were at least drift-checked. That was the right
instinct and the wrong home for these particular files, for one reason: **these
are build artefacts of this repo's generator, not hand-written host config.**

A drift check against a generator reports a failure every time the generator
legitimately changes. The hazard is not the noise — it is the correction it
invites: someone restoring a host to the mirrored copy, silently reverting a
deliberate change to the code that decides when a house fails over. Two of
these units were about to gain siblings (`cluster-statistics-pull.*`) that no
mirror would have known about.

So they are owned here, beside the runbook that owns the install story, and the
test above is the drift check.

## What is deliberately NOT captured here

`/etc/cluster-sync/` also contains **`cluster-fileset.key`** and
**`cluster-fileset-valkey.env`**. Those are secrets. No process that captures
this directory — here, in a fleet repo, or in a backup that leaves the host —
may include them. If you ever automate capturing that directory wholesale,
exclude or template those two **first**.

## The values these were generated with

Units are emitted from a config, so this reference pins one. Five of the six
take no configuration at all and are byte-identical on every install. The
exception is `cluster-statistics-pull.timer`, whose `OnUnitActiveSec` follows
the configured publish interval; it is shown here at the default of 30 minutes.

`ExecStart` paths are `/etc/cluster-sync/`, which is where
`docs/RUNBOOK-installation.md` puts the bundle.
