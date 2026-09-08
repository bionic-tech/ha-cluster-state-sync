# ADR-010: History continuity by whole-copy snapshots, not replication

**Status:** Accepted — producer, detection, scheduler and promotion hand-over built. **The snapshot is deliberately not replicated**; see §8.
**Date:** 2026-09-08
**Deciders:** mmanning (project owner)

## Context

Both nodes keep their own SQLite recorder, so a promoted standby opens a database that stops on the
day it was built and **every graph has a gap**. True since the first release, and unwritten until a
documentation review found it — nobody had ever been hurt by it, which is itself worth noting.

The obvious answer, Litestream, was rejected after measurement. The full reasoning is in
25-recorder-replication; the decisive numbers:

| | |
|---|---|
| Recorder database | **2.21 GB** |
| Go-bag cap | 512 MB |
| Measured cold-boot RTO | ~102 s to `Home Assistant initialized` |
| `VACUUM INTO` on the live database | **9.9 s → 1.59 GB** (it compacts) |
| Database change rate | ~3.5 MB/hour |

## Decision

### 1. Whole consistent copies, not a write-ahead-log replicator

`VACUUM INTO` produces a **consistent copy of a live database in one statement**, from the Python
standard library. No writer to stop, no cooperation from Home Assistant, no torn file.

**Rejected: writing a WAL replicator, in Go or in Python.** The owner asked for Litestream parity
and then re-scoped it by outcome — "whatever is needed so history survives a failover" — which this
already satisfies. Hand-written WAL shipping fails *silently*, producing a database that looks fine
until it is queried, and that is the one failure mode this project refuses to build. No mature
pure-Python equivalent exists (checked: `pylitestream` on PyPI is an unrelated data-streaming
engine).

**Cost, accepted:** you lose at most one snapshot interval of history. For graphs that is nothing.

### 2. The interval is the wear decision, not the location

Each snapshot rewrites the whole compacted database, so cadence is write endurance:

| Interval | Writes/day |
|---|---|
| 15 min | **152 GB** |
| 30 min | 76 GB |
| 60 min | 38 GB |

**The copy stays in the config tree**, at the owner's instruction, beside everything else this
integration touches. Putting it on a cheaper disk would spare the wear and introduce drift — the
go-bag, the swap script and every operator's mental model assume one location, and "I'll just copy
it from A to B" is how that assumption breaks at 3am.

So `storage.py` detects the disk from **inside the container** (`os.stat().st_dev` →
`/sys/dev/block/<maj>:<min>` → `queue/rotational`), defaults flash to 30 minutes and spinning to 15,
and warns below 20 on flash **in GB per day, never a predicted lifetime** — rated endurance is a
datasheet figure the code cannot read. Unknown storage gets the cautious default and **no warning**.

### 3. 🚨 It is excluded from the go-bag, for two independent reasons

At 1.59 GB it is three times the fileset cap — and the go-bag is swapped **synchronously during a
promotion**, so carrying a database would put it on the ~102 s RTO critical path for data that is
not needed to bring the house back at all.

### 4. History is continuous without any merge

The owner asked whether the returning node could be sent "only the missing data", so failback loses
nothing. **Merging two divergent recorder databases is not realistically achievable** — `states`,
`state_attributes`, `statistics` and `events` all carry autoincrement keys and cross-references
(`attributes_id`, `old_state_id`, `metadata_id`), so a merge means remapping every ID, and getting
it subtly wrong yields a database that looks fine until queried.

**It is also unnecessary.** The snapshot is a *whole copy* and replication follows leadership:

```
t0  tiger1 leads, history H                    snapshots ship to tiger2
t1  tiger1 dies; tiger2 promotes, restores H,
    and keeps recording                        tiger2 holds H + H′
t2  tiger1 returns as STANDBY                  direction flips; tiger2 ships to tiger1
t3  failback: tiger1 restores                  H + H′ + H″   ← continuous
```

Which is exactly why **the snapshot wins over the standby's own database**. That local database is
real — 42 MB measured on the cold standby, accumulated across test promotions — and keeping it on
failback is the only way to *lose* H′.

**Restore only if the snapshot is newer** than the local database; otherwise keep local and mark
degraded. This guards the unusual sequence where a node would otherwise promote onto an *older*
copy and discard more recent local rows.

### 5. A soft gate on failback, never on promotion

A **planned** handover waits for a fresh snapshot to land. An **emergency** promotion — the peer
actually died — proceeds regardless and marks degraded. This is the D4 asymmetry already proven for
the go-bag: *a degraded go-bag marks degraded, it never blocks promotion.* A hard gate would leave a
house with no Home Assistant while a 1.6 GB file copies, which is the failure the cluster exists to
prevent.

"Fresh" is **twice the snapshot interval** — self-adjusting, tolerant of one missed cycle, and no
new setting to explain.

### 6. Warm standby is incompatible, and provably so

`recorder.disable` only sets a flag that makes `_process_one_event` drop events; the database
connection stays open, and there is no reload service. **Swapping the recorder database under a
running Home Assistant is impossible — a restart is mandatory.**

