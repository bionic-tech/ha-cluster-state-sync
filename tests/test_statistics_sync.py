"""Long-term statistics replication.

Only statistics, because measurement said so: multi-year statistics grow ~5,500
rows/day while raw `states` churns ~324,000/day, and block-replicating the whole
database cost 4 GB/day through Valkey. This ships the part worth having.
"""

from __future__ import annotations

import pathlib
import sqlite3

import pytest

from custom_components.cluster_state_sync.statistics_sync import (
    SchemaMismatch,
    apply_payload,
    export_since,
    schema_version,
    write_seed,
)

SCHEMA = 53


def _recorder(path: pathlib.Path, version: int = SCHEMA) -> sqlite3.Connection:
    """A miniature of Home Assistant's recorder, real enough to exercise ids."""
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE schema_changes (
            change_id INTEGER PRIMARY KEY, schema_version INTEGER, changed TEXT);
        CREATE TABLE statistics_meta (
            id INTEGER PRIMARY KEY, statistic_id TEXT UNIQUE, source TEXT,
            unit_of_measurement TEXT, has_mean INTEGER, has_sum INTEGER, name TEXT);
        CREATE TABLE statistics (
            id INTEGER PRIMARY KEY, metadata_id INTEGER, created_ts REAL,
            start_ts REAL, mean REAL, min REAL, max REAL, last_reset_ts REAL,
            state REAL, sum REAL,
            UNIQUE(metadata_id, start_ts));
        CREATE INDEX ix_statistics_start_ts ON statistics (start_ts);
        CREATE TABLE migration_changes (
            migration_id TEXT PRIMARY KEY, version INTEGER);
        -- The ten-day churn this design drops. Present in the seed's SCHEMA
        -- because Home Assistant queries them on startup; empty of rows.
        CREATE TABLE states (
            state_id INTEGER PRIMARY KEY, metadata_id INTEGER, state TEXT);
        CREATE TABLE states_meta (
            metadata_id INTEGER PRIMARY KEY, entity_id TEXT);
        CREATE TABLE recorder_runs (
            run_id INTEGER PRIMARY KEY, start TEXT);
        """
    )
    con.execute("INSERT INTO schema_changes (schema_version, changed) VALUES (?, '')", (version,))
    con.commit()
    return con


def _add(con, statistic_id, start_ts, value, meta_id=None):
    con.execute(
        "INSERT OR IGNORE INTO statistics_meta (statistic_id, source, has_mean, has_sum) "
        "VALUES (?, 'recorder', 1, 0)",
        (statistic_id,),
    )
    mid = con.execute(
        "SELECT id FROM statistics_meta WHERE statistic_id=?", (statistic_id,)
    ).fetchone()[0]
    con.execute(
        "INSERT OR IGNORE INTO statistics (metadata_id, start_ts, mean) VALUES (?,?,?)",
        (mid, start_ts, value),
    )
    con.commit()
    return mid


def test_rows_are_remapped_by_name_never_by_numeric_id(tmp_path: pathlib.Path) -> None:
    """🚨 The corruption this design most needs to avoid.

    `statistics.metadata_id` is local to each database. If the peer's numeric id
    were copied across, one node's energy readings would silently attach to a
    different node's sensor -- a graph that looks fine and is wrong.

    Here the two databases deliberately assign DIFFERENT ids to the same
    statistic, which is exactly what happens in the wild.
    """
    leader, follower = tmp_path / "l.db", tmp_path / "f.db"
    lc, fc = _recorder(leader), _recorder(follower)

    # Follower already knows a different sensor, so its id sequence is offset.
    _add(fc, "sensor.decoy", 1000.0, 0.0)
    _add(fc, "sensor.energy", 1000.0, 1.0)
    follower_id = fc.execute(
        "SELECT id FROM statistics_meta WHERE statistic_id='sensor.energy'"
    ).fetchone()[0]

    leader_id = _add(lc, "sensor.energy", 2000.0, 42.0)
    assert leader_id != follower_id, "test is meaningless unless the ids differ"
    lc.close()

    apply_payload(follower, export_since(leader, since_ts=1500.0))

    row = fc.execute(
        "SELECT s.mean FROM statistics s JOIN statistics_meta m ON m.id=s.metadata_id "
        "WHERE m.statistic_id='sensor.energy' AND s.start_ts=2000.0"
    ).fetchone()
    assert row is not None and row[0] == 42.0, "the reading did not land on the right sensor"
    # And nothing was attached to the decoy.
    decoy = fc.execute(
        "SELECT COUNT(*) FROM statistics s JOIN statistics_meta m ON m.id=s.metadata_id "
        "WHERE m.statistic_id='sensor.decoy'"
    ).fetchone()[0]
    assert decoy == 1, "a reading was attached to the wrong sensor"
    fc.close()


def test_a_schema_mismatch_refuses_rather_than_guessing(tmp_path: pathlib.Path) -> None:
    """A gap in history beats a database that opens cleanly and is wrong."""
    leader, follower = tmp_path / "l.db", tmp_path / "f.db"
    lc = _recorder(leader, version=53)
    fc = _recorder(follower, version=52)
    _add(lc, "sensor.energy", 2000.0, 42.0)
    lc.close()

    with pytest.raises(SchemaMismatch) as err:
        apply_payload(follower, export_since(leader))
    assert "52" in str(err.value) and "53" in str(err.value)
    assert fc.execute("SELECT COUNT(*) FROM statistics").fetchone()[0] == 0
    fc.close()


def test_an_unreadable_schema_fails_the_guard_rather_than_passing_it(
    tmp_path: pathlib.Path,
) -> None:
    """`schema_version` returns 0, which is never equal to a real version.

    Failing open here would mean a database with no recognisable schema quietly
    accepting rows.
    """
    blank = tmp_path / "blank.db"
    con = sqlite3.connect(blank)
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    assert schema_version(con) == 0
    con.close()


def test_applying_twice_changes_nothing(tmp_path: pathlib.Path) -> None:
    """A follower may fetch the same payload twice after an interrupted run."""
    leader, follower = tmp_path / "l.db", tmp_path / "f.db"
    lc, fc = _recorder(leader), _recorder(follower)
    _add(lc, "sensor.energy", 2000.0, 42.0)
    lc.close()

    payload = export_since(leader)
    first = apply_payload(follower, payload)
    second = apply_payload(follower, payload)
    assert first == 1
    assert second == 0, "a replay inserted duplicate rows"
    assert fc.execute("SELECT COUNT(*) FROM statistics").fetchone()[0] == 1
    fc.close()


def test_the_watermark_advances_so_the_next_export_is_a_delta(
    tmp_path: pathlib.Path,
) -> None:
    """The whole point: ~5,500 rows a day, not 6.4 million every time."""
    leader = tmp_path / "l.db"
    lc = _recorder(leader)
    _add(lc, "sensor.energy", 1000.0, 1.0)
    _add(lc, "sensor.energy", 2000.0, 2.0)

    first = export_since(leader, since_ts=0.0)
    assert len(first.rows) == 2
    assert first.watermark == 2000.0

    _add(lc, "sensor.energy", 3000.0, 3.0)
    lc.close()

    second = export_since(leader, since_ts=first.watermark)
    assert len(second.rows) == 1, "the export was not a delta"
    assert second.rows[0]["start_ts"] == 3000.0


def test_the_source_database_is_never_written(tmp_path: pathlib.Path) -> None:
    """Export runs against a LIVE recorder on the leader."""
    leader = tmp_path / "l.db"
    lc = _recorder(leader)
    _add(lc, "sensor.energy", 1000.0, 1.0)
    lc.close()
    before = leader.stat().st_mtime_ns, leader.stat().st_size
    export_since(leader)
    assert (leader.stat().st_mtime_ns, leader.stat().st_size) == before


def test_a_reading_whose_metadata_never_arrived_is_skipped_not_guessed(
    tmp_path: pathlib.Path,
) -> None:
    """An orphaned reading attached to a guessed sensor is worse than a gap."""
    leader, follower = tmp_path / "l.db", tmp_path / "f.db"
    lc, fc = _recorder(leader), _recorder(follower)
    _add(lc, "sensor.energy", 2000.0, 42.0)
    lc.close()

    payload = export_since(leader)
    payload.meta = []  # metadata lost in transit
    assert apply_payload(follower, payload) == 0
    assert fc.execute("SELECT COUNT(*) FROM statistics").fetchone()[0] == 0
    fc.close()


def test_the_seed_is_a_database_home_assistant_can_actually_open(
    tmp_path: pathlib.Path,
) -> None:
    """🚨 The seed's schema must be COMPLETE, not just the statistics tables.

    A file holding only `statistics_meta` and `statistics` looks like a
    successful seed and is not a recorder. Home Assistant queries `states`,
    `states_meta`, `recorder_runs` and the rest on startup; against a file
    missing them it attempts a migration or the recorder dies -- and a house
    whose recorder is down looks completely normal until someone opens a graph.
    """
    leader, seed = tmp_path / "l.db", tmp_path / "seed.db"
    lc = _recorder(leader)
    lc.execute("INSERT INTO migration_changes VALUES ('context_id', 1)")
    lc.execute("INSERT INTO states_meta VALUES (1, 'sensor.energy')")
    lc.execute("INSERT INTO states VALUES (1, 1, '42')")
    for i in range(5):
        _add(lc, "sensor.energy", 1000.0 + i, float(i))
    lc.commit()
    lc.close()

    assert write_seed(leader, seed) == 5
    sc = sqlite3.connect(seed)

    # Every table Home Assistant will open is present...
    tables = {r[0] for r in sc.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"states", "states_meta", "recorder_runs", "migration_changes"} <= tables

    # ...and so are the indexes, or the first Energy query table-scans 6.4M rows.
    indexes = {r[0] for r in sc.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "ix_statistics_start_ts" in indexes

    # The statistics came across.
    assert sc.execute("SELECT COUNT(*) FROM statistics").fetchone()[0] == 5
    assert sc.execute("SELECT COUNT(*) FROM statistics_meta").fetchone()[0] == 1

    # The schema version came across, so `apply_payload`'s guard can check it,
    # and `migration_changes` with it -- the recorder consults both to decide
    # whether the database needs migrating.
    assert schema_version(sc) == SCHEMA
    assert sc.execute("SELECT COUNT(*) FROM migration_changes").fetchone()[0] == 1

    # But the ten-day churn did NOT -- that is the whole point of a seed.
    assert sc.execute("SELECT COUNT(*) FROM states").fetchone()[0] == 0
    sc.close()


def test_a_seed_from_an_unreadable_schema_is_refused(tmp_path: pathlib.Path) -> None:
    """Its schema version is what the standby checks every delta against.

    A seed carrying 0 would refuse every payload forever, and the operator
    would have copied a large file for nothing.
    """
    blank = tmp_path / "blank.db"
    con = sqlite3.connect(blank)
    con.execute("CREATE TABLE statistics (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    with pytest.raises(SchemaMismatch):
        write_seed(blank, tmp_path / "seed.db")
    assert not (tmp_path / "seed.db").exists()


def test_a_seed_accepts_the_deltas_that_follow_it(tmp_path: pathlib.Path) -> None:
    """The seed and the delta stream are one mechanism, so test them joined.

    This is the whole cold-standby story in one test: seed once by hand, then
    let the deltas land on it.
    """
    leader, seed = tmp_path / "l.db", tmp_path / "seed.db"
    lc = _recorder(leader)
    _add(lc, "sensor.energy", 1000.0, 1.0)
    write_seed(leader, seed)

    _add(lc, "sensor.energy", 2000.0, 2.0)
    lc.close()

    assert apply_payload(seed, export_since(leader, since_ts=1000.0)) == 1
    sc = sqlite3.connect(seed)
    assert sc.execute("SELECT COUNT(*) FROM statistics").fetchone()[0] == 2
    sc.close()
