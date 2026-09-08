"""Replicate long-term statistics, and nothing else.

**Why only statistics.** Measured on a real 3,595-entity estate, 2026-09-08:

```
statistics            (multi-year, Energy dashboard)  6,409,578 rows   +5,535/day
statistics_short_term (10-day detail)                   804,079 rows  +68,976/day
states                (10-day raw history)            3,977,092 rows +324,517/day
```

The data anyone actually grieves losing -- years of energy and climate history --
grows by about **half a megabyte a day**. Raw `states` is ten days of noise that
churns sixty times faster, and replicating it block-wise was measured at **4 GB
a day through Valkey** even at the optimal chunk size. So this ships the part
worth having and lets the rest gap (ADR-010).

**Rows are remapped, never copied by id.** `statistics.metadata_id` points at a
row in `statistics_meta` whose numeric id is local to each database. Copying
those ids across would silently attach one node's readings to another node's
sensors. Everything is keyed on `statistic_id` -- the stable string name -- and
the numeric id is resolved on arrival.

🚨 **It refuses to apply across a schema mismatch.** Home Assistant records its
recorder schema version in `schema_changes`; two nodes on different Home
Assistant versions can differ. Writing rows shaped for one schema into another
is how history gets silently corrupted, so a mismatch stops and reports rather
than guessing. A gap in history beats a corrupted database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import json
import logging
import pathlib
import sqlite3
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: Only these two tables. `statistics_short_term` is deliberately excluded: it
#: holds ten days of five-minute detail, churns fourteen times faster than the
#: long-term table, and is regenerated from live data anyway.
META_TABLE = "statistics_meta"
STATS_TABLE = "statistics"

#: The columns carried. Named explicitly rather than `SELECT *` so a schema
#: addition upstream cannot silently change the payload's shape -- and so the
#: schema guard has something concrete to protect.
META_COLUMNS = (
    "statistic_id",
    "source",
    "unit_of_measurement",
    "has_mean",
    "has_sum",
    "name",
)
STATS_COLUMNS = (
    "created_ts",
    "start_ts",
    "mean",
    "min",
    "max",
    "last_reset_ts",
    "state",
    "sum",
)


class SchemaMismatch(RuntimeError):
    """The two databases disagree about their recorder schema version."""


@dataclass
class SyncPayload:
    """What crosses between nodes. Small by construction."""

    schema_version: int
    meta: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    #: The highest `start_ts` included, so the next export resumes from here.
    watermark: float = 0.0
    #: The floor of the window: this payload contains EVERY row with
    #: `start_ts > covers_from`. Without it a follower cannot tell "nothing
    #: new" from "you have been offline longer than the window and there is a
    #: hole in your history" -- and the second one silently becomes the first.
    covers_from: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.meta

    def to_bytes(self) -> bytes:
        """Serialise for the wire: JSON, gzipped.

        Gzip because the payload is a JSON array of near-identical short
        records, which is the case compression was invented for -- a month's
        window measures ~33 MB raw and roughly a tenth of that compressed. It
        travels through Valkey, whose runbook recommends `maxmemory 1gb`.
        """
        return gzip.compress(
            json.dumps(
                {
                    "schema_version": self.schema_version,
                    "meta": self.meta,
                    "rows": self.rows,
                    "watermark": self.watermark,
                    "covers_from": self.covers_from,
                },
                separators=(",", ":"),
            ).encode("utf-8"),
            mtime=0,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> SyncPayload:
        """Rebuild a payload. Raises `ValueError` on anything malformed.

        Every field is checked here rather than at first use. These bytes have
        already been authenticated by the time they arrive, so a failure means
        the *publisher* emitted something wrong -- and a `TypeError` raised
        deep inside the apply loop, halfway through a transaction, is a much
        worse way to find that out.
        """
        try:
            data = json.loads(gzip.decompress(raw).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
            raise ValueError(f"statistics payload did not decode: {err}") from err
        if not isinstance(data, dict):
            raise ValueError(f"statistics payload is not an object: {type(data).__name__}")
        meta, rows = data.get("meta"), data.get("rows")
        if not isinstance(meta, list) or not isinstance(rows, list):
            raise ValueError("statistics payload has a non-list meta or rows")
        if not all(isinstance(r, dict) for r in (*meta, *rows)):
            raise ValueError("statistics payload contains a non-object record")
        try:
            return cls(
                schema_version=int(data["schema_version"]),
                meta=meta,
                rows=rows,
                watermark=float(data.get("watermark") or 0.0),
                covers_from=float(data.get("covers_from") or 0.0),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise ValueError(f"statistics payload has a bad header: {err}") from err


def schema_version(conn: sqlite3.Connection) -> int:
    """The recorder's own schema version, or 0 when it cannot be established.

    0 is deliberately falsy and never equal to a real version, so an
    unreadable schema fails the guard rather than passing it.
    """
    try:
        row = conn.execute(
            "SELECT schema_version FROM schema_changes ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def export_since(db_path: str | pathlib.Path, since_ts: float = 0.0) -> SyncPayload:
    """Read statistics newer than `since_ts`, with the metadata they need.

    Opened read-only: this runs against a live recorder on the leader and must
    never write to it.

    All metadata is sent every time, not just new metadata. It is ~1,000 rows,
    it is what makes the remapping possible, and a row whose metadata went
    missing would be unattachable on arrival.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=60.0)
    con.row_factory = sqlite3.Row
    try:
        payload = SyncPayload(schema_version=schema_version(con), covers_from=since_ts)
        payload.meta = [
            dict(r) for r in con.execute(f"SELECT {', '.join(META_COLUMNS)} FROM {META_TABLE}")
        ]
        cols = ", ".join(f"s.{c}" for c in STATS_COLUMNS)
        rows = con.execute(
            f"SELECT m.statistic_id, {cols} FROM {STATS_TABLE} s "
            f"JOIN {META_TABLE} m ON m.id = s.metadata_id "
            "WHERE s.start_ts > ? ORDER BY s.start_ts",
            (since_ts,),
        )
        payload.rows = [dict(r) for r in rows]
        if payload.rows:
            payload.watermark = max(float(r["start_ts"] or 0) for r in payload.rows)
        else:
            payload.watermark = since_ts
        return payload
    finally:
        con.close()