Therefore warm promotion *with* a swap is `docker stop` + swap + `docker start` + boot, while cold
promotion is `docker start` + boot. **Warm is strictly slower**, because it adds a stop it does not
otherwise pay. Warm standby plus recorder replication is not awkward; it is pointless.

So the wizard asks about history **early**, and then offers only combinations that work:

| History matters? | Database | Standby models offered |
|---|---|---|
| yes | **shared** (PostgreSQL / MariaDB) | cold **or** warm |
| yes | **dedicated** (SQLite per node) | **cold only** |
| no | either | cold or warm |

The later screen is **filtered**, so a broken combination cannot be selected at all rather than
being warned about. Nobody has to understand why.

### 7. It logs; it never claims the degraded marker

The first implementation marked the go-bag's degraded marker on a missing or older snapshot. That
was wrong and a test caught it: `mark()` is **first-reason-wins**, so a missing history file
pre-empted `stale` — letting "your graphs will have a hole" outrank "your identity may be wrong",
which is the one signal an operator acts on at 3am.

Recorder health is reported by its own diagnostic (`sensor.<node>_recorder_snapshot_age`) and by
the log. The shared marker stays reserved for identity and go-bag faults.

### 8. The snapshot is NOT shipped to the peer — and that is the honest position

A first implementation shipped it with **rsync over SSH**, pulled by the follower. That was wrong
and the project's own guard caught it (`test_the_tier_one_rsync_is_never_emitted`, owner decision
2026-08-30). Recorded here because the reasoning matters more than the code:

- **rsync runs on the host.** It works on Docker, might work on Core, and **cannot work on Home
  Assistant OS at all** — there is no host shell. Every host-side component narrows who can use
  this project.
- **It would have restored SSH trust between hosts**, which removing tier 1 deliberately ended. The
  bundle's own documentation says "Nothing is lost by the rsync's removal". Something would have
  been.
- **The integration has zero `subprocess` by design**, so it could not have driven this itself. That
  constraint was a signal, not an obstacle to route around.

**Our own mechanism cannot carry it either, and that is a real limitation rather than an excuse.**
`fileset.py` seals each file as a **single whole blob** — there is no chunking — and `max_bytes`
aborts the entire publish (`nothing published`) when the total is exceeded. A 1.59 GB database
would therefore not merely be large; it would break identity replication, which is the one thing
the go-bag exists to protect.

So the snapshot is **produced locally and not replicated**. It is still worth having: a consistent,
compacted, restore-ready copy of the history database that costs ~10s and never touches the live
one.

**For history that actually survives a failover, the answer is a shared database** — Home
Assistant's own `db_url`, pointing both nodes at one PostgreSQL or MariaDB. Zero new code, zero
host components, works on every deployment model including HA OS, and it removes the second
database rather than trying to synchronise it. This is what the wizard steers toward (§6), and it
is why that steer exists.

**If per-node replication is ever wanted**, the native route is to add **chunking** to `fileset.py`
so large changing files ship as content-addressed pieces and only changed chunks cross. That is an
extension of the mechanism we already built, in Python, inside Home Assistant — and it is a
deliberate piece of work with its own Valkey sizing consequences, not something to bolt on.

## Consequences

**Good.** No new daemon, no third-party binary, no third host-side component — the standby's
existing `cluster-fileset-pull` timer carries the file, because the integration has zero
`subprocess` by design and cannot rsync. History is continuous across any number of failovers
without a merge.

**Accepted.** Up to one interval of history is lost per failover. Snapshots cost real disk writes,
which is why the interval is detected and defaulted rather than fixed.

**Unresolved.** Whether to shorten `purge_keep_days`: cutting 10 → 3 days saves ~0.85 GB and lands
at ~1.4 GB, because ~0.49 GB is long-term statistics that **no retention setting purges** and which
grow forever.

## Related

- 25 — Recorder replication — the options considered and rejected
- [ADR-001](./ADR-001-active-passive-topology.md) — the RTO budget this must not enter
- [ADR-008](./ADR-008-liveness-signals-must-prove-measurement.md) — why the wear warning states GB/day

---

## Addendum, 2026-09-08 — what was actually built, and the two things this ADR got wrong

The decision above stands: a shared database is still the right answer, and it is still what the
wizard steers toward. Two of its supporting claims did not survive contact with the real estate.

### Wrong #1: the file-shipping route was not native, and could not be made native

The plan was to ship the `VACUUM INTO` snapshot on the existing `cluster-fileset-pull` timer. That
timer runs `docker run` on the host — which works on Docker, might work on Supervised, and cannot
work on Home Assistant OS. The transport that was actually written used **rsync over SSH**, which
would have been this project's first direct node-to-node dependency: every other mechanism here
reaches Valkey and nothing else, which is precisely why its failure modes are as simple as they
are. It was removed on 2026-09-08 rather than shipped.

### Wrong #2: "chunking is the native route" — the measurements say otherwise

