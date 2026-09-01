# RTO measurement tools

Two analysers that answer **H1 — what is the cold-boot RTO?** from data
`node-a` has already written, without a failover drill and without touching
the standby.

H1 is the gating decision. Until it is measured, "cold standby is the
recommended default" is an assumption inherited from
[ADR-001](../../docs/adr/ADR-001-active-passive-topology.md), not a finding. If
cold misses the 2.5-minute budget, warm becomes mandatory and every risk
attached to the firewall bundle goes live.

## The result is asymmetric

These tools can **disprove** cold standby. They cannot confirm it.

If Home Assistant's own startup already eats the budget, cold is dead and no
further measurement is needed — a decisive answer, for free. If it comes back
comfortably under, cold stays *viable* and you learn how much headroom the
unmeasured parts have to fit into.

What they do **not** measure:

| Excluded | Why it matters |
|---|---|
| Container start → first log line | Docker start latency sits outside HA's clock |
| The snapshot restore | `cluster_state_sync` has never run; T7 bounds it separately |
| Keepalived detection + notify scripts | Happens before the container is asked to start |
| Cold-cache and reconnection penalty | These are figures from a **warm** host restarting itself |

That last one is the big one. A cold node reconnects to every device from
scratch; a warm one already holds those connections. Retry and backoff extend a
cold start in a way a running node's history never shows.

## Both are standard library only

No installation on the host. Python 3.11+ (`node-a` has it).

---

## 1. `ha_log_rto.py` — Home Assistant's own startup time

Parses `home-assistant.log` for the four timing lines HA emits at INFO or
above. `node-a`'s `configuration.yaml` sets `logger: default: info`, so all
four are present at default verbosity.

```bash
python3 -m tools.rto.ha_log_rto \
    /mnt/docker_data/homeassistant/config/home-assistant.log
```

Or without the package layout, straight on the host:

```bash
python3 ha_log_rto.py /mnt/docker_data/homeassistant/config/home-assistant.log
```

Options: `--budget` (seconds, default 150) and `--top` (slow domains to list).

**Limitation: sample size.** Home Assistant defaults to `backupCount=1`
(`bootstrap.py:686`), so the file holds the current run and the previous one
only. On node-a that was a single run across 18 days of uptime. For a
distribution rather than a data point, use the recorder below.

> **Ordering gotcha, for anyone editing this parser.** `Starting Home
> Assistant <version>` is emitted by `hass.async_start()` **after** bootstrap
> finishes, so it is the *last* line of a startup, not the first. A run reads
> `Setup of domain …` → `Home Assistant initialized in Xs` → `Starting Home
> Assistant`. Treating the `Starting` line as the opening delimiter — which is
> the intuitive reading, and what the first version of this tool did — silently
> discards every timing line and reports a healthy instance as "never finished
> starting".

### Measured on node-a, 2026-08-25

**27.19s**, 18% of a 150s budget, 194 domains, slowest `wled` at 0.97s. See
READINESS §3.4.

The per-domain breakdown is the actionable part — if the total is too big, it
names which integrations to trim.

---

## 2. `ha_recorder_rto.py` — real restart times, from history

Sharper on both counts. The recorder keeps every run inside the purge window
(**10 days** by default — `node-a` has no `recorder:` block, so it runs the
default SQLite recorder), and it can measure *time to useful* rather than time
to a function returning.

Two measurements:

- **Downtime between runs** — from `recorder_runs`, one row per HA run. The
  empirical distribution of every restart you have already performed.
- **Time to a usable entity set** — from `states`, how long after a restart
  before a quorum (default 90%) of the entities present beforehand had written
  a real state again. An entity that returns `unavailable` does not count.

```bash
# Take a consistent copy first — do not analyse the live file
sqlite3 /mnt/docker_data/homeassistant/config/home-assistant_v2.db \
    ".backup /tmp/ha-rto.db"

python3 -m tools.rto.ha_recorder_rto /tmp/ha-rto.db
```

Options: `--budget` (seconds, default 150) and `--quorum` (fraction, default
0.9).

The database is opened `mode=ro` and there is a test asserting a write raises,
so it cannot disturb a running recorder. Copy anyway — a live database has a
WAL, and `.backup` gives you a consistent snapshot rather than a torn read.

### Reading the output

`closed_incorrect` marks a run that did not shut down cleanly. Its `end` is
backfilled from the last state the recorder managed to persist, so that gap's
downtime is **overstated** by however long the final write gap was. Those rows
are flagged rather than filtered, because an unclean stop is the case a
failover most resembles.

---

## What to do with the numbers

| Outcome | Meaning |
|---|---|
| Worst startup ≥ budget | **Cold is dead.** Decide warm, and the firewall bundle moves onto the critical path. |
| Worst startup ≪ budget | Cold stays viable. The remainder — container start, restore, reconnection — must fit in what is left, and still needs H1 proper to confirm. |

Either way, record the figure in
the internal readiness assessment §3.2 against H1, and update
ADR-001's implementation-status note.

## Tests

`tests/test_rto_log.py`, `tests/test_rto_recorder.py`, `tests/test_rto_cli.py`.

The recorder tests build a real SQLite database with the real column
definitions and run the real queries against it. Nothing is mocked — the whole
value of the tool is that its SQL is correct, and a double would assert only
that we called ourselves.

Every log line and column referenced in these modules was verified against the
installed Home Assistant 2026.6.4 source, with file and line cited in the
module docstrings, rather than recalled.
