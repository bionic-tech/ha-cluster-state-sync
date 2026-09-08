"""Consistent copies of the recorder database, so history survives a failover.

Both nodes keep their own SQLite recorder, so a promoted standby opens a book
that stops on the day it was built. Every graph has a gap. That was true from
the start and went unwritten until 2026-09-08.

**This is not Litestream and deliberately not a WAL replicator.** The outcome
asked for was "whatever is needed so history survives a failover", and SQLite's
own `VACUUM INTO` already satisfies it:

* it produces a **consistent** copy of a live database in one statement -- no
  torn file, no cooperation needed from Home Assistant, no writer to stop;
* it **compacts** on the way out (measured: 2.21 GB source -> 1.58 GB copy);
* it is one line of stdlib, so there is no WAL parsing to get subtly wrong --
  and a bug in hand-written WAL shipping corrupts history *silently*, which is
  the one failure this project refuses to build.

Measured on a real 3,595-entity estate, 2026-09-08: **11.7 seconds**, which is
1.3% duty at a 15-minute cadence. Time is not the constraint. **Write endurance
is** -- each snapshot rewrites the whole compacted database, so 15 minutes is
152 GB/day. See `storage.py`, which detects the disk and picks a kind default.

What it costs: you lose at most one interval of history. For graphs, that is
nothing. For the last hour of a house's state you have the *snapshot mirror*,
which is what the rest of this integration is for.

**The copy lives in the config directory**, beside everything else, on purpose.
Putting it on a cheaper disk would spare the wear and introduce drift: the
go-bag, the swap script and every operator's mental model assume one location,
and "I'll just copy it from A to B" is how that assumption breaks at 3am. The
answer to wear is the interval, not the location.

🚨 **It must be excluded from the go-bag.** At 1.58 GB it is three times the
512 MB fileset cap, and the go-bag is swapped synchronously during a promotion
-- carrying a database there would put it on the RTO critical path, which is
exactly what this design exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
import pathlib
import sqlite3

_LOGGER = logging.getLogger(__name__)

#: Where the copy lands, relative to the config directory. Dot-prefixed so it
#: sorts out of the way, and named for what it is rather than when it was made
#: -- a single rolling file, because the peer rsyncs deltas against it and a
#: dated series would transfer the whole database every time.
SNAPSHOT_NAME = ".cluster_sync_recorder.db"

#: Written beside the snapshot while it is being produced. A consumer that sees
#: this must not ship the snapshot: `VACUUM INTO` writes a new file rather than
#: updating in place, so a copy taken mid-write is a partial database.
SNAPSHOT_LOCK_NAME = ".cluster_sync_recorder.writing"

#: The recorder's own database, as Home Assistant names it.
RECORDER_DB_NAME = "home-assistant_v2.db"


@dataclass(frozen=True)
class SnapshotResult:
    """What a snapshot attempt actually did. Never a bare bool."""

    ok: bool
    bytes_written: int = 0
    seconds: float = 0.0
    error: str | None = None
    #: When the copy completed. The age sensor needs this; without it a
    #: "snapshot age" would be the age of the process, not of the data.
    taken_at: datetime | None = None


def snapshot_path(config_dir: str) -> pathlib.Path:
    return pathlib.Path(config_dir) / SNAPSHOT_NAME


def _lock_path(config_dir: str) -> pathlib.Path:
    return pathlib.Path(config_dir) / SNAPSHOT_LOCK_NAME


def take_snapshot(config_dir: str, *, db_name: str = RECORDER_DB_NAME) -> SnapshotResult:
    """Produce a consistent copy of the recorder database. **Blocking.**

    Call from an executor -- `VACUUM INTO` took 11.7s on a 2.21 GB database and
    the event loop must not wait on that.

    Opens the source **read-only**. This never writes to the live database, so
    the worst case for Home Assistant is unchanged behaviour and a wasted read.

    Every failure is caught and reported. A recorder pointed at PostgreSQL or
    MariaDB has no SQLite file at all, and that is a perfectly ordinary
    configuration -- the correct response is "not applicable", not an exception
    on a timer.
    """
    src = pathlib.Path(config_dir) / db_name
    if not src.is_file():
        # Shared-database installs land here, and so does a recorder that has
        # been disabled. Neither is an error worth logging every interval.
        return SnapshotResult(False, error="no SQLite recorder database")

    dst = snapshot_path(config_dir)
    tmp = dst.with_suffix(".tmp")
    lock = _lock_path(config_dir)
    started = datetime.now(tz=UTC)

    # Clear any wreckage from a previous run before claiming the lock, so a
    # crash mid-snapshot cannot wedge this permanently.
    for stale in (tmp, tmp.with_name(tmp.name + "-wal"), tmp.with_name(tmp.name + "-shm")):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
        except OSError as err:
            return SnapshotResult(False, error=f"cannot clear {stale.name}: {err}")

    try:
        lock.write_text(started.isoformat() + "\n", encoding="utf-8")
    except OSError as err:
        return SnapshotResult(False, error=f"cannot write the lock: {err}")

    try:
        # `mode=ro` and `immutable=0`: read-only, but still honouring the WAL,
        # so this sees committed data rather than a stale main file.
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=120.0)
        try:
            con.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            con.close()
        size = tmp.stat().st_size
        # Rename last, and only once the copy is complete. A consumer either
        # sees the previous good snapshot or the new one, never a partial file.
        os.replace(tmp, dst)
    except (sqlite3.Error, OSError) as err:
        for wreck in (tmp,):
            try:
                wreck.unlink()
            except OSError:
                pass
        return SnapshotResult(False, error=f"{type(err).__name__}: {err}")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass

    elapsed = (datetime.now(tz=UTC) - started).total_seconds()
    _LOGGER.info(
        "Recorder snapshot written: %.2f GB in %.1fs (%s)",
        size / 1e9,
        elapsed,
        dst.name,
    )
    return SnapshotResult(True, bytes_written=size, seconds=elapsed, taken_at=datetime.now(tz=UTC))