def apply_payload(db_path: str | pathlib.Path, payload: SyncPayload) -> int:
    """Apply a payload to this node's recorder. Returns rows inserted.

    🚨 Refuses outright on a schema mismatch. The alternative -- writing rows
    shaped for one schema into another -- corrupts history in a way that opens
    cleanly and is wrong, which is the failure this project exists to prevent.

    Idempotent: `INSERT OR IGNORE` against the natural key `(metadata_id,
    start_ts)`, so replaying a payload changes nothing. That matters because a
    follower may fetch the same payload twice after an interrupted run.
    """
    con = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        local = schema_version(con)
        if local != payload.schema_version:
            raise SchemaMismatch(
                f"this node's recorder schema is {local}, the peer's is "
                f"{payload.schema_version}. Refusing to apply: writing rows shaped "
                "for one schema into another corrupts history silently. Bring both "
                "nodes to the same Home Assistant version."
            )

        # Metadata first, keyed on the stable string name. The numeric id is
        # whatever this database already uses, or a new one -- never the peer's.
        for m in payload.meta:
            con.execute(
                f"INSERT OR IGNORE INTO {META_TABLE} "
                f"({', '.join(META_COLUMNS)}) VALUES ({', '.join('?' * len(META_COLUMNS))})",
                tuple(m.get(c) for c in META_COLUMNS),
            )
        ids = {r[0]: r[1] for r in con.execute(f"SELECT statistic_id, id FROM {META_TABLE}")}

        inserted = 0
        placeholders = ", ".join("?" * (len(STATS_COLUMNS) + 1))
        for row in payload.rows:
            metadata_id = ids.get(row["statistic_id"])
            if metadata_id is None:
                # Metadata that never arrived. Skipping is right: an orphaned
                # reading attached to a guessed sensor is worse than a gap.
                continue
            cur = con.execute(
                f"INSERT OR IGNORE INTO {STATS_TABLE} "
                f"(metadata_id, {', '.join(STATS_COLUMNS)}) VALUES ({placeholders})",
                (metadata_id, *(row.get(c) for c in STATS_COLUMNS)),
            )
            inserted += cur.rowcount if cur.rowcount > 0 else 0
        con.commit()
        return inserted
    finally:
        con.close()