Chunking a 1.59 GB database was going to be the extension. Then the database was measured, and the
premise collapsed. From the live 2.2 GB recorder, 3,595 entities:

| table | rows | growth |
|---|---:|---:|
| `statistics` — multi-year, Energy dashboard | 6,409,578 | **+5,535/day** |
| `statistics_short_term` — 10-day detail | 804,079 | +68,976/day |
| `states` — 10-day raw history | 3,977,092 | +324,517/day |

**The part anyone grieves losing is half a megabyte a day.** Years of energy and climate history
live in `statistics`, which no retention setting ever purges. The 1.59 GB is almost entirely
`states` — ten days of noise that churns sixty times faster and that a purge deletes anyway. Block
replication of the whole file was measured at **4 GB a day through Valkey** even at the optimal
chunk size, to carry half a megabyte of data worth having.

So chunking was not built. Row-level replication of `statistics` alone was.

### What ships instead

Long-term statistics cross as **rows**, through the same Valkey, the same sealed envelope and the
same key as the go-bag. No new transport, no new port, no node-to-node trust, and identical
behaviour on Docker, Supervised and HA OS.

- A **rolling window**, not a delta log. A delta log is only correct while the follower keeps up,
  and a cold standby is off for weeks by design. Every pass republishes every row in the window,
  and applying is idempotent, so a standby that has been off for a fortnight catches up from one
  fetch. Measured: 7 days = 0.99 MB gzipped, 30 days = 4.48 MB, 90 days = 12.51 MB; the 30-day
  export takes 1.7 s against the live database.
- The payload states the **floor it covers**, so a standby that has been off *longer* than the
  window is told. Without that, the months in between exist on neither node, no later window
  contains them, and the replica looks perfectly healthy.
- Rows are remapped by `statistic_id`, **never** by `metadata_id`, which is local to each database.
  Copying those ids across would attach one node's readings to another node's sensors.
- It **refuses across a recorder schema mismatch**. A gap in history beats a database that opens
  cleanly and is wrong.

### The price, stated rather than implied

**Raw `states` does not cross.** After a failover the Energy dashboard and every long-range graph
are intact; the logbook and the last ten days of detail start fresh. That is the trade this ADR's
own measurements argue for, and the wizard says it in those words rather than burying it here.

### The one manual step

Six and a half million existing rows cannot arrive at half a megabyte a day, so the standby needs a
one-off seed. Moving a file that size is the single job with no native Home Assistant answer: no
supported API takes an upload of it, and a node-to-node copy would need exactly the SSH dependency
that was rejected above. So the integration writes the file and a **button** tells the operator
what to run — instructions in a persistent notification, not a paragraph in a document, because
AR-0040 was a step that quietly did not happen whose only symptom was a line nobody read.

The seed is adopted only after SQLite's own `quick_check` passes and its schema version reads. A
484 MB copy that was still running would otherwise become a store that opens cleanly and is missing
history.

### The standby cannot raise its own alarm

In the cold model the standby's Home Assistant is stopped: no logbook, no repairs panel, no
entities. Everything it discovers — a schema mismatch, a gap, a missing seed — would die on a host
nobody reads. So it writes a status line back to Valkey and **the leader raises the repair on its
behalf**. This is the only place in this integration where data flows standby → leader, and that is
why it exists.

### Fail-back needs no second copy

`cluster-fileset-swap.sh` **moves** the store into place as the recorder at promotion rather than
copying it. A left-behind copy would freeze at that date and be re-installed, stale, at some later
promotion. When the node goes back to standby, `statistics_pull.py` finds no store, finds a local
recorder holding all of those statistics plus everything recorded while it was leader, and rebuilds
from that.

### Verified on the live pair, 2026-09-08

Not asserted — run, on node-a (leader, Home Assistant up) and node-b (cold standby, container
stopped, `vrrp-state=BACKUP`), through the generated bundle script:

| step | result |
|---|---|
| Publish the 30-day window from the live recorder | 203,021 rows, 1,034 statistics, 4.48 MB sealed, 1.7 s |
| Write the seed | 6,409,818 rows, 484 MB, 4.8 s |
| Copy it to the standby | 17.5 s over the LAN |
| Standby adopts it | integrity check passed, schema 53 confirmed |
| Delete 5,000 recent rows, re-run the pull | 5,000 applied; history reached forward again |
| Standby's status read back on the leader | `{"state":"ok","applied":5000,"schema_version":53}` |
| Store's schema vs the live recorder's | **13/13 tables, 20/20 indexes, exact match** |
| Recorder in the running HA image | schema 53 — current, no migration, starts on it |

The store holds 6,409,818 rows across 1,034 statistics reaching back to 2021-12-21.

Two defects surfaced only because it was run for real, neither of which any unit test would have
caught: the standby's installed `crypto.py` and `resp.py` predated the feature and had to be
reinstalled with it (a whole-bundle reinstall, not a file drop), and a rehearsal config that named
the config directory with the wrong key silently fell back to the default path — caught only
because the program reports *which* path it looked in rather than reporting "not seeded".