#: Copied wholesale into a seed. Everything else in the recorder gets its
#: schema but no rows: `states`, `events` and their satellites are the ten-day
#: churn this design deliberately drops, and `recorder_runs`/`statistics_runs`
#: are a node's own bookkeeping, which the promoted node appends to itself.
#:
#: `migration_changes` travels with `schema_changes` because modern recorder
#: versions consult BOTH to decide whether a database needs migrating. A seed
#: carrying one and not the other reads as half-migrated.
SEED_DATA_TABLES = ("schema_changes", "migration_changes", META_TABLE, STATS_TABLE)


def write_seed(db_path: str | pathlib.Path, out_path: str | pathlib.Path) -> int:
    """Write a seed: the recorder's whole schema, but only statistics in it.

    The standby needs a starting point -- 6.4 million existing rows would take
    years to arrive at 5,500 a day -- and it needs one **Home Assistant can
    open**. That is the constraint that shapes this function.

    A file containing only `statistics_meta` and `statistics` is not a recorder
    database. Home Assistant queries `states`, `events`, `states_meta` and nine
    other tables on startup; against a file missing them it does not report a
    bad seed, it attempts a migration or crashes the recorder -- and a house
    whose recorder is down looks fine until someone opens a graph.

    So this copies **every** table and index definition from the source, and
    populates only `SEED_DATA_TABLES`. The result is a valid, fully-shaped,
    almost-empty recorder holding years of statistics: Home Assistant opens it,
    finds its schema current, and starts appending.

    Measured against the live 2.2 GB recorder, 2026-09-08: **6,409,818 rows,
    484 MB, 4.8 seconds**. Copied to a standby and adopted there, it produced a
    store whose schema matched the leader's exactly -- all 13 tables, all 20
    indexes -- which the recorder in the running Home Assistant image reported
    as schema 53, current, no migration needed.

    Returns the number of statistics rows written.
    """
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=120.0)
    try:
        version = schema_version(src)
        if not version:
            raise SchemaMismatch(
                f"{db_path} has no readable recorder schema version; refusing to "
                "write a seed whose schema cannot be checked on arrival"
            )
        out = pathlib.Path(out_path)
        tmp = out.with_name(out.name + ".tmp")
        tmp.unlink(missing_ok=True)
        try:
            rows = _fill_seed(src, tmp)
            # Renamed only once the file is whole. After this the temporary
            # path no longer exists, so the cleanup below finds nothing.
            tmp.replace(out)
        finally:
            # AR-0047. The likeliest failure here is a full disk, and the
            # partial file measured 484 MB. Leaving it behind makes the
            # condition that caused the failure worse, and the next attempt
            # starts with less room than this one had.
            tmp.unlink(missing_ok=True)
        _LOGGER.info(
            "Statistics seed written: %d rows, recorder schema %d, %d bytes",
            rows,
            version,
            out.stat().st_size,
        )
        return rows
    finally:
        src.close()


def _fill_seed(src: sqlite3.Connection, tmp: pathlib.Path) -> int:
    """Build the seed at `tmp`. Returns the statistics row count."""
    dst = sqlite3.connect(str(tmp))
    try:
        # Tables first, then indexes -- an index cannot be created before the
        # table it is on. `sqlite_*` names are SQLite's own internals
        # (`sqlite_stat1`, the auto-indexes); CREATE against them is rejected
        # outright, and they are rebuilt by ANALYZE anyway.
        for kind in ("table", "index"):
            for (ddl,) in src.execute(
                "SELECT sql FROM sqlite_master WHERE type=? AND sql IS NOT NULL "
                "AND name NOT LIKE 'sqlite_%'",
                (kind,),
            ):
                dst.execute(ddl)
        dst.commit()
    finally:
        dst.close()

    # ATTACH from the read-only source, so the bulk copy happens inside SQLite
    # rather than through a million round trips in Python. The source stays
    # read-only; only the attached seed is written.
    src.execute("ATTACH DATABASE ? AS seed", (str(tmp),))
    try:
        present = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in SEED_DATA_TABLES:
            if table in present:
                src.execute(f"INSERT INTO seed.{table} SELECT * FROM main.{table}")
        src.commit()
        return int(src.execute(f"SELECT COUNT(*) FROM seed.{STATS_TABLE}").fetchone()[0])
    finally:
        src.execute("DETACH DATABASE seed")
